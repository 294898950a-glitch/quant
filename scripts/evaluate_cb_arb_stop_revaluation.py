"""Evaluate stop-loss revaluation with conservative valuation parameters.

Buy ranking remains the normal absolute value-gap ranking. When a holding
hits the price stop, the remaining value gap is recomputed with a separate
conservative valuation cache before deciding whether to sell or switch.
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
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}

REVALUE_CONFIGS: list[dict[str, Any]] = [
    {
        "name": "normal_revalue",
        "description": "止损复查仍用原估值",
        "overrides": {},
    },
    {
        "name": "vol_70",
        "description": "止损复查时波动价值按 70% 估",
        "overrides": {"vol_multiplier_factor": 0.70},
    },
    {
        "name": "vol_50",
        "description": "止损复查时波动价值按 50% 估",
        "overrides": {"vol_multiplier_factor": 0.50},
    },
    {
        "name": "spread_plus_100",
        "description": "止损复查时信用折价增加 100bp",
        "overrides": {"credit_spread_add_bp": 100.0},
    },
    {
        "name": "spread_plus_200",
        "description": "止损复查时信用折价增加 200bp",
        "overrides": {"credit_spread_add_bp": 200.0},
    },
    {
        "name": "vol_70_spread_100",
        "description": "止损复查时波动价值 70% 且信用折价增加 100bp",
        "overrides": {"vol_multiplier_factor": 0.70, "credit_spread_add_bp": 100.0},
    },
    {
        "name": "vol_50_spread_200",
        "description": "止损复查时波动价值 50% 且信用折价增加 200bp",
        "overrides": {"vol_multiplier_factor": 0.50, "credit_spread_add_bp": 200.0},
    },
]


def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    overrides: dict[str, float],
    result: dict[str, Any],
) -> dict[str, Any]:
    row = {
        "name": name,
        "description": description,
        "period": period,
        "start": start,
        "end": end,
        "overrides_json": json.dumps(overrides, sort_keys=True),
        **BASE_PARAMS,
        **result["metrics"],
    }
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
    output_dir = args.output_dir or args.data_root / "value_gap_stop_revaluation"
    output_dir.mkdir(parents=True, exist_ok=True)
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)

    buy_ranks = _load_or_build_value_ranks(
        args.data_root,
        start_all,
        end_all,
        args.fixed_source,
        args.rule,
        output_dir / "daily_value_gap_amounts_buy.parquet",
        args.reuse_ranks,
    )

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    exit_2020_rows: list[dict[str, Any]] = []

    for cfg in REVALUE_CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        overrides = dict(cfg["overrides"])
        if overrides:
            stop_ranks = _load_or_build_value_ranks(
                args.data_root,
                start_all,
                end_all,
                args.fixed_source,
                args.rule,
                output_dir / f"daily_value_gap_amounts_stop_{name}.parquet",
                args.reuse_ranks,
                config_overrides=overrides,
            )
        else:
            stop_ranks = buy_ranks

        train = _run_value_gap_backtest(
            buy_ranks[(buy_ranks["trade_date"] >= args.train_start) & (buy_ranks["trade_date"] <= args.train_end)],
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            BASE_PARAMS,
            stop_revalue_ranks=stop_ranks[
                (stop_ranks["trade_date"] >= args.train_start)
                & (stop_ranks["trade_date"] <= args.train_end)
            ],
        )
        test = _run_value_gap_backtest(
            buy_ranks[(buy_ranks["trade_date"] >= args.test_start) & (buy_ranks["trade_date"] <= args.test_end)],
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            BASE_PARAMS,
            stop_revalue_ranks=stop_ranks[
                (stop_ranks["trade_date"] >= args.test_start)
                & (stop_ranks["trade_date"] <= args.test_end)
            ],
        )
        summary_rows.append(
            _row(name, description, "train", args.train_start, args.train_end, overrides, train)
        )
        summary_rows.append(
            _row(name, description, "test", args.test_start, args.test_end, overrides, test)
        )
        print(
            f"[stop_revalue] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']}",
            flush=True,
        )

        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            y = _run_value_gap_backtest(
                buy_ranks[(buy_ranks["trade_date"] >= start) & (buy_ranks["trade_date"] <= end)],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                BASE_PARAMS,
                stop_revalue_ranks=stop_ranks[
                    (stop_ranks["trade_date"] >= start) & (stop_ranks["trade_date"] <= end)
                ],
            )
            yearly_rows.append(_row(name, description, str(year), start, end, overrides, y))
            if year == 2020:
                exit_2020_rows.extend(_exit_rows(name, y["trades"]))

    summary_rows.sort(key=lambda r: (r["period"], -float(r["score"])))
    yearly_rows.sort(key=lambda r: (r["name"], r["period"]))
    _write_csv(output_dir / "summary_stop_revaluation.csv", summary_rows)
    _write_csv(output_dir / "yearly_stop_revaluation.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_stop_revaluation.csv", exit_2020_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "base_params": BASE_PARAMS,
                "configs": REVALUE_CONFIGS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[stop_revalue] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
