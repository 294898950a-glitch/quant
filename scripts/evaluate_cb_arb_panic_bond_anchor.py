"""Evaluate using bond-floor protection for option-type CBs on panic days."""

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
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}


CONFIGS: list[dict[str, Any]] = [
    {
        "name": "buy_anchor_05",
        "description": "恐慌日只允许债底上方5%以内的期权型候选买入/换仓",
        "params": {
            "panic_rule": "agent_combo",
            "panic_option_bond_anchor_enabled": 1.0,
            "panic_option_bond_floor_buffer": 0.05,
        },
    },
    {
        "name": "buy_anchor_10",
        "description": "恐慌日只允许债底上方10%以内的期权型候选买入/换仓",
        "params": {
            "panic_rule": "agent_combo",
            "panic_option_bond_anchor_enabled": 1.0,
            "panic_option_bond_floor_buffer": 0.10,
        },
    },
    {
        "name": "buy_anchor_15",
        "description": "恐慌日只允许债底上方15%以内的期权型候选买入/换仓",
        "params": {
            "panic_rule": "agent_combo",
            "panic_option_bond_anchor_enabled": 1.0,
            "panic_option_bond_floor_buffer": 0.15,
        },
    },
    {
        "name": "sell_anchor_05",
        "description": "恐慌日持仓也必须在债底上方5%以内，否则卖出",
        "params": {
            "panic_rule": "agent_combo",
            "panic_option_bond_anchor_enabled": 1.0,
            "panic_option_bond_anchor_sell_enabled": 1.0,
            "panic_option_bond_floor_buffer": 0.05,
        },
    },
    {
        "name": "sell_anchor_10",
        "description": "恐慌日持仓也必须在债底上方10%以内，否则卖出",
        "params": {
            "panic_rule": "agent_combo",
            "panic_option_bond_anchor_enabled": 1.0,
            "panic_option_bond_anchor_sell_enabled": 1.0,
            "panic_option_bond_floor_buffer": 0.10,
        },
    },
    {
        "name": "sell_anchor_15",
        "description": "恐慌日持仓也必须在债底上方15%以内，否则卖出",
        "params": {
            "panic_rule": "agent_combo",
            "panic_option_bond_anchor_enabled": 1.0,
            "panic_option_bond_anchor_sell_enabled": 1.0,
            "panic_option_bond_floor_buffer": 0.15,
        },
    },
]


def _with_cost_params(params: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not args.cost_model_enabled:
        return dict(params)
    out = dict(params)
    out.update(
        {
            "cost_model_enabled": 1.0,
            "slippage_pct": float(args.slippage_pct),
            "market_impact_coeff": float(args.market_impact_coeff),
            "market_impact_cap_pct": float(args.market_impact_cap_pct),
            "holding_cost_pct": float(args.holding_cost_pct),
        }
    )
    return out


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
    p.add_argument(
        "--configs",
        default=None,
        help="Comma-separated config names to run. Defaults to all non-baseline configs.",
    )
    p.add_argument("--panic-dates-file", type=Path, default=None)
    p.add_argument("--panic-signal-column", default="panic_day_trained")
    p.add_argument("--panic-effective-lag-days", type=int, default=1)
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_panic_bond_anchor"
    output_dir.mkdir(parents=True, exist_ok=True)
    base = _with_cost_params(BASE, args)
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
    configs = CONFIGS
    if args.configs:
        selected = {name.strip() for name in str(args.configs).split(",") if name.strip()}
        known = {str(cfg["name"]) for cfg in CONFIGS}
        unknown = selected - known
        if unknown:
            raise SystemExit(f"unknown config(s): {', '.join(sorted(unknown))}")
        configs = [cfg for cfg in CONFIGS if str(cfg["name"]) in selected]

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    exit_2020_rows: list[dict[str, Any]] = []

    for cfg in configs:
        name = str(cfg["name"])
        description = str(cfg["description"])
        params = {**base, **dict(cfg["params"])}
        if args.panic_dates_file is not None:
            params["panic_dates_file"] = str(args.panic_dates_file)
            params["panic_signal_column"] = str(args.panic_signal_column)
            params["panic_effective_lag_days"] = int(args.panic_effective_lag_days)
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
            f"[panic_bond_anchor] {name} "
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
    _write_csv(output_dir / "summary_panic_bond_anchor.csv", summary_rows)
    _write_csv(output_dir / "yearly_panic_bond_anchor.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_panic_bond_anchor.csv", exit_2020_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "base": base,
                "configs": configs,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[panic_bond_anchor] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
