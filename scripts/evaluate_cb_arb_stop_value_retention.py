"""Compare stop-loss handling by retained value gap.

This keeps the absolute value-gap buy logic fixed and only changes what
happens after a position reaches the price stop.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
    _score,
    _write_csv,
)


BASE_PARAMS: dict[str, float] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_signal_threshold": 999.0,
}

PARAM_COLUMNS = [
    "min_gap_pct",
    "sell_gap_pct",
    "switch_hurdle_pct",
    "max_hold_days",
    "stop_signal_threshold",
    "stop_gap_ratio_floor",
    "stop_gap_ratio_floor_weak",
    "stop_gap_ratio_floor_flat_weak",
    "stop_gap_ratio_floor_neutral",
    "stop_gap_ratio_floor_strong",
    "panic_rebound_enabled",
    "panic_drawdown",
    "panic_ret_20d",
    "panic_breadth_20d",
    "panic_amount_pctile",
    "panic_rebound_stop_gap_ratio_floor",
]


def _params(**updates: float) -> dict[str, float]:
    params = {**BASE_PARAMS, "stop_gap_ratio_floor": 0.30}
    params.update(updates)
    return params


STOP_CONFIGS: list[tuple[str, str, dict[str, float]]] = [
    ("price_stop", "跌到止损线就卖", _params(stop_gap_ratio_floor=999.0)),
    ("gap_positive", "低估金额仍为正就不按止损卖", _params(stop_gap_ratio_floor=0.0)),
    ("retain_30", "低估金额保留 30% 以上就不按止损卖", _params(stop_gap_ratio_floor=0.30)),
    ("retain_50", "低估金额保留 50% 以上就不按止损卖", _params(stop_gap_ratio_floor=0.50)),
    ("retain_65", "低估金额保留 65% 以上就不按止损卖", _params(stop_gap_ratio_floor=0.65)),
    (
        "strong_65_else_30",
        "强市保留 65%，其它状态保留 30%",
        _params(stop_gap_ratio_floor=0.30, stop_gap_ratio_floor_strong=0.65),
    ),
    (
        "panic_rebound_65_else_30",
        "大跌后开始修复时保留 65%，其它状态保留 30%",
        _params(
            stop_gap_ratio_floor=0.30,
            panic_rebound_enabled=1.0,
            panic_drawdown=-0.08,
            panic_ret_20d=0.0,
            panic_breadth_20d=0.50,
            panic_amount_pctile=0.50,
            panic_rebound_stop_gap_ratio_floor=0.65,
        ),
    ),
    (
        "deep_rebound_65_else_30",
        "深跌后开始修复时保留 65%，其它状态保留 30%",
        _params(
            stop_gap_ratio_floor=0.30,
            panic_rebound_enabled=1.0,
            panic_drawdown=-0.12,
            panic_ret_20d=0.0,
            panic_breadth_20d=0.50,
            panic_amount_pctile=0.50,
            panic_rebound_stop_gap_ratio_floor=0.65,
        ),
    ),
    (
        "weak_price_strong_65_else_30",
        "弱市按原止损，强市保留 65%，其它状态保留 30%",
        _params(
            stop_gap_ratio_floor=0.30,
            stop_gap_ratio_floor_weak=999.0,
            stop_gap_ratio_floor_strong=0.65,
        ),
    ),
]


def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    result: dict[str, Any],
    params: dict[str, float],
) -> dict[str, Any]:
    row = {
        "name": name,
        "description": description,
        "period": period,
        "start": start,
        "end": end,
        "params_json": json.dumps(params, sort_keys=True),
        **result["metrics"],
    }
    for key in PARAM_COLUMNS:
        row[key] = params.get(key, "")
    row["score"] = _score(result["metrics"])
    return row


def _exit_rows(name: str, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(str(trade["exit_reason"]), []).append(trade)
    rows: list[dict[str, Any]] = []
    for reason, reason_trades in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in reason_trades]
        pnl_amount = [float(t["pnl_amount"]) for t in reason_trades]
        rows.append(
            {
                "name": name,
                "exit_reason": reason,
                "count": len(reason_trades),
                "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 6),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "wins": sum(1 for v in pnl_pct if v > 0),
            }
        )
    return rows


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--reuse-ranks", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_stop_value_retention"
    output_dir.mkdir(parents=True, exist_ok=True)

    ranks_path = output_dir / "daily_value_gap_amounts.parquet"
    ranks = _load_or_build_value_ranks(
        args.data_root,
        min(args.train_start, args.test_start),
        max(args.train_end, args.test_end),
        args.fixed_source,
        args.rule,
        ranks_path,
        args.reuse_ranks,
    )

    rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    exit_rows_2020: list[dict[str, Any]] = []
    trade_rows_2020: list[dict[str, Any]] = []

    for name, description, params in STOP_CONFIGS:
        train = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= args.train_start) & (ranks["trade_date"] <= args.train_end)],
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        test = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= args.test_start) & (ranks["trade_date"] <= args.test_end)],
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        rows.append(_row(name, description, "train", args.train_start, args.train_end, train, params))
        rows.append(_row(name, description, "test", args.test_start, args.test_end, test, params))
        print(
            f"[stop_retention] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']}",
            flush=True,
        )

        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            y = _run_value_gap_backtest(
                ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            yearly_rows.append(_row(name, description, str(year), start, end, y, params))
            if year == 2020:
                exit_rows_2020.extend(_exit_rows(name, y["trades"]))
                for trade in y["trades"]:
                    trade_rows_2020.append({"name": name, **trade})

    rows.sort(key=lambda r: (r["period"], -float(r["score"])))
    yearly_rows.sort(key=lambda r: (r["name"], r["period"]))
    _write_csv(output_dir / "summary_stop_value_retention.csv", rows)
    _write_csv(output_dir / "yearly_stop_value_retention.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_stop_value_retention.csv", exit_rows_2020)
    _write_csv(output_dir / "trades_2020_stop_value_retention.csv", trade_rows_2020)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "configs": [
                    {
                        "name": name,
                        "description": description,
                        "params": params,
                    }
                    for name, description, params in STOP_CONFIGS
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[stop_retention] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
