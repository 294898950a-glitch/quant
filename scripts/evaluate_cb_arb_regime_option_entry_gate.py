"""Evaluate regime-specific option-sourced entry gates for cb_arb value-gap switch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_daily_regime_switch import _build_daily_features  # noqa: E402
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
        "name": "baseline_no_regime_entry_gate",
        "description": "当前主策略入口；不按市场状态限制期权型低估买入",
        "params": {},
    },
    {
        "name": "weak_signal_0",
        "description": "仅弱市：期权型买入不允许弱势信号",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_bad_signals_weak": 0,
        },
    },
    {
        "name": "weak_signal_1",
        "description": "仅弱市：期权型买入最多 1 个弱势信号",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_bad_signals_weak": 1,
        },
    },
    {
        "name": "weak_ratio_1p15",
        "description": "仅弱市：期权型买入要求价格/债底 <= 1.15",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_close_to_bond_floor_weak": 1.15,
        },
    },
    {
        "name": "weak_ratio_1p25",
        "description": "仅弱市：期权型买入要求价格/债底 <= 1.25",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_close_to_bond_floor_weak": 1.25,
        },
    },
    {
        "name": "weak_ratio_1p25_signal_1",
        "description": "仅弱市：期权型买入要求价格/债底 <= 1.25 且弱势信号 <= 1",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_close_to_bond_floor_weak": 1.25,
            "option_entry_max_bad_signals_weak": 1,
        },
    },
    {
        "name": "flat_weak_signal_1",
        "description": "仅弱震荡：期权型买入最多 1 个弱势信号",
        "params": {
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_bad_signals_flat_weak": 1,
        },
    },
    {
        "name": "flat_weak_ratio_1p25",
        "description": "仅弱震荡：期权型买入要求价格/债底 <= 1.25",
        "params": {
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_close_to_bond_floor_flat_weak": 1.25,
        },
    },
    {
        "name": "weak_strict_flat_mild",
        "description": "弱市严格，弱震荡轻度限制；强市和普通市场放开",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_close_to_bond_floor_weak": 1.20,
            "option_entry_max_bad_signals_weak": 0,
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_close_to_bond_floor_flat_weak": 1.35,
            "option_entry_max_bad_signals_flat_weak": 1,
        },
    },
    {
        "name": "weak_mild_flat_signal",
        "description": "弱市价格接近债底，弱震荡只看弱势信号",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_close_to_bond_floor_weak": 1.30,
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_bad_signals_flat_weak": 1,
        },
    },
    {
        "name": "weak_and_flat_signal_1",
        "description": "弱市和弱震荡都最多允许 1 个弱势信号",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_bad_signals_weak": 1,
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_bad_signals_flat_weak": 1,
        },
    },
    {
        "name": "weak_signal_0_flat_signal_2",
        "description": "弱市不允许弱势信号，弱震荡最多 2 个弱势信号",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_bad_signals_weak": 0,
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_bad_signals_flat_weak": 2,
        },
    },
    {
        "name": "weak_ratio_1p15_flat_ratio_1p35",
        "description": "弱市要求更接近债底，弱震荡允许稍远",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_close_to_bond_floor_weak": 1.15,
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_close_to_bond_floor_flat_weak": 1.35,
        },
    },
    {
        "name": "weak_share_0p90",
        "description": "仅弱市：期权低估占比不能超过 90%",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_option_share_weak": 0.90,
        },
    },
    {
        "name": "weak_share_0p95_flat_share_0p95",
        "description": "弱市和弱震荡：期权低估占比不能超过 95%",
        "params": {
            "option_entry_gate_enabled_weak": 1.0,
            "option_entry_max_option_share_weak": 0.95,
            "option_entry_gate_enabled_flat_weak": 1.0,
            "option_entry_max_option_share_flat_weak": 0.95,
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


def _source_regime_rows(
    name: str,
    result: dict[str, Any],
    ranks_by_key: dict[tuple[str, str], Any],
    features: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in result["trades"]:
        key = (str(trade["entry_date"]), str(trade["cb_code"]))
        rank_row = ranks_by_key.get(key)
        source = "missing"
        bond_share = 0.0
        option_share = 0.0
        close_to_bond_floor = None
        if rank_row is not None:
            source, bond_share, option_share = _gap_source_shares(rank_row, {})
            if float(getattr(rank_row, "bond_floor", 0.0) or 0.0) > 0:
                close_to_bond_floor = float(rank_row.close) / float(rank_row.bond_floor)
        regime = str(features.get(str(trade["entry_date"]), {}).get("regime", "neutral"))
        grouped.setdefault((regime, source), []).append(
            {
                **trade,
                "bond_share": bond_share,
                "option_share": option_share,
                "close_to_bond_floor": close_to_bond_floor,
            }
        )

    rows: list[dict[str, Any]] = []
    for (regime, source), trades in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in trades]
        pnl_amount = [float(t["pnl_amount"]) for t in trades]
        option_share = [float(t["option_share"]) for t in trades]
        ratios = [
            float(t["close_to_bond_floor"])
            for t in trades
            if t["close_to_bond_floor"] is not None
        ]
        rows.append(
            {
                "name": name,
                "regime": regime,
                "source": source,
                "count": len(trades),
                "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 6),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "wins": sum(1 for v in pnl_pct if v > 0),
                "avg_option_share": round(sum(option_share) / len(option_share), 6),
                "avg_close_to_bond_floor": round(sum(ratios) / len(ratios), 6) if ratios else None,
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
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "regime_option_entry_gate"
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
    ranks_by_key = {
        (str(r.trade_date), str(r.ts_code)): r
        for r in ranks.itertuples(index=False)
    }
    features = _build_daily_features(252, args.rule)

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    exit_2020_rows: list[dict[str, Any]] = []
    source_2020_rows: list[dict[str, Any]] = []
    source_test_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        params = _with_cost_params({**BASE_PARAMS, **dict(cfg.get("params", {}))}, args)
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
        source_test_rows.extend(_source_regime_rows(name, test, ranks_by_key, features))
        print(
            f"[regime_option_entry_gate] {name} "
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
                source_2020_rows.extend(_source_regime_rows(name, y, ranks_by_key, features))

    summary_rows.sort(key=lambda r: (str(r["period"]), -float(r["score"])))
    yearly_rows.sort(key=lambda r: (str(r["name"]), str(r["period"])))
    _write_csv(output_dir / "summary_regime_option_entry_gate.csv", summary_rows)
    _write_csv(output_dir / "yearly_regime_option_entry_gate.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_regime_option_entry_gate.csv", exit_2020_rows)
    _write_csv(output_dir / "entry_source_2020_regime_option_entry_gate.csv", source_2020_rows)
    _write_csv(output_dir / "entry_source_test_regime_option_entry_gate.csv", source_test_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "train_start": args.train_start,
                "train_end": args.train_end,
                "test_start": args.test_start,
                "test_end": args.test_end,
                "base_params": BASE_PARAMS,
                "configs": CONFIGS,
                "cost_model_enabled": bool(args.cost_model_enabled),
                "regime_rule": args.rule,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[regime_option_entry_gate] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
