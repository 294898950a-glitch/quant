"""Evaluate neutral/conservative/panic adjusted value gates for cb_arb."""

from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
    _score,
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

CONSERVATIVE_OVERRIDES = {
    "vol_multiplier_factor": 0.80,
    "credit_spread_add_bp": 100.0,
}

PANIC_OVERRIDES = {
    "credit_spread_add_bp": 200.0,
}

CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_retain30",
        "description": "原始中性价值 + 30%止损复查",
        "adjusted": False,
    },
    {
        "name": "conservative_gate",
        "description": "中性排序，保守价值必须仍低估",
        "adjusted": True,
        "conservative_liquidity": 0.02,
        "panic_liquidity": None,
    },
    {
        "name": "conservative_or_panic3",
        "description": "平时保守价值，恐慌日用恐慌价值扣3%流动性",
        "adjusted": True,
        "conservative_liquidity": 0.02,
        "panic_liquidity": 0.03,
    },
    {
        "name": "conservative_or_panic5",
        "description": "平时保守价值，恐慌日用恐慌价值扣5%流动性",
        "adjusted": True,
        "conservative_liquidity": 0.02,
        "panic_liquidity": 0.05,
    },
]


def _apply_liquidity_discount(ranks: pd.DataFrame, liquidity_pct: float) -> pd.DataFrame:
    rows = ranks.copy()
    rows["theoretical"] = rows["theoretical"].astype(float) * (1.0 - float(liquidity_pct))
    rows["deviation"] = (rows["close"].astype(float) - rows["theoretical"].astype(float)) / rows[
        "theoretical"
    ].astype(float)
    rows["value_gap_amount"] = (
        (rows["theoretical"].astype(float) - rows["close"].astype(float))
        * rows["buy_qty"].astype(float)
    )
    rows["value_gap_pct_of_cash"] = rows["value_gap_amount"] / rows["position_cash"].astype(float)
    return rows


def _load_adjusted_ranks(
    args: argparse.Namespace,
    output_dir: Path,
    name: str,
    start_all: str,
    end_all: str,
    overrides: dict[str, float],
    liquidity_pct: float,
) -> pd.DataFrame:
    base_path = output_dir / f"daily_value_gap_amounts_{name}_pre_liquidity.parquet"
    final_path = output_dir / f"daily_value_gap_amounts_{name}.parquet"
    if args.reuse_ranks and final_path.exists():
        ranks = pd.read_parquet(final_path)
        ranks["trade_date"] = ranks["trade_date"].astype(str)
        ranks["ts_code"] = ranks["ts_code"].astype(str)
        return ranks
    ranks = _load_or_build_value_ranks(
        args.data_root,
        start_all,
        end_all,
        args.fixed_source,
        args.rule,
        base_path,
        args.reuse_ranks,
        config_overrides=overrides,
    )
    ranks = _apply_liquidity_discount(ranks, liquidity_pct)
    ranks.to_parquet(final_path, index=False)
    return ranks


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
        help="Comma-separated config names to run. Defaults to all configs.",
    )
    p.add_argument("--force", action="store_true", help="Re-run configs already present in output CSV.")
    return p.parse_args()


def _load_existing_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return pd.read_csv(path).to_dict("records")


