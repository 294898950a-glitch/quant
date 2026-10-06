#!/usr/bin/env python3
"""Fresh-holdout and matched-style-control test for the frozen point-in-time
value-gap candidate (open thread pit_value_gap_incremental_edge).

Frozen before this script was written (research_insights.yaml, commit 13d97df):
  signal   pit_norating_lag1 (point-in-time conversion value, every bond priced
           as AA, rating floor off, yesterday's signal traded at today's close)
  params   min_gap_pct 0, sell_gap_pct 0, switch_hurdle_pct 0.03,
           max_hold_days 180, stop_gap_ratio_floor 0
  holdout  first trading day after 2026-05-08 through the warehouse end

Controls use the same engine, parameters, costs, universe and delay. Each day
the model's signal values are kept as a set and handed out to bonds in a
different order: lowest conversion premium first (low_premium), lowest
price + premium first (double_low), or at random (random_<seed>). Thresholds
and hurdles therefore behave exactly as for the model; only which bond gets
which value differs.

Falsifiers, fixed in advance:
  1. holdout excess of the model vs the project CB index <= 0
  2. the model does not beat both style controls, pooled and in >= 5 of 8 years

Also reported, added after the first run showed the project index is a mean
price level and not a return: beta and alpha against the equal-weight return of
all listed CBs, and the average number of open positions.

The random controls are not a fair null: a signal with no day-to-day
persistence makes the engine switch constantly and pay costs each time.

Scope: local diagnostic only; no VM/spot; no strategy or truth change.

跑法 (~10 min):
    python scripts/evaluate_cb_arb_pit_holdout.py
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
FROZEN_PARAMS = {
    "min_gap_pct": 0.0, "sell_gap_pct": 0.0, "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0, "stop_gap_ratio_floor": 0.0,
}
PREVIOUS_END = "20260508"
SPLITS = {"train": ("20190101", "20241231"), "test": ("20250101", "20260508")}
SIGNAL_COLUMNS = [
    "theoretical", "bond_floor", "option_value", "intrinsic", "deviation",
    "rank", "n_ranked", "rank_pct", "value_gap_amount", "value_gap_pct_of_cash",
]
COST_ARGS = argparse.Namespace(
    cost_model_enabled=True, slippage_pct=0.0015, market_impact_coeff=0.0010,
    market_impact_cap_pct=0.02, holding_cost_pct=0.0,
)
_SIGNALS: dict[str, pd.DataFrame] = {}
_CTX: dict[str, Any] = {}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=Path("data/cb_arb_concurrent_supervised_20260511_094500"))
    p.add_argument("--pit-path", type=Path, default=WAREHOUSE / "cb_conv_value_pit.parquet")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--random-seeds", type=int, default=20)
    p.add_argument("--workers", type=int, default=9)
    p.add_argument("--reuse-ranks", action="store_true")
    p.add_argument("--output-dir", type=Path, default=Path("data/cb_arb_pit_holdout_2026-10-07"))
    return p.parse_args()


def _ranks_path(output_dir: Path) -> Path:
    return output_dir / "daily_value_gap_amounts_pit_norating.parquet"


def _build_ranks(ctx: dict[str, Any]) -> None:
    """Daily ranks with no rating information. Runs in its own process, so the patches never reach the parent.

    cb_basic.rating is the latest rating, not the rating on the day: price every bond as AA and switch the
    rating floor off. Conversion price and maturity are already point-in-time in the valuation code itself.
    """
    original = repair_times.price_cb

    def price_cb_no_rating(spec, valuation_date, stock_price, **kwargs):
        return original(spec=replace(spec, rating="AA"), valuation_date=valuation_date, stock_price=stock_price, **kwargs)

    load_basic = repair_times._load_cb_basic

    def load_basic_no_rating() -> pd.DataFrame:
        df = load_basic().copy()
        df["rating"] = "AA"
        df["rating_int"] = int(df["rating_int"].max())
        return df

    repair_times.price_cb = price_cb_no_rating
    repair_times._load_cb_basic = load_basic_no_rating
    value_gap._load_or_build_value_ranks(
        ctx["data_root"], ctx["start"], ctx["end"], ctx["fixed_source"], ctx["rule"],
        _ranks_path(ctx["output_dir"]), ctx["reuse_ranks"],
    )


def _lag_signal(ranks: pd.DataFrame) -> pd.DataFrame:
    """Act on yesterday's signal at today's close."""
    out = ranks.sort_values(["ts_code", "trade_date"]).copy()
    out[SIGNAL_COLUMNS] = out.groupby("ts_code")[SIGNAL_COLUMNS].shift(1)
    out = out.dropna(subset=["value_gap_amount"])
    out["rank"] = out.groupby("trade_date")["deviation"].rank(method="first").astype(int) - 1
    out["n_ranked"] = out.groupby("trade_date")["ts_code"].transform("size")
    out["rank_pct"] = out["rank"] / out["n_ranked"]
    return out.sort_values(["trade_date", "rank"]).reset_index(drop=True)


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


def _reassign(ranks: pd.DataFrame, order_key: pd.Series) -> pd.DataFrame:
    """Give the k-th best signal bundle of each day to the bond ranked k-th by order_key (ascending)."""
    cols = SIGNAL_COLUMNS
    out = ranks.copy()
    out["_key"] = order_key.values
    donor = out.sort_values(["trade_date", "value_gap_amount"], ascending=[True, False], kind="stable")
    taker = out.sort_values(["trade_date", "_key"], ascending=[True, True], kind="stable", na_position="last")
    out.loc[taker.index, cols] = donor[cols].to_numpy()
    return out.drop(columns="_key")


def _build_signals(ranks: pd.DataFrame, pit: pd.DataFrame, seeds: int) -> dict[str, pd.DataFrame]:
    premium = ranks.merge(pit[["ts_code", "trade_date", "conv_prem"]], on=["ts_code", "trade_date"], how="left")["conv_prem"]
    premium.index = ranks.index
    signals = {
        "model": ranks,
        "low_premium": _reassign(ranks, premium),
        "double_low": _reassign(ranks, ranks["close"] + premium),
    }
    for seed in range(seeds):
        rng = np.random.default_rng(seed)
        signals[f"random_{seed:02d}"] = _reassign(ranks, pd.Series(rng.random(len(ranks)), index=ranks.index))
    return {name: _lag_signal(df) for name, df in signals.items()}


def _run(task: tuple[str, str]) -> dict[str, Any]:
    name, split = task
    start, end = _CTX["splits"][split]
    ranks = _SIGNALS[name]
    out = value_gap._run_value_gap_backtest(
        ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
        start, end, _CTX["data_root"], _CTX["fixed_source"], _CTX["rule"],
        value_gap._with_cost_params(FROZEN_PARAMS, COST_ARGS),
    )
    return {"signal": name, "split": split, "metrics": out["metrics"], "equity_curve": out["equity_curve"],
            "trades": [(t["entry_date"], t["exit_date"]) for t in out["trades"]]}


def _equal_weight_return() -> pd.Series:
    """Equal-weight daily return of every listed CB.

    The project index (mean close of all listed bonds) is a price level, not a return: new issues enter
    near par and called bonds leave near their highs, so it sags. 2019-2024 it gains 27% while this
    series compounds to 99%.
    """
    cb = pd.read_parquet(WAREHOUSE / "cb_daily.parquet", columns=["ts_code", "trade_date", "close"])
    cb = cb.sort_values(["ts_code", "trade_date"])
    cb["ret"] = cb.groupby("ts_code")["close"].pct_change()
    return cb[cb["ret"].abs() < 0.6].groupby("trade_date")["ret"].mean()


def _beta_alpha(strategy: pd.Series, market: pd.Series) -> tuple[float, float, float]:
    d = pd.DataFrame({"y": strategy, "m": market}).dropna()
    x = np.column_stack([np.ones(len(d)), d["m"].values])
    coef, *_ = np.linalg.lstsq(x, d["y"].values, rcond=None)
    resid = d["y"].values - x @ coef
    se = np.sqrt(np.diag(np.linalg.inv(x.T @ x)) * resid.var(ddof=2))
    return float(coef[1]), float(coef[0] * 252), float(coef[0] / se[0])


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cb_days = sorted(pd.read_parquet(WAREHOUSE / "cb_daily.parquet", columns=["trade_date"])["trade_date"].unique())
    end = cb_days[-1]
    holdout_start = next(d for d in cb_days if d > PREVIOUS_END)
    splits = {**SPLITS, "holdout": (holdout_start, end)}
    ctx = {
        "data_root": args.data_root, "pit_path": args.pit_path, "fixed_source": args.fixed_source,
        "rule": args.rule, "output_dir": args.output_dir, "reuse_ranks": args.reuse_ranks,
        "start": splits["train"][0], "end": end, "splits": splits,
    }
    _CTX.update(ctx)
    fork = get_context("fork")
    with fork.Pool(1, maxtasksperchild=1) as pool:
        pool.map(_build_ranks, [ctx])
    ranks = pd.read_parquet(_ranks_path(args.output_dir))
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    ranks["ts_code"] = ranks["ts_code"].astype(str)
    ranks = ranks.reset_index(drop=True)
    pit = pd.read_parquet(args.pit_path)
    _SIGNALS.update(_build_signals(ranks, pit, args.random_seeds))
    print(f"[holdout] signals ready: {len(_SIGNALS)}; holdout {holdout_start}..{end}", flush=True)

    with fork.Pool(args.workers) as pool:
        runs = pool.map(_run, [(name, split) for name in _SIGNALS for split in splits])

    index = value_gap._load_cb_daily().groupby("trade_date")["close"].mean().sort_index()
    index.index = index.index.astype(str)
    market = _equal_weight_return()

    split_rows, yearly_rows = [], []
    for r in runs:
        start, stop = splits[r["split"]]
        eq = pd.Series(dict(r["equity_curve"])).sort_index()
        ret = eq.pct_change().dropna()
        market_return = float((1.0 + market.reindex(ret.index).fillna(0.0)).prod() - 1.0)
        beta, alpha, alpha_t = _beta_alpha(ret, market)
        open_positions = [sum(a <= d < b for a, b in r["trades"]) for d in eq.index]
        split_rows.append({
            "signal": r["signal"], "split": r["split"], "start": start, "end": stop, "days": len(eq),
            "trades": len(r["trades"]),
            "avg_positions": float(np.mean(open_positions)),
            "total_return": r["metrics"]["total_return"],
            "excess_vs_index": r["metrics"]["excess_return"],
            "equal_weight_return": market_return,
            "excess_vs_equal_weight": float(eq.iloc[-1] / eq.iloc[0] - 1.0 - market_return),
            "beta_to_equal_weight": beta,
            "alpha_annual": alpha,
            "alpha_t": alpha_t,
            "max_drawdown": r["metrics"]["max_drawdown"],
            "sharpe": float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0.0,
        })
        yearly = _yearly(r["equity_curve"], index)
        for year, row in yearly.iterrows():
            yearly_rows.append({"signal": r["signal"], "split": r["split"], "year": year,
                                "return": round(float(row["return"]), 6), "excess": round(float(row["excess"]), 6)})
    split_df = pd.DataFrame(split_rows)
    yearly_df = pd.DataFrame(yearly_rows)

    # One number per calendar year per signal: 2026 is split between test and holdout, so compound the two.
    def per_year(df: pd.DataFrame) -> pd.Series:
        return df.groupby("year")["return"].apply(lambda s: float(np.prod(1.0 + s) - 1.0))

    year_table = pd.DataFrame({name: per_year(g) for name, g in yearly_df.groupby("signal")})
    random_cols = [c for c in year_table.columns if c.startswith("random_")]
    compare = pd.DataFrame({
        "model": year_table["model"],
        "low_premium": year_table["low_premium"],
        "double_low": year_table["double_low"],
        "random_median": year_table[random_cols].median(axis=1),
        "random_max": year_table[random_cols].max(axis=1),
    })
    compare["model_beats_both_styles"] = (compare["model"] > compare["low_premium"]) & (compare["model"] > compare["double_low"])

    def pooled(col: str) -> float:
        return float(np.prod(1.0 + compare[col]) - 1.0)

    hold = split_df[split_df["split"] == "holdout"].set_index("signal")
    random_hold = hold.loc[random_cols, "total_return"]
    styles = split_df[split_df["signal"].isin(["model", "low_premium", "double_low"])]
    verdict = {
        "alpha_vs_equal_weight_return": {
            f"{row.signal}|{row.split}": {"beta": round(row.beta_to_equal_weight, 3), "alpha_annual": round(row.alpha_annual, 4),
                                          "alpha_t": round(row.alpha_t, 2), "total_return": round(row.total_return, 4),
                                          "equal_weight_return": round(row.equal_weight_return, 4)}
            for row in styles.itertuples()
        },
        "holdout": {
            "window": list(splits["holdout"]),
            "model_total_return": float(hold.loc["model", "total_return"]),
            "model_excess_vs_index": float(hold.loc["model", "excess_vs_index"]),
            "equal_weight_return": float(hold.loc["model", "equal_weight_return"]),
            "model_excess_vs_equal_weight": float(hold.loc["model", "excess_vs_equal_weight"]),
            "model_beta_to_equal_weight": float(hold.loc["model", "beta_to_equal_weight"]),
            "model_alpha_annual": float(hold.loc["model", "alpha_annual"]),
            "model_alpha_t": float(hold.loc["model", "alpha_t"]),
            "model_avg_positions": float(hold.loc["model", "avg_positions"]),
            "low_premium_avg_positions": float(hold.loc["low_premium", "avg_positions"]),
            "double_low_avg_positions": float(hold.loc["double_low", "avg_positions"]),
            "low_premium_total_return": float(hold.loc["low_premium", "total_return"]),
            "double_low_total_return": float(hold.loc["double_low", "total_return"]),
            "random_total_return_median": float(random_hold.median()),
            "random_total_return_max": float(random_hold.max()),
            "model_rank_among_random": int((random_hold >= hold.loc["model", "total_return"]).sum()) + 1,
            "falsifier_1_excess_le_0": bool(hold.loc["model", "excess_vs_index"] <= 0),
        },
        "style_control": {
            "pooled_return_2019_to_end": {c: pooled(c) for c in ("model", "low_premium", "double_low", "random_median")},
            "years_model_beats_both_styles": int(compare["model_beats_both_styles"].sum()),
            "years_total": int(len(compare)),
            "falsifier_2_fails_to_beat_styles": bool(
                not (pooled("model") > pooled("low_premium") and pooled("model") > pooled("double_low")
                     and compare["model_beats_both_styles"].sum() >= 5)
            ),
        },
    }

    split_df.round(6).to_csv(args.output_dir / "split_results.csv", index=False)
    compare.round(6).to_csv(args.output_dir / "yearly_return_model_vs_controls.csv")
    summary = {
        "run_id": args.output_dir.name,
        "scope": "local diagnostic only; no VM/spot; no strategy or truth change",
        "frozen_params": FROZEN_PARAMS, "signal": "pit_norating_lag1", "splits": splits,
        "random_seeds": args.random_seeds, "verdict": verdict,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=float) + "\n", encoding="utf-8")
    falsified = verdict["holdout"]["falsifier_1_excess_le_0"] or verdict["style_control"]["falsifier_2_fails_to_beat_styles"]
    report = {
        "schema_version": 1,
        "run_id": args.output_dir.name,
        "date": date.today().isoformat(),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": "reject" if falsified else "mini-spec-retry",
        "three_exits_section": {
            "adoption_pass": False,
            "review_summary": "Pre-registered holdout and matched style controls for the frozen point-in-time candidate.",
            "review_main_reason": "falsifier fired" if falsified else "no falsifier fired",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            ["point-in-time value-gap ranking as an edge over low-premium / double-low rotation"] if falsified else []
        ),
        "learnings": [json.dumps(verdict, ensure_ascii=False, default=float)],
        "follow_up_actions": ["Evidence-only record; do not promote to truth without user approval."],
        "status": "COMPLETE",
        "notes": "Local diagnostic only; no VM/spot; no strategy or truth change.",
        "references": ["data/research_framework/research_insights.yaml#pit_value_gap_incremental_edge"],
        "related_reports": ["data/cb_arb_conv_price_lookahead_2026-10-07/report.yaml"],
    }
    (args.output_dir / "report.yaml").write_text(yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8")

    show = split_df[~split_df["signal"].str.startswith("random_")]
    print(show.round(4).to_string(index=False))
    print(compare.round(4).to_string())
    print(json.dumps(verdict, ensure_ascii=False, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
