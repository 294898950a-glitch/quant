"""Run cb_arb phase 1.5 cost-realism checks for agreed baselines."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
)
from strategies.cb_arb.verifier import (  # noqa: E402
    _index_total_return,
    _load_trading_days,
    run_backtest,
)


YEARS = [2019, 2020, 2021, 2022, 2023, 2024]
DEFAULT_OUTPUT = _REPO_ROOT / "data" / "cb_arb_cost_realism_test_2026-05-16"
DEFAULT_VALUE_ROOT = _REPO_ROOT / "data" / "cb_arb_concurrent_supervised_20260511_094500"
DEFAULT_HDRF_SUMMARY = _REPO_ROOT / "data" / "cb_arb_two_line_cross_validation_2026-05-15" / "summary.json"


def _load_yaml_current(path: Path) -> tuple[list[float], dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    weights = [float(row.get("current")) for row in data.get("parameters", [])]
    rules = {str(row.get("name")): row.get("current") for row in data.get("rules", [])}
    return weights, rules


def _date_pool(year: int) -> set[str]:
    start = f"{year}0101"
    end = f"{year}1231"
    return {d for d in _load_trading_days() if start <= d <= end}


def _pool_bounds(pool: set[str]) -> tuple[str, str]:
    if not pool:
        return "", ""
    return min(pool), max(pool)


def _row(strategy: str, period: str, start: str, end: str, metrics: dict[str, Any]) -> dict[str, Any]:
    benchmark = _index_total_return(start, end) if start and end else 0.0
    return {
        "strategy": strategy,
        "period": period,
        "start": start,
        "end": end,
        "excess_return": metrics.get("excess_return", 0.0),
        "total_return": metrics.get("total_return", 0.0),
        "benchmark_total_return": round(benchmark, 6),
        "sharpe": metrics.get("sharpe", ""),
        "max_drawdown": metrics.get("max_drawdown", 0.0),
        "total_trades": metrics.get("total_trades", 0),
        "win_rate": metrics.get("win_rate", 0.0),
        "n_days": metrics.get("n_days", 0),
    }


def _compound(rows: list[dict[str, Any]]) -> float:
    value = 1.0
    for row in rows:
        value *= 1.0 + float(row.get("excess_return") or 0.0)
    return round(value - 1.0, 6)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _main_rows(weights: list[float], rules: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    all_pool: set[str] = set()
    for year in YEARS:
        pool = _date_pool(year)
        all_pool.update(pool)
        start, end = _pool_bounds(pool)
        result = run_backtest(weights, {}, rules, oos_event_ids=pool)
        rows.append(_row("main_yaml_current", str(year), start, end, result.oos_metrics))
        print(f"[cost-realism] main year={year} metrics={result.oos_metrics}", flush=True)
    start, end = _pool_bounds(all_pool)
    full = run_backtest(weights, {}, rules, oos_event_ids=all_pool)
    full_row = _row("main_yaml_current", "2019-2024", start, end, full.oos_metrics)
    rows.append(full_row)
    return rows, full_row


def _value_gap_rows(args: argparse.Namespace, cost_params: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary = json.loads(args.hdrf_summary.read_text(encoding="utf-8"))
    params = dict(summary["hdrf_params"])
    params.update(cost_params)
    ranks_path = args.output_dir / "value_gap_daily_value_amounts.parquet"
    ranks = _load_or_build_value_ranks(
        args.value_root,
        "20190101",
        "20241231",
        args.fixed_source,
        args.rule,
        ranks_path,
        args.reuse_ranks,
    )
    rows: list[dict[str, Any]] = []
    for year in YEARS:
        start = f"{year}0101"
        end = f"{year}1231"
        result = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
            start,
            end,
            args.value_root,
            args.fixed_source,
            args.rule,
            params,
        )
        actual_start = min(result["equity_curve"])[0] if result["equity_curve"] else ""
        actual_end = max(result["equity_curve"])[0] if result["equity_curve"] else ""
        rows.append(_row("value_gap_medium_signal", str(year), actual_start, actual_end, result["metrics"]))
        print(f"[cost-realism] value_gap year={year} metrics={result['metrics']}", flush=True)
    result = _run_value_gap_backtest(
        ranks,
        "20190101",
        "20241231",
        args.value_root,
        args.fixed_source,
        args.rule,
        params,
    )
    actual_start = min(result["equity_curve"])[0] if result["equity_curve"] else ""
    actual_end = max(result["equity_curve"])[0] if result["equity_curve"] else ""
    full_row = _row("value_gap_medium_signal", "2019-2024", actual_start, actual_end, result["metrics"])
    rows.append(full_row)
    return rows, full_row


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml-path", type=Path, default=_REPO_ROOT / "strategies" / "cb_arb" / "tunable_space.yaml")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--value-root", type=Path, default=DEFAULT_VALUE_ROOT)
    parser.add_argument("--hdrf-summary", type=Path, default=DEFAULT_HDRF_SUMMARY)
    parser.add_argument("--fixed-source", type=int, default=2)
    parser.add_argument("--rule", default="score_4state")
    parser.add_argument("--reuse-ranks", action="store_true")
    parser.add_argument("--slippage-pct", type=float, default=0.0015)
    parser.add_argument("--market-impact-coeff", type=float, default=0.0010)
    parser.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    parser.add_argument("--holding-cost-pct", type=float, default=0.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    weights, rules = _load_yaml_current(args.yaml_path)
    cost_params = {
        "cost_model_enabled": 1.0,
        "slippage_pct": float(args.slippage_pct),
        "market_impact_coeff": float(args.market_impact_coeff),
        "market_impact_cap_pct": float(args.market_impact_cap_pct),
        "holding_cost_pct": float(args.holding_cost_pct),
    }
    rules.update(cost_params)
    rows: list[dict[str, Any]] = []
    main_rows, main_full = _main_rows(weights, rules)
    rows.extend(main_rows)
    value_rows, value_full = _value_gap_rows(args, cost_params)
    rows.extend(value_rows)
    _write_csv(args.output_dir / "holdout_with_cost.csv", rows)
    yearly_main = [r for r in main_rows if r["period"] != "2019-2024"]
    yearly_value = [r for r in value_rows if r["period"] != "2019-2024"]
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cost_model": cost_params,
        "main_yaml_current": {
            "full_span": main_full,
            "compounded_yearly_excess_return": _compound(yearly_main),
            "simple_sum_yearly_excess_return": round(
                sum(float(r["excess_return"]) for r in yearly_main), 6
            ),
        },
        "value_gap_medium_signal": {
            "full_span": value_full,
            "compounded_yearly_excess_return": _compound(yearly_value),
            "simple_sum_yearly_excess_return": round(
                sum(float(r["excess_return"]) for r in yearly_value), 6
            ),
        },
        "outputs": ["holdout_with_cost.csv", "summary.json"],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[cost-realism] summary", json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