def _completed_config_names(summary_rows: list[dict[str, Any]]) -> set[str]:
    periods_by_name: dict[str, set[str]] = {}
    for row in summary_rows:
        periods_by_name.setdefault(str(row.get("name", "")), set()).add(str(row.get("period", "")))
    return {name for name, periods in periods_by_name.items() if {"train", "test"} <= periods}


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_three_value_gate"
    output_dir.mkdir(parents=True, exist_ok=True)
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)

    selected = None
    if args.configs:
        selected = {name.strip() for name in str(args.configs).split(",") if name.strip()}
        known = {str(cfg["name"]) for cfg in CONFIGS}
        unknown = selected - known
        if unknown:
            raise SystemExit(f"unknown config(s): {', '.join(sorted(unknown))}")

    summary_path = output_dir / "summary_three_value_gate.csv"
    yearly_path = output_dir / "yearly_three_value_gate.csv"
    exit_2020_path = output_dir / "exit_2020_three_value_gate.csv"
    summary_rows: list[dict[str, Any]] = _load_existing_rows(summary_path)
    yearly_rows: list[dict[str, Any]] = _load_existing_rows(yearly_path)
    exit_2020_rows: list[dict[str, Any]] = _load_existing_rows(exit_2020_path)
    completed = _completed_config_names(summary_rows)
    configs_to_run = [
        cfg
        for cfg in CONFIGS
        if (selected is None or str(cfg["name"]) in selected)
        and (args.force or str(cfg["name"]) not in completed)
    ]
    if not configs_to_run:
        print("[three_value] no configs to run", flush=True)
        return 0

    neutral = _load_or_build_value_ranks(
        args.data_root,
        start_all,
        end_all,
        args.fixed_source,
        args.rule,
        output_dir / "daily_value_gap_amounts_neutral.parquet",
        args.reuse_ranks,
    )

    for cfg in configs_to_run:
        name = str(cfg["name"])
        description = str(cfg["description"])
        params = dict(BASE_PARAMS)
        conservative_ranks = None
        panic_ranks = None
        if cfg.get("adjusted"):
            params["adjusted_value_gate_enabled"] = 1.0
            params["adjusted_gate_min_gap_pct"] = 0.0
            params["panic_rule"] = "agent_combo"
            conservative_ranks = _load_adjusted_ranks(
                args,
                output_dir,
                "conservative",
                start_all,
                end_all,
                CONSERVATIVE_OVERRIDES,
                float(cfg.get("conservative_liquidity", 0.02)),
            )
            if cfg.get("panic_liquidity") == 0.03:
                panic_ranks = _load_adjusted_ranks(
                    args,
                    output_dir,
                    "panic3",
                    start_all,
                    end_all,
                    PANIC_OVERRIDES,
                    0.03,
                )
            elif cfg.get("panic_liquidity") == 0.05:
                panic_ranks = _load_adjusted_ranks(
                    args,
                    output_dir,
                    "panic5",
                    start_all,
                    end_all,
                    PANIC_OVERRIDES,
                    0.05,
                )

        train = _run_value_gap_backtest(
            neutral[(neutral["trade_date"] >= args.train_start) & (neutral["trade_date"] <= args.train_end)],
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
            conservative_ranks=conservative_ranks[
                (conservative_ranks["trade_date"] >= args.train_start)
                & (conservative_ranks["trade_date"] <= args.train_end)
            ]
            if conservative_ranks is not None
            else None,
            panic_ranks=panic_ranks[
                (panic_ranks["trade_date"] >= args.train_start)
                & (panic_ranks["trade_date"] <= args.train_end)
            ]
            if panic_ranks is not None
            else None,
        )
        test = _run_value_gap_backtest(
            neutral[(neutral["trade_date"] >= args.test_start) & (neutral["trade_date"] <= args.test_end)],
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
            conservative_ranks=conservative_ranks[
                (conservative_ranks["trade_date"] >= args.test_start)
                & (conservative_ranks["trade_date"] <= args.test_end)
            ]
            if conservative_ranks is not None
            else None,
            panic_ranks=panic_ranks[
                (panic_ranks["trade_date"] >= args.test_start)
                & (panic_ranks["trade_date"] <= args.test_end)
            ]
            if panic_ranks is not None
            else None,
        )
        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, params, test))
        print(
            f"[three_value] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']}",
            flush=True,
        )

        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            y = _run_value_gap_backtest(
                neutral[(neutral["trade_date"] >= start) & (neutral["trade_date"] <= end)],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
                conservative_ranks=conservative_ranks[
                    (conservative_ranks["trade_date"] >= start)
                    & (conservative_ranks["trade_date"] <= end)
                ]
                if conservative_ranks is not None
                else None,
                panic_ranks=panic_ranks[
                    (panic_ranks["trade_date"] >= start) & (panic_ranks["trade_date"] <= end)
                ]
                if panic_ranks is not None
                else None,
            )
            yearly_rows.append(_row(name, description, str(year), start, end, params, y))
            if year == 2020:
                exit_2020_rows.extend(_exit_rows(name, y["trades"]))
        summary_rows.sort(key=lambda r: (str(r["period"]), -float(r["score"])))
        yearly_rows.sort(key=lambda r: (str(r["name"]), str(r["period"])))
        _write_csv(summary_path, summary_rows)
        _write_csv(yearly_path, yearly_rows)
        _write_csv(exit_2020_path, exit_2020_rows)
        del conservative_ranks
        del panic_ranks
        gc.collect()

    summary_rows.sort(key=lambda r: (str(r["period"]), -float(r["score"])))
    yearly_rows.sort(key=lambda r: (str(r["name"]), str(r["period"])))
    _write_csv(summary_path, summary_rows)
    _write_csv(yearly_path, yearly_rows)
    _write_csv(exit_2020_path, exit_2020_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "base_params": BASE_PARAMS,
                "conservative_overrides": CONSERVATIVE_OVERRIDES,
                "panic_overrides": PANIC_OVERRIDES,
                "configs": CONFIGS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[three_value] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
