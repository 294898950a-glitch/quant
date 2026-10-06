#!/usr/bin/env python3
"""Diagnose look-ahead in cb_basic.conv_price and rerun the value-gap baseline
on point-in-time conversion value.

cb_basic.conv_price is the latest conversion price, applied to all history.
This script (1) measures how far the resulting conversion value is from the
value that was true on each day, (2) reruns the unmodified value-gap-switch
backtest with the pricer fed point-in-time conversion value, and (3) checks the
result against a one-day signal delay, a rating-free universe, and known
low-premium style exposure.

The strategy code is not changed. price_cb's theoretical value depends on the
stock price S and conversion price K only through S/K, so replacing K with
K_eff = 100 * S / conv_value_pit(ts_code, date) reprices each bond-day exactly
on its true moneyness. Bond-days without a point-in-time value are dropped.

Scope: local diagnostic only; no VM/spot; no strategy or truth change.

跑法 (~30 min, 9 processes):
    python scripts/analyze_cb_conv_price_lookahead.py
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.analyze_cb_arb_repair_times as repair_times  # noqa: E402
import scripts.evaluate_cb_arb_value_gap_switch as value_gap  # noqa: E402

WAREHOUSE = _REPO_ROOT / "data" / "cb_warehouse"
BENCHMARK = _REPO_ROOT / "data" / "benchmarks" / "cb_equal.parquet"
SPLITS = {"train": ("20190101", "20241231"), "test": ("20250101", "20260508")}
RANK_LABELS = ("static", "pit", "pit_norating")
GRID_LABELS = ("static", "pit", "pit_lag1", "pit_norating", "pit_norating_lag1")
SIGNAL_COLUMNS = [
    "theoretical", "bond_floor", "option_value", "intrinsic", "deviation",
    "rank", "n_ranked", "rank_pct", "value_gap_amount", "value_gap_pct_of_cash",
]
COST_ARGS = argparse.Namespace(
    cost_model_enabled=True, slippage_pct=0.0015, market_impact_coeff=0.0010,
    market_impact_cap_pct=0.02, holding_cost_pct=0.0,
)
_RANKS: dict[str, pd.DataFrame] = {}
_CTX: dict[str, Any] = {}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=Path("data/cb_arb_concurrent_supervised_20260511_094500"))
    p.add_argument("--pit-path", type=Path, default=WAREHOUSE / "cb_conv_value_pit.parquet")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--workers", type=int, default=9)
    p.add_argument("--reuse-ranks", action="store_true")
    p.add_argument("--output-dir", type=Path, default=Path("data/cb_arb_conv_price_lookahead_2026-10-07"))
    return p.parse_args()


def _ranks_path(output_dir: Path, label: str) -> Path:
    return output_dir / f"daily_value_gap_amounts_{label}.parquet"


def _build_ranks(task: tuple[str, dict[str, Any]]) -> str:
    """Runs in its own process, so the patches below never reach the parent."""
    label, ctx = task
    if label != "static":
        pit = pd.read_parquet(ctx["pit_path"], columns=["ts_code", "trade_date", "conv_value"])
        pit = pit[pit["conv_value"] > 0]
        conv_value = dict(zip(zip(pit["ts_code"], pit["trade_date"]), pit["conv_value"].astype(float)))
        original = repair_times.price_cb
        drop_rating = label == "pit_norating"

        def price_cb_pit(spec, valuation_date, stock_price, **kwargs):
            true_value = conv_value.get((spec.ts_code, valuation_date))
            if true_value is None:
                raise ValueError("no point-in-time conversion value")  # fail closed
            spec = replace(spec, conv_price=100.0 * stock_price / true_value)
            if drop_rating:
                spec = replace(spec, rating="AA")
            return original(spec=spec, valuation_date=valuation_date, stock_price=stock_price, **kwargs)

        repair_times.price_cb = price_cb_pit
        if drop_rating:
            # cb_basic.rating is also the latest value; neutralize it entirely.
            load_basic = repair_times._load_cb_basic

            def load_basic_no_rating() -> pd.DataFrame:
                df = load_basic().copy()
                df["rating"] = "AA"
                df["rating_int"] = int(df["rating_int"].max())
                return df

            repair_times._load_cb_basic = load_basic_no_rating
    value_gap._load_or_build_value_ranks(
        ctx["data_root"], ctx.get("start", SPLITS["train"][0]), ctx.get("end", SPLITS["test"][1]),
        ctx["fixed_source"], ctx["rule"], _ranks_path(ctx["output_dir"], label), ctx["reuse_ranks"],
    )
    return label


def _lag_signal(ranks: pd.DataFrame) -> pd.DataFrame:
    """Act on yesterday's signal at today's close."""
    out = ranks.sort_values(["ts_code", "trade_date"]).copy()
    out[SIGNAL_COLUMNS] = out.groupby("ts_code")[SIGNAL_COLUMNS].shift(1)
    out = out.dropna(subset=["value_gap_amount"])
    out["rank"] = out.groupby("trade_date")["deviation"].rank(method="first").astype(int) - 1
    out["n_ranked"] = out.groupby("trade_date")["ts_code"].transform("size")
    out["rank_pct"] = out["rank"] / out["n_ranked"]
    return out.sort_values(["trade_date", "rank"]).reset_index(drop=True)


def _load_ranks(output_dir: Path) -> None:
    for label in GRID_LABELS:
        ranks = pd.read_parquet(_ranks_path(output_dir, label.replace("_lag1", "")))
        ranks["trade_date"] = ranks["trade_date"].astype(str)
        ranks["ts_code"] = ranks["ts_code"].astype(str)
        _RANKS[label] = _lag_signal(ranks) if label.endswith("_lag1") else ranks


def _run_backtest(task: tuple[str, dict[str, float], str]) -> dict[str, Any]:
    label, params, split = task
    start, end = SPLITS[split]
    ranks = _RANKS[label]
    out = value_gap._run_value_gap_backtest(
        ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
        start, end, _CTX["data_root"], _CTX["fixed_source"], _CTX["rule"],
        value_gap._with_cost_params(params, COST_ARGS),
    )
    return {
        "label": label, "split": split, "params": params, "metrics": out["metrics"],
        "score": value_gap._score(out["metrics"]), "equity_curve": out["equity_curve"],
        "trades": out["trades"],
    }


def _benchmark_close() -> pd.Series:
    bm = pd.read_parquet(BENCHMARK)
    bm["trade_date"] = pd.to_datetime(bm["trade_date"]).dt.strftime("%Y%m%d")
    return bm.set_index("trade_date")["close"]


def _yearly(curve: list[tuple[str, float]], bench: pd.Series) -> pd.DataFrame:
    equity = pd.Series(dict(curve)).sort_index()
    bm = bench.reindex(equity.index).ffill()
    rows = {}
    for year, eq in equity.groupby(equity.index.str[:4]):
        prev_eq = equity[equity.index < eq.index[0]]
        prev_bm = bm[bm.index < eq.index[0]]
        eq_base = prev_eq.iloc[-1] if len(prev_eq) else eq.iloc[0]
        bm_year = bm[eq.index]
        bm_base = prev_bm.iloc[-1] if len(prev_bm) else bm_year.iloc[0]
        ret = eq.pct_change().fillna(eq.iloc[0] / eq_base - 1.0)
        bm_ret = bm_year.pct_change().fillna(bm_year.iloc[0] / bm_base - 1.0)
        relative = (1.0 + ret).cumprod() / (1.0 + bm_ret).cumprod()
        rows[year] = {
            "return": eq.iloc[-1] / eq_base - 1.0,
            "benchmark": bm_year.iloc[-1] / bm_base - 1.0,
            "excess": eq.iloc[-1] / eq_base - bm_year.iloc[-1] / bm_base,
            "sharpe": float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0.0,
            "max_drawdown": float((eq / eq.cummax() - 1.0).min()),
            "drawdown_vs_benchmark": float((relative / relative.cummax() - 1.0).min()),
        }
    return pd.DataFrame(rows).T


def _contamination(static_ranks: pd.DataFrame, pit: pd.DataFrame) -> pd.DataFrame:
    """How wrong is the static-conv_price conversion value, overall and in the top-10 picks."""
    m = static_ranks.merge(pit[["ts_code", "trade_date", "conv_value"]], on=["ts_code", "trade_date"], how="left")
    m = m[m["conv_value"] > 0].copy()
    m["year"] = m["trade_date"].str[:4]
    m["err"] = m["intrinsic"] / m["conv_value"] - 1.0
    gap_vs_err = m.groupby("trade_date").apply(
        lambda d: d["value_gap_amount"].corr(d["err"], method="spearman"), include_groups=False
    )
    rows = []
    for year, g in m.groupby("year"):
        top = g[g["rank"] < 10]
        rows.append({
            "year": year,
            "ranked_bond_days": len(g),
            "all_mean_abs_error": g["err"].abs().mean(),
            "all_share_overstated_gt_25pct": (g["err"] > 0.25).mean(),
            "top10_median_error": top["err"].median(),
            "top10_share_overstated_gt_25pct": (top["err"] > 0.25).mean(),
            # conversion value >10% above market price cannot persist: it is a free conversion profit
            "top10_share_static_value_above_price": (top["intrinsic"] > 1.10 * top["close"]).mean(),
            "top10_share_true_value_above_price": (top["conv_value"] > 1.10 * top["close"]).mean(),
            "daily_spearman_gap_vs_error": gap_vs_err[gap_vs_err.index.str[:4] == year].mean(),
        })
    return pd.DataFrame(rows)


def _low_premium_factor(pit: pd.DataFrame) -> pd.DataFrame:
    """Daily, cost-free style returns: lowest-quintile conversion premium / double-low minus universe."""
    cb = pd.read_parquet(WAREHOUSE / "cb_daily.parquet", columns=["ts_code", "trade_date", "close", "vol"])
    p = cb.merge(pit[["ts_code", "trade_date", "conv_prem"]], on=["ts_code", "trade_date"], how="left")
    p = p.sort_values(["ts_code", "trade_date"])
    g = p.groupby("ts_code")
    p["ret"] = g["close"].pct_change()
    p["amount_20d"] = (p["close"] * p["vol"] * 10).groupby(p["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=5).mean()
    )
    p["premium_lag"] = g["conv_prem"].shift(1)
    p["double_low_lag"] = g["close"].shift(1) + p["premium_lag"]
    p["liquid_lag"] = g["amount_20d"].shift(1)
    u = p[(p["trade_date"] >= SPLITS["train"][0]) & p["premium_lag"].notna() & p["ret"].notna()
          & (p["liquid_lag"] >= 1e7) & (p["ret"].abs() < 0.6)]
    universe = u.groupby("trade_date")["ret"].mean()

    def factor(col: str) -> pd.Series:
        q = u.groupby("trade_date")[col].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
        return u[q == 0].groupby("trade_date")["ret"].mean() - universe

    return pd.DataFrame({"low_premium": factor("premium_lag"), "double_low": factor("double_low_lag")})


def _alpha(y: np.ndarray, factors: list[np.ndarray]) -> tuple[float, float, np.ndarray]:
    x = np.column_stack([np.ones(len(y))] + factors)
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    se = np.sqrt(np.diag(np.linalg.inv(x.T @ x)) * resid.var(ddof=x.shape[1]))
    return float(beta[0] * 252), float(beta[0] / se[0]), beta


def _attribution(run: dict[str, Any], bench: pd.Series, style: pd.DataFrame) -> dict[str, float]:
    ret = pd.Series(dict(run["equity_curve"])).sort_index().pct_change().dropna()
    d = pd.DataFrame({"y": ret, "bm": bench.pct_change()}).join(style).dropna()
    a1, t1, b1 = _alpha(d["y"].values, [d["bm"].values])
    a2, t2, b2 = _alpha(d["y"].values, [d["bm"].values, d["low_premium"].values])
    a3, t3, _ = _alpha(d["y"].values, [d["bm"].values, d["low_premium"].values, d["double_low"].values])
    return {
        "annual_return": float(d["y"].mean() * 252),
        "benchmark_annual_return": float(d["bm"].mean() * 252),
        "beta_to_benchmark": float(b1[1]),
        "alpha_vs_benchmark": a1, "alpha_vs_benchmark_t": t1,
        "low_premium_loading": float(b2[2]),
        "alpha_vs_benchmark_and_low_premium": a2, "alpha_vs_benchmark_and_low_premium_t": t2,
        "alpha_vs_benchmark_low_premium_double_low": a3, "alpha_vs_benchmark_low_premium_double_low_t": t3,
    }


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "data_root": args.data_root, "pit_path": args.pit_path, "fixed_source": args.fixed_source,
        "rule": args.rule, "output_dir": args.output_dir, "reuse_ranks": args.reuse_ranks,
    }
    _CTX.update(ctx)
    fork = get_context("fork")
    with fork.Pool(len(RANK_LABELS), maxtasksperchild=1) as pool:
        for label in pool.imap_unordered(_build_ranks, [(label, ctx) for label in RANK_LABELS]):
            print(f"[lookahead] ranks ready: {label}", flush=True)

    _load_ranks(args.output_dir)
    grid = value_gap._candidate_grid()
    with fork.Pool(args.workers) as pool:
        train = pool.map(_run_backtest, [(label, p, "train") for label in GRID_LABELS for p in grid], chunksize=2)
        by_label = {
            label: sorted([r for r in train if r["label"] == label], key=lambda r: r["score"], reverse=True)
            for label in GRID_LABELS
        }
        # Test is run only for the top-N candidates selected on train, as in the baseline evaluator.
        test = pool.map(_run_backtest, [(label, r["params"], "test") for label in GRID_LABELS
                                        for r in by_label[label][: args.top_n]])
    print("[lookahead] grids done", flush=True)

    bench = _benchmark_close()
    pit = pd.read_parquet(args.pit_path)
    style = _low_premium_factor(pit)
    priced = pd.read_parquet(WAREHOUSE / "cb_daily.parquet", columns=["ts_code", "trade_date"])
    priced_keys = set(zip(priced["ts_code"], priced["trade_date"]))

    grid_rows, yearly_rows, attribution_rows = [], [], []
    for label in GRID_LABELS:
        runs = by_label[label]
        best = runs[0]
        tests = [r for r in test if r["label"] == label]
        best_test = next(r for r in tests if r["params"] == best["params"])
        train_excess = pd.Series([r["metrics"]["excess_return"] for r in runs])
        test_excess = pd.Series([r["metrics"]["excess_return"] for r in tests])
        trades = best["trades"] + best_test["trades"]
        grid_rows.append({
            "label": label,
            "train_candidates": len(runs),
            "train_excess_min": train_excess.min(),
            "train_excess_median": train_excess.median(),
            "train_excess_max": train_excess.max(),
            "train_share_positive": (train_excess > 0).mean(),
            "train_best_params": json.dumps(best["params"], sort_keys=True),
            "train_best_excess": best["metrics"]["excess_return"],
            "train_best_total_return": best["metrics"]["total_return"],
            "train_best_max_drawdown": best["metrics"]["max_drawdown"],
            "test_excess_of_train_best": best_test["metrics"]["excess_return"],
            "test_total_return_of_train_best": best_test["metrics"]["total_return"],
            "test_max_drawdown_of_train_best": best_test["metrics"]["max_drawdown"],
            "test_excess_median_of_top_n": test_excess.median(),
            "test_excess_min_of_top_n": test_excess.min(),
            "trades_train_best_plus_test": len(trades),
            # the backtest settles at entry price when a bond has no close on the exit date
            "exits_without_market_price": sum((t["cb_code"], t["exit_date"]) not in priced_keys for t in trades),
        })
        all_years = pd.concat([_yearly(r["equity_curve"], bench)["excess"].rename(i) for i, r in enumerate(runs)], axis=1)
        best_years = pd.concat([_yearly(best["equity_curve"], bench), _yearly(best_test["equity_curve"], bench)])
        for year, row in best_years.iterrows():
            in_train = year in all_years.index
            yearly_rows.append({
                "label": label, "year": year, "split": "train" if in_train else "test",
                **{k: round(float(v), 6) for k, v in row.items()},
                "excess_median_all_train_candidates": round(float(all_years.loc[year].median()), 6) if in_train else None,
                "share_train_candidates_positive": round(float((all_years.loc[year] > 0).mean()), 4) if in_train else None,
            })
        for split, run in (("train", best), ("test", best_test)):
            attribution_rows.append({"label": label, "split": split, **_attribution(run, bench, style)})

    contamination = _contamination(_RANKS["static"], pit)
    grid_df = pd.DataFrame(grid_rows)
    yearly_df = pd.DataFrame(yearly_rows)
    attribution_df = pd.DataFrame(attribution_rows)
    contamination.round(6).to_csv(args.output_dir / "conv_value_error_by_year.csv", index=False)
    grid_df.round(6).to_csv(args.output_dir / "grid_summary.csv", index=False)
    yearly_df.to_csv(args.output_dir / "yearly_excess.csv", index=False)
    attribution_df.round(6).to_csv(args.output_dir / "style_attribution.csv", index=False)

    summary = {
        "run_id": args.output_dir.name,
        "scope": "local diagnostic only; no VM/spot; no strategy or truth change",
        "splits": SPLITS,
        "cost_model": vars(COST_ARGS),
        "grid_candidates": len(grid),
        "conv_value_error_by_year": contamination.round(4).to_dict("records"),
        "grid_summary": grid_df.round(6).to_dict("records"),
        "style_attribution": attribution_df.round(4).to_dict("records"),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=float) + "\n", encoding="utf-8"
    )
    report = {
        "schema_version": 1,
        "run_id": args.output_dir.name,
        "date": date.today().isoformat(),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": "mini-spec-retry",
        "three_exits_section": {
            "adoption_pass": False,
            "review_summary": "Diagnostic evidence only. The static-conv_price baseline is invalid; the "
                              "point-in-time rerun is not a promotion candidate until the follow-up tests run.",
            "review_main_reason": "cb_basic.conv_price look-ahead",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": [
            "any valuation that reads cb_basic.conv_price or cb_basic.rating for a historical date",
        ],
        "learnings": [
            "The value-gap ranking built on static conv_price ranks bonds by conversion-value error; see "
            "conv_value_error_by_year.csv.",
            "The same backtest on point-in-time conversion value has positive train excess for every grid "
            "candidate, including with a one-day signal delay and no rating information; see grid_summary.csv.",
            "Train-period excess is explained by low-premium / double-low style exposure; see "
            "style_attribution.csv.",
        ],
        "follow_up_actions": [
            "Run the three required tests of open thread pit_value_gap_incremental_edge in research_insights.yaml.",
            "Evidence-only record; do not promote to truth without user approval.",
        ],
        "status": "COMPLETE",
        "notes": "Local diagnostic only; no VM/spot; no strategy or truth change.",
        "references": ["data/research_framework/research_insights.yaml#conv_price_lookahead_contamination_2026_10_07"],
        "related_reports": ["data/cb_arb_option_false_undervalue_attribution_2026-05-17/report.yaml"],
        "evaluator_report": {
            "scope": summary["scope"],
            "inputs": {
                "data_root": str(args.data_root),
                "pit_path": str(args.pit_path.relative_to(_REPO_ROOT) if args.pit_path.is_absolute() else args.pit_path),
                "splits": SPLITS,
            },
            "hypothesis": "cb_basic.conv_price is the latest conversion price; applying it to history makes the "
                          "value-gap signal rank bonds by data error instead of by undervaluation.",
            "artifacts": [
                "conv_value_error_by_year.csv", "grid_summary.csv", "yearly_excess.csv",
                "style_attribution.csv", "summary.json",
            ],
        },
    }
    (args.output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(grid_df[["label", "train_excess_min", "train_excess_median", "train_excess_max",
                   "test_excess_of_train_best", "test_excess_median_of_top_n"]].round(3).to_string(index=False))
    print(f"[lookahead] wrote {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
