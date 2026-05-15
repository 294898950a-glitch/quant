"""Evaluate stop-loss revaluation by source of remaining value gap."""

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

SOURCE_CONFIGS: list[dict[str, Any]] = [
    {
        "name": "normal_revalue",
        "description": "止损复查用原估值",
        "params": {},
    },
    {
        "name": "all_credit_200bp_proxy",
        "description": "全体信用折价 +200bp 的旧对照近似，保留作基准",
        "params": {},
        "config_overrides": {"credit_spread_add_bp": 200.0},
    },
    {
        "name": "source_bond20_option10_mixed_light",
        "description": "债底型债底打 8 折，期权型期权打 9 折，混合轻度压力",
        "params": {
            "source_stress_enabled": 1.0,
            "stress_bond_cut_bond": 0.20,
            "stress_option_cut_bond": 0.00,
            "stress_bond_cut_option": 0.00,
            "stress_option_cut_option": 0.10,
            "stress_bond_cut_mixed": 0.10,
            "stress_option_cut_mixed": 0.05,
        },
    },
    {
        "name": "source_bond10_option10_mixed_light",
        "description": "债底型债底打 9 折，期权型期权打 9 折，混合轻度压力",
        "params": {
            "source_stress_enabled": 1.0,
            "stress_bond_cut_bond": 0.10,
            "stress_option_cut_bond": 0.00,
            "stress_bond_cut_option": 0.00,
            "stress_option_cut_option": 0.10,
            "stress_bond_cut_mixed": 0.05,
            "stress_option_cut_mixed": 0.05,
        },
    },
    {
        "name": "source_bond20_option15_mixed_mid",
        "description": "债底型债底打 8 折，期权型期权打 85 折，混合中度压力",
        "params": {
            "source_stress_enabled": 1.0,
            "stress_bond_cut_bond": 0.20,
            "stress_option_cut_bond": 0.00,
            "stress_bond_cut_option": 0.00,
            "stress_option_cut_option": 0.15,
            "stress_bond_cut_mixed": 0.10,
            "stress_option_cut_mixed": 0.08,
        },
    },
]


def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    params: dict[str, float],
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
    output_dir = args.output_dir or args.data_root / "value_gap_stop_source_stress"
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

    for cfg in SOURCE_CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        params = {**BASE_PARAMS, **dict(cfg.get("params", {}))}
        config_overrides = dict(cfg.get("config_overrides", {}))
        if config_overrides:
            stop_ranks = _load_or_build_value_ranks(
                args.data_root,
                start_all,
                end_all,
                args.fixed_source,
                args.rule,
                output_dir / f"daily_value_gap_amounts_stop_{name}.parquet",
                args.reuse_ranks,
                config_overrides=config_overrides,
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
            params,
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
            params,
            stop_revalue_ranks=stop_ranks[
                (stop_ranks["trade_date"] >= args.test_start)
                & (stop_ranks["trade_date"] <= args.test_end)
            ],
        )
        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, params, test))
        print(
            f"[source_stress] {name} "
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
                params,
                stop_revalue_ranks=stop_ranks[
                    (stop_ranks["trade_date"] >= start) & (stop_ranks["trade_date"] <= end)
                ],
            )
            yearly_rows.append(_row(name, description, str(year), start, end, params, y))
            if year == 2020:
                exit_2020_rows.extend(_exit_rows(name, y["trades"]))

    summary_rows.sort(key=lambda r: (r["period"], -float(r["score"])))
    yearly_rows.sort(key=lambda r: (r["name"], r["period"]))
    _write_csv(output_dir / "summary_stop_source_stress.csv", summary_rows)
    _write_csv(output_dir / "yearly_stop_source_stress.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_stop_source_stress.csv", exit_2020_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "base_params": BASE_PARAMS,
                "configs": SOURCE_CONFIGS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[source_stress] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
