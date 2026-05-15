"""Evaluate cb_arb value-gap ranking with normal-market volatility inputs."""

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


VOL_CONFIGS: list[dict[str, Any]] = [
    {
        "name": "original_vol",
        "description": "原始估值波动率",
        "overrides": {},
    },
    {
        "name": "vol252",
        "description": "估值使用252天波动率",
        "overrides": {"vol_window_days": 252.0},
    },
    {
        "name": "vol252_cap60",
        "description": "估值使用252天波动率，上限60%",
        "overrides": {"vol_window_days": 252.0, "vol_cap": 0.60},
    },
    {
        "name": "vol252_cap50",
        "description": "估值使用252天波动率，上限50%",
        "overrides": {"vol_window_days": 252.0, "vol_cap": 0.50},
    },
]

STOP_CONFIGS: list[dict[str, Any]] = [
    {
        "name": "price_stop",
        "params": {
            "min_gap_pct": 0.0,
            "sell_gap_pct": 0.0,
            "switch_hurdle_pct": 0.03,
            "max_hold_days": 180.0,
            "stop_gap_ratio_floor": 999.0,
            "stop_signal_threshold": 999.0,
        },
    },
    {
        "name": "retain_30",
        "params": {
            "min_gap_pct": 0.0,
            "sell_gap_pct": 0.0,
            "switch_hurdle_pct": 0.03,
            "max_hold_days": 180.0,
            "stop_gap_ratio_floor": 0.30,
            "stop_signal_threshold": 999.0,
        },
    },
]


def _row(
    vol_name: str,
    vol_description: str,
    stop_name: str,
    period: str,
    start: str,
    end: str,
    overrides: dict[str, float],
    params: dict[str, float],
    result: dict[str, Any],
) -> dict[str, Any]:
    row = {
        "vol_name": vol_name,
        "vol_description": vol_description,
        "stop_name": stop_name,
        "period": period,
        "start": start,
        "end": end,
        "overrides_json": json.dumps(overrides, sort_keys=True),
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
    output_dir = args.output_dir or args.data_root / "value_gap_normal_vol"
    output_dir.mkdir(parents=True, exist_ok=True)
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    exit_2020_rows: list[dict[str, Any]] = []

    for vol_cfg in VOL_CONFIGS:
        vol_name = str(vol_cfg["name"])
        vol_description = str(vol_cfg["description"])
        overrides = dict(vol_cfg["overrides"])
        ranks = _load_or_build_value_ranks(
            args.data_root,
            start_all,
            end_all,
            args.fixed_source,
            args.rule,
            output_dir / f"daily_value_gap_amounts_{vol_name}.parquet",
            args.reuse_ranks,
            config_overrides=overrides,
        )
        for stop_cfg in STOP_CONFIGS:
            stop_name = str(stop_cfg["name"])
            params = dict(stop_cfg["params"])
            name = f"{vol_name}_{stop_name}"
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
            summary_rows.append(
                _row(vol_name, vol_description, stop_name, "train", args.train_start, args.train_end, overrides, params, train)
            )
            summary_rows.append(
                _row(vol_name, vol_description, stop_name, "test", args.test_start, args.test_end, overrides, params, test)
            )
            print(
                f"[normal_vol] {name} "
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
                yearly_rows.append(
                    _row(vol_name, vol_description, stop_name, str(year), start, end, overrides, params, y)
                )
                if year == 2020:
                    exit_2020_rows.extend(_exit_rows(name, y["trades"]))

    summary_rows.sort(key=lambda r: (r["period"], -float(r["score"])))
    yearly_rows.sort(key=lambda r: (r["vol_name"], r["stop_name"], r["period"]))
    _write_csv(output_dir / "summary_normal_vol.csv", summary_rows)
    _write_csv(output_dir / "yearly_normal_vol.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_normal_vol.csv", exit_2020_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "vol_configs": VOL_CONFIGS,
                "stop_configs": STOP_CONFIGS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[normal_vol] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
