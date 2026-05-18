"""Evaluate permanent universe filters for cb_arb value-gap switch."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _gap_source_shares,
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
    _score,
    _with_cost_params,
    _write_csv,
)


BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}


CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_full_universe",
        "description": "Full current universe control.",
        "filters": [],
    },
    {
        "name": "exclude_far_above_bond_floor",
        "description": "Exclude CBs whose train-window close/bond_floor ever exceeds 1.5.",
        "filters": ["far_above_bond_floor"],
    },
    {
        "name": "exclude_high_moneyness",
        "description": "Exclude CBs whose train-window stock/conv_price ever exceeds 1.6.",
        "filters": ["high_moneyness"],
    },
    {
        "name": "exclude_short_maturity",
        "description": "Exclude CBs whose train-window remaining maturity ever falls below 1 year.",
        "filters": ["short_maturity"],
    },
    {
        "name": "exclude_small_size",
        "description": "Exclude CBs with issue_size below 10 yi.",
        "filters": ["small_size"],
    },
    {
        "name": "combined_lowrisk_universe",
        "description": "Exclude far-above-bond-floor, high-moneyness, or short-maturity CBs.",
        "filters": ["far_above_bond_floor", "high_moneyness", "short_maturity"],
    },
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20221231")
    p.add_argument("--validate-start", default="20230101")
    p.add_argument("--validate-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--reuse-ranks", action="store_true")
    p.add_argument("--base-ranks-path", type=Path, default=None)
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    return p.parse_args()


def _load_base_ranks(args: argparse.Namespace, output_dir: Path) -> pd.DataFrame:
    start_all = min(args.train_start, args.validate_start, args.test_start)
    end_all = max(args.train_end, args.validate_end, args.test_end)
    if args.base_ranks_path is not None and args.base_ranks_path.exists():
        ranks = pd.read_parquet(args.base_ranks_path)
    else:
        ranks = _load_or_build_value_ranks(
            args.data_root,
            start_all,
            end_all,
            args.fixed_source,
            args.rule,
            output_dir / "daily_value_gap_amounts.parquet",
            args.reuse_ranks,
        )
    ranks = ranks.copy()
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    ranks["ts_code"] = ranks["ts_code"].astype(str)
    ranks = ranks[(ranks["trade_date"] >= start_all) & (ranks["trade_date"] <= end_all)]
    return _add_universe_attributes(ranks)


def _add_universe_attributes(ranks: pd.DataFrame) -> pd.DataFrame:
    basic = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_basic.parquet")
    basic = basic[
        ["ts_code", "stk_code", "conv_price", "issue_size", "maturity_date"]
    ].copy()
    basic["ts_code"] = basic["ts_code"].astype(str)
    basic["stk_code"] = basic["stk_code"].astype(str)
    basic["conv_price"] = pd.to_numeric(basic["conv_price"], errors="coerce")
    basic["issue_size"] = pd.to_numeric(basic["issue_size"], errors="coerce")
    basic["maturity_date"] = basic["maturity_date"].astype(str)

    stock = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/stk_daily_qfq.parquet")
    stock = stock[["trade_date", "stk_code", "close"]].copy()
    stock["trade_date"] = stock["trade_date"].astype(str)
    stock["stk_code"] = stock["stk_code"].astype(str)
    stock = stock.rename(columns={"close": "stock_close"})
    stock["stock_close"] = pd.to_numeric(stock["stock_close"], errors="coerce")

    out = ranks.merge(basic, on="ts_code", how="left").merge(
        stock,
        on=["trade_date", "stk_code"],
        how="left",
    )
    out["close_to_bond_floor"] = out["close"].astype(float) / out["bond_floor"].where(
        out["bond_floor"].astype(float) > 0,
        pd.NA,
    ).astype(float)
    out["moneyness_stock_to_conv"] = out["stock_close"] / out["conv_price"]
    trade_dates = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
    maturity_dates = pd.to_datetime(out["maturity_date"], format="%Y%m%d", errors="coerce")
    out["remaining_maturity_years"] = (maturity_dates - trade_dates).dt.days / 365.25
    return out


def _exclusion_sets(ranks: pd.DataFrame, train_start: str, train_end: str) -> dict[str, set[str]]:
    train = ranks[(ranks["trade_date"] >= train_start) & (ranks["trade_date"] <= train_end)].copy()
    sets: dict[str, set[str]] = {}
    sets["far_above_bond_floor"] = set(
        train.loc[train["close_to_bond_floor"].astype(float) > 1.5, "ts_code"].astype(str)
    )
    sets["high_moneyness"] = set(
        train.loc[train["moneyness_stock_to_conv"].astype(float) > 1.6, "ts_code"].astype(str)
    )
    sets["short_maturity"] = set(
        train.loc[train["remaining_maturity_years"].astype(float) < 1.0, "ts_code"].astype(str)
    )
    sets["small_size"] = set(
        ranks.loc[pd.to_numeric(ranks["issue_size"], errors="coerce") < 10.0, "ts_code"].astype(str)
    )
    return sets


def _excluded_codes(cfg: dict[str, Any], sets: dict[str, set[str]]) -> set[str]:
    excluded: set[str] = set()
    for name in cfg.get("filters", []):
        excluded.update(sets.get(str(name), set()))
    return excluded


def _filter_ranks(ranks: pd.DataFrame, excluded: set[str]) -> pd.DataFrame:
    if not excluded:
        return ranks.copy()
    return ranks[~ranks["ts_code"].astype(str).isin(excluded)].copy()


def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    params: dict[str, Any],
    result: dict[str, Any],
    excluded_count: int,
) -> dict[str, Any]:
    row = {
        "name": name,
        "description": description,
        "period": period,
        "start": start,
        "end": end,
        "excluded_count": excluded_count,
        "params_json": json.dumps(params, sort_keys=True),
        **result["metrics"],
    }
    row["score"] = _score(result["metrics"])
    return row


def _source_rows(name: str, period: str, result: dict[str, Any], ranks_by_key: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in result["trades"]:
        key = (str(trade["entry_date"]), str(trade["cb_code"]))
        rank_row = ranks_by_key.get(key)
        source = "missing"
        bond_share = 0.0
        option_share = 0.0
        close_to_bond_floor = None
        moneyness = None
        maturity_years = None
        issue_size = None
        if rank_row is not None:
            source, bond_share, option_share = _gap_source_shares(rank_row, {})
            close_to_bond_floor = getattr(rank_row, "close_to_bond_floor", None)
            moneyness = getattr(rank_row, "moneyness_stock_to_conv", None)
            maturity_years = getattr(rank_row, "remaining_maturity_years", None)
            issue_size = getattr(rank_row, "issue_size", None)
        grouped.setdefault(source, []).append(
            {
                **trade,
                "source": source,
                "bond_share": bond_share,
                "option_share": option_share,
                "close_to_bond_floor": close_to_bond_floor,
                "moneyness_stock_to_conv": moneyness,
                "remaining_maturity_years": maturity_years,
                "issue_size": issue_size,
            }
        )

    rows: list[dict[str, Any]] = []
    for source, trades in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in trades]
        pnl_amount = [float(t["pnl_amount"]) for t in trades]
        rows.append(
            {
                "name": name,
                "period": period,
                "source": source,
                "count": len(trades),
                "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 6),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "wins": sum(1 for v in pnl_pct if v > 0),
                "avg_option_share": _avg([t["option_share"] for t in trades]),
                "avg_close_to_bond_floor": _avg([t["close_to_bond_floor"] for t in trades]),
                "avg_moneyness_stock_to_conv": _avg([t["moneyness_stock_to_conv"] for t in trades]),
                "avg_remaining_maturity_years": _avg([t["remaining_maturity_years"] for t in trades]),
                "avg_issue_size": _avg([t["issue_size"] for t in trades]),
            }
        )
    return rows


def _avg(values: list[Any]) -> float | None:
    parsed = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if parsed.empty:
        return None
    return round(float(parsed.mean()), 6)


def _metric(rows: list[dict[str, Any]], name: str, period: str, field: str) -> float:
    row = next(r for r in rows if r["name"] == name and r["period"] == period)
    return float(row[field])


def _adoption_summary(summary_rows: list[dict[str, Any]], yearly_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline = "baseline_full_universe"
    base_train = _metric(summary_rows, baseline, "train", "excess_return")
    base_validate = _metric(summary_rows, baseline, "validate", "excess_return")
    base_test = _metric(summary_rows, baseline, "test", "excess_return")
    base_2020_dd = _metric(yearly_rows, baseline, "2020", "max_drawdown")
    out: list[dict[str, Any]] = []
    for cfg in CONFIGS:
        name = str(cfg["name"])
        train_delta = _metric(summary_rows, name, "train", "excess_return") - base_train
        validate_delta = _metric(summary_rows, name, "validate", "excess_return") - base_validate
        test_delta = _metric(summary_rows, name, "test", "excess_return") - base_test
        dd_2020_delta = _metric(yearly_rows, name, "2020", "max_drawdown") - base_2020_dd
        year_dd = [
            float(r["max_drawdown"])
            for r in yearly_rows
            if r["name"] == name and str(r["period"]).isdigit() and int(r["period"]) <= 2024
        ]
        worst_dd = min(year_dd) if year_dd else 0.0
        adoption_pass = (
            name != baseline
            and train_delta >= 0.005
            and validate_delta >= 0.0
            and dd_2020_delta >= 0.05
            and worst_dd >= -0.15
        )
        out.append(
            {
                "name": name,
                "adoption_pass": adoption_pass,
                "adoption_pass_numeric": int(adoption_pass),
                "train_excess_delta_vs_baseline": round(train_delta, 6),
                "validate_excess_delta_vs_baseline": round(validate_delta, 6),
                "test_excess_delta_vs_baseline": round(test_delta, 6),
                "max_drawdown_2020_delta_vs_baseline": round(dd_2020_delta, 6),
                "worst_yearly_max_drawdown_2019_2024": round(worst_dd, 6),
                "score": round(train_delta + validate_delta + dd_2020_delta + min(test_delta, 0.0), 6),
            }
        )
    out.sort(key=lambda r: (int(r["adoption_pass_numeric"]), float(r["score"])), reverse=True)
    return out


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_universe_filter"
    output_dir.mkdir(parents=True, exist_ok=True)

    base_ranks = _load_base_ranks(args, output_dir)
    base_ranks.to_parquet(output_dir / "daily_value_gap_amounts_with_universe_attrs.parquet", index=False)
    exclusion_sets = _exclusion_sets(base_ranks, args.train_start, args.train_end)
    params = _with_cost_params(dict(BASE_PARAMS), args)

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    entry_source_rows: list[dict[str, Any]] = []
    universe_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        excluded = _excluded_codes(cfg, exclusion_sets)
        ranks = _filter_ranks(base_ranks, excluded)
        ranks.to_parquet(output_dir / f"daily_value_gap_amounts_{name}.parquet", index=False)
        ranks_by_key = {
            (str(r.trade_date), str(r.ts_code)): r
            for r in ranks.itertuples(index=False)
        }
        universe_rows.append(
            {
                "name": name,
                "filters_json": json.dumps(cfg.get("filters", []), sort_keys=True),
                "excluded_count": len(excluded),
                "remaining_codes": int(ranks["ts_code"].nunique()),
                "excluded_codes_json": json.dumps(sorted(excluded), ensure_ascii=False),
            }
        )

        for period, start, end in (
            ("train", args.train_start, args.train_end),
            ("validate", args.validate_start, args.validate_end),
            ("test", args.test_start, args.test_end),
        ):
            result = _run_value_gap_backtest(
                ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            summary_rows.append(_row(name, description, period, start, end, params, result, len(excluded)))
            entry_source_rows.extend(_source_rows(name, period, result, ranks_by_key))
            print(
                f"[universe_filter] {name} {period} "
                f"excess={result['metrics']['excess_return']} "
                f"dd={result['metrics']['max_drawdown']} "
                f"excluded={len(excluded)}",
                flush=True,
            )

        for year in range(2019, 2027):
            start = f"{year}0101"
            end = min(f"{year}1231", args.test_end)
            if start > args.test_end:
                continue
            result = _run_value_gap_backtest(
                ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            yearly_rows.append(_row(name, description, str(year), start, end, params, result, len(excluded)))
            if year == 2020:
                entry_source_rows.extend(_source_rows(name, "2020", result, ranks_by_key))

    adoption_rows = _adoption_summary(summary_rows, yearly_rows)
    selected = adoption_rows[0] if adoption_rows else {}
    _write_csv(output_dir / "summary_universe_filter.csv", summary_rows)
    _write_csv(output_dir / "yearly_universe_filter.csv", yearly_rows)
    _write_csv(output_dir / "entry_source_universe_filter.csv", entry_source_rows)
    _write_csv(output_dir / "universe_filter_membership.csv", universe_rows)
    _write_csv(output_dir / "adoption_universe_filter.csv", adoption_rows)

    summary = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "candidate_count": len(CONFIGS),
        "selected_name": selected.get("name"),
        "adoption_pass": bool(selected.get("adoption_pass", False)),
        "selected_table_row": selected,
        "train_start": args.train_start,
        "train_end": args.train_end,
        "validate_start": args.validate_start,
        "validate_end": args.validate_end,
        "test_start": args.test_start,
        "test_end": args.test_end,
        "artifacts": [
            "summary_universe_filter.csv",
            "yearly_universe_filter.csv",
            "entry_source_universe_filter.csv",
            "universe_filter_membership.csv",
            "adoption_universe_filter.csv",
            "daily_value_gap_amounts_with_universe_attrs.parquet",
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": "2026-05-17",
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": "pending_manual_review",
        "status": "COMPLETE",
        "summary": "Universe-filter retest completed; no truth promotion is implied.",
        "selected_name": summary["selected_name"],
        "adoption_pass": summary["adoption_pass"],
        "references": summary["artifacts"],
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(
            {
                **report,
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "exclusion_set_counts": {k: len(v) for k, v in exclusion_sets.items()},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "status": "COMPLETE",
                "ack": "mechanical artifacts written for L4/L6 review",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    print(f"[universe_filter] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
