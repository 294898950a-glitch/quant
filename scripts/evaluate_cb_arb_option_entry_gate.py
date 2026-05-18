"""Evaluate option-sourced value-gap entry gates for cb_arb value-gap switch."""

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
        "name": "baseline_no_entry_gate",
        "description": "当前入口，不限制期权型低估买入",
        "params": {},
    },
    *[
        {
            "name": f"ratio_{str(ratio).replace('.', 'p')}",
            "description": f"期权型候选买入时要求价格/债底 <= {ratio}",
            "params": {
                "option_entry_gate_enabled": 1.0,
                "option_entry_max_close_to_bond_floor": ratio,
            },
        }
        for ratio in (1.15, 1.25, 1.35, 1.50)
    ],
    *[
        {
            "name": f"bad_signal_max_{max_bad}",
            "description": f"期权型候选买入时最多允许 {max_bad} 个弱势信号",
            "params": {
                "option_entry_gate_enabled": 1.0,
                "option_entry_max_bad_signals": max_bad,
            },
        }
        for max_bad in (0, 1, 2)
    ],
    *[
        {
            "name": f"ratio_{str(ratio).replace('.', 'p')}_signal_{max_bad}",
            "description": f"期权型候选买入要求价格/债底 <= {ratio} 且弱势信号 <= {max_bad}",
            "params": {
                "option_entry_gate_enabled": 1.0,
                "option_entry_max_close_to_bond_floor": ratio,
                "option_entry_max_bad_signals": max_bad,
            },
        }
        for ratio in (1.20, 1.30, 1.40)
        for max_bad in (0, 1, 2)
    ],
    *[
        {
            "name": f"option_share_max_{str(max_share).replace('.', 'p')}",
            "description": f"期权型候选买入时要求期权低估占比 <= {max_share}",
            "params": {
                "option_entry_gate_enabled": 1.0,
                "option_entry_max_option_share": max_share,
            },
        }
        for max_share in (0.90, 0.95)
    ],
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


def _source_rows(name: str, result: dict[str, Any], ranks_by_key: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
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
        item = {
            **trade,
            "source": source,
            "bond_share": bond_share,
            "option_share": option_share,
            "close_to_bond_floor": close_to_bond_floor,
        }
        grouped.setdefault(source, []).append(item)

    rows: list[dict[str, Any]] = []
    for source, trades in sorted(grouped.items()):
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
    output_dir = args.output_dir or args.data_root / "value_gap_option_entry_gate"
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
        source_test_rows.extend(_source_rows(name, test, ranks_by_key))
        print(
            f"[option_entry_gate] {name} "
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
                source_2020_rows.extend(_source_rows(name, y, ranks_by_key))

    summary_rows.sort(key=lambda r: (str(r["period"]), -float(r["score"])))
    yearly_rows.sort(key=lambda r: (str(r["name"]), str(r["period"])))
    _write_csv(output_dir / "summary_option_entry_gate.csv", summary_rows)
    _write_csv(output_dir / "yearly_option_entry_gate.csv", yearly_rows)
    _write_csv(output_dir / "exit_2020_option_entry_gate.csv", exit_2020_rows)
    _write_csv(output_dir / "entry_source_2020_option_entry_gate.csv", source_2020_rows)
    _write_csv(output_dir / "entry_source_test_option_entry_gate.csv", source_test_rows)
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
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[option_entry_gate] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
