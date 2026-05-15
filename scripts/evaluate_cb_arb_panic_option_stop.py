"""Evaluate delaying option-type stop-losses during market panic."""

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


BASE: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_signal_threshold": 999.0,
}

CONFIGS: list[dict[str, Any]] = [
    {
        "name": "price_stop",
        "description": "非复查，跌到止损线就卖",
        "params": {"stop_gap_ratio_floor": 999.0},
    },
    {
        "name": "retain_30",
        "description": "止损复查，低估保留30%以上不卖",
        "params": {"stop_gap_ratio_floor": 0.30},
    },
    {
        "name": "panic_combo_delay5",
        "description": "期权型在组合恐慌日延迟5个交易日，非恐慌按价格止损",
        "params": {
            "stop_gap_ratio_floor": 999.0,
            "panic_option_delay_enabled": 1.0,
            "panic_option_delay_days": 5.0,
            "panic_rule": "agent_combo",
        },
    },
    {
        "name": "panic_combo_delay10",
        "description": "期权型在组合恐慌日延迟10个交易日，非恐慌按价格止损",
        "params": {
            "stop_gap_ratio_floor": 999.0,
            "panic_option_delay_enabled": 1.0,
            "panic_option_delay_days": 10.0,
            "panic_rule": "agent_combo",
        },
    },
    {
        "name": "panic_combo_delay20",
        "description": "期权型在组合恐慌日延迟20个交易日，非恐慌按价格止损",
        "params": {
            "stop_gap_ratio_floor": 999.0,
            "panic_option_delay_enabled": 1.0,
            "panic_option_delay_days": 20.0,
            "panic_rule": "agent_combo",
        },
    },
    {
        "name": "breadth40_delay10",
        "description": "期权型在20日上涨比例低于40%时延迟10个交易日",
        "params": {
            "stop_gap_ratio_floor": 999.0,
            "panic_option_delay_enabled": 1.0,
            "panic_option_delay_days": 10.0,
            "panic_rule": "breadth40",
        },
    },
]


def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    params: dict[str, Any],
    result: dict[str, Any],
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
    output_dir = args.output_dir or args.data_root / "value_gap_panic_option_stop"
    output_dir.mkdir(parents=True, exist_ok=True)
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)
    ranks = _load_or_build_value_ranks(
        args.data_root,
        start_all,
        end_all,
        args.fixed_source,
        args.rule,
        output_dir / "daily_value_gap_amounts.parquet",
        args.reuse_ranks,
    )

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    exit_2020_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        params = {**BASE, **dict(cfg["params"])}
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
        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, params, test))
        print(
            f"[panic_option] {name} "
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
            yearly_rows.append(_row(name, description, str(year), start, end, params, y))
            if year == 2020:
                exit_2020_rows.extend(_exit_rows(name, y["trades"]))

    summary_rows.sort(key=lambda r: (r["period"], -float(r["score"])))
    yearly_rows.sort(key=lambda r: (r["name"], r["period"]))
    _write_csv(output_dir / "summary_panic_option_stop.csv", summary_rows)
    _write_csv(output_dir / "yearly_panic_option_stop.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_panic_option_stop.csv", exit_2020_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "base": BASE,
                "configs": CONFIGS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[panic_option] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
