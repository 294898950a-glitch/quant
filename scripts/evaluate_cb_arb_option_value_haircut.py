"""Evaluate option-value haircuts for cb_arb value-gap switch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

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
        "name": "baseline_no_haircut",
        "description": "当前主策略估值，不调整期权价值",
        "mode": "none",
    },
    *[
        {
            "name": f"ratio_1p4_weight_{str(weight).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.4 时，期权价值乘以 {weight}",
            "mode": "ratio",
            "ratio_threshold": 1.40,
            "option_weight": weight,
        }
        for weight in (0.75, 0.50)
    ],
    *[
        {
            "name": f"ratio_1p5_weight_{str(weight).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.5 时，期权价值乘以 {weight}",
            "mode": "ratio",
            "ratio_threshold": 1.50,
            "option_weight": weight,
        }
        for weight in (0.75, 0.50)
    ],
    *[
        {
            "name": f"moneyness_1p6_weight_{str(weight).replace('.', 'p')}",
            "description": f"期权型低估且正股/转股价 > 1.6 时，期权价值乘以 {weight}",
            "mode": "moneyness",
            "moneyness_threshold": 1.60,
            "option_weight": weight,
        }
        for weight in (0.75, 0.50)
    ],
    *[
        {
            "name": f"ratio_1p5_or_mny_1p6_weight_{str(weight).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.5 或正股/转股价 > 1.6 时，期权价值乘以 {weight}",
            "mode": "ratio_or_moneyness",
            "ratio_threshold": 1.50,
            "moneyness_threshold": 1.60,
            "option_weight": weight,
        }
        for weight in (0.75, 0.50, 0.25)
    ],
    {
        "name": "progressive_stocklike_haircut",
        "description": "越像股票、越远离债底，期权价值折减越强",
        "mode": "progressive",
    },
]


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
    p.add_argument("--base-ranks-path", type=Path, default=None)
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    return p.parse_args()


def _load_base_ranks(args: argparse.Namespace, output_dir: Path) -> pd.DataFrame:
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)
    if args.base_ranks_path is not None and args.base_ranks_path.exists():
        ranks = pd.read_parquet(args.base_ranks_path)
    else:
        ranks = _load_or_build_value_ranks(
            args.data_root,
            start_all,
            end_all,
            args.fixed_source,
            args.rule,
            output_dir / "daily_value_gap_amounts_base.parquet",
            args.reuse_ranks,
        )
    ranks = ranks.copy()
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    ranks["ts_code"] = ranks["ts_code"].astype(str)
    ranks = ranks[(ranks["trade_date"] >= start_all) & (ranks["trade_date"] <= end_all)]
    return _add_moneyness(ranks)


def _add_moneyness(ranks: pd.DataFrame) -> pd.DataFrame:
    basic = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_basic.parquet")
    basic = basic[["ts_code", "stk_code", "conv_price"]].copy()
    basic["ts_code"] = basic["ts_code"].astype(str)
    basic["stk_code"] = basic["stk_code"].astype(str)
    basic["conv_price"] = pd.to_numeric(basic["conv_price"], errors="coerce")

    stock = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/stk_daily_qfq.parquet")
    stock = stock[["trade_date", "stk_code", "close"]].copy()
    stock["trade_date"] = stock["trade_date"].astype(str)
    stock["stk_code"] = stock["stk_code"].astype(str)
    stock = stock.rename(columns={"close": "stock_close"})
    stock["stock_close"] = pd.to_numeric(stock["stock_close"], errors="coerce")

    out = ranks.merge(basic, on="ts_code", how="left").merge(
        stock,
        on=["trade_date", "stk_code"],
        how="left",
    )
    out["moneyness_stock_to_conv"] = out["stock_close"] / out["conv_price"]
    return out


def _option_source_mask(rows: pd.DataFrame) -> pd.Series:
    close = rows["close"].astype(float)
    bond_floor = rows["bond_floor"].astype(float)
    theoretical = rows["theoretical"].astype(float)
    base_gap = theoretical - close
    bond_gap = (bond_floor - close).clip(lower=0.0)
    option_gap = theoretical - pd.concat([close, bond_floor], axis=1).max(axis=1)
    option_gap = option_gap.clip(lower=0.0)
    total_gap = bond_gap + option_gap
    option_share = option_gap / total_gap.where(total_gap > 0, pd.NA)
    return (base_gap > 0) & (option_share >= 0.60)


def _adjust_ranks(ranks: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    adjusted = ranks.copy()
    mode = str(cfg.get("mode", "none"))
    if mode == "none":
        return adjusted, {
            "name": cfg["name"],
            "adjusted_rows": 0,
            "avg_option_weight": 1.0,
            "min_option_weight": 1.0,
            "max_option_weight": 1.0,
        }

    close = adjusted["close"].astype(float)
    bond_floor = adjusted["bond_floor"].astype(float)
    option_value = adjusted["option_value"].astype(float)
    ratio = close / bond_floor.where(bond_floor > 0, pd.NA)
    moneyness = pd.to_numeric(adjusted["moneyness_stock_to_conv"], errors="coerce")
    option_mask = _option_source_mask(adjusted)

    if mode == "ratio":
        mask = option_mask & (ratio > float(cfg["ratio_threshold"]))
        weight = pd.Series(1.0, index=adjusted.index)
        weight.loc[mask] = float(cfg["option_weight"])
    elif mode == "moneyness":
        mask = option_mask & (moneyness > float(cfg["moneyness_threshold"]))
        weight = pd.Series(1.0, index=adjusted.index)
        weight.loc[mask] = float(cfg["option_weight"])
    elif mode == "ratio_or_moneyness":
        mask = option_mask & (
            (ratio > float(cfg["ratio_threshold"]))
            | (moneyness > float(cfg["moneyness_threshold"]))
        )
        weight = pd.Series(1.0, index=adjusted.index)
        weight.loc[mask] = float(cfg["option_weight"])
    elif mode == "progressive":
        mask = option_mask & ((ratio > 1.40) | (moneyness > 1.60))
        weight = pd.Series(1.0, index=adjusted.index)
        weight.loc[option_mask & ((ratio > 1.40) | (moneyness > 1.60))] = 0.75
        weight.loc[option_mask & ((ratio > 1.50) | (moneyness > 1.70))] = 0.50
        weight.loc[option_mask & ((ratio > 1.70) | (moneyness > 2.00))] = 0.25
    else:
        raise ValueError(f"unknown haircut mode: {mode}")

    new_theoretical = bond_floor + option_value * weight
    adjusted.loc[mask, "theoretical"] = new_theoretical.loc[mask]
    adjusted.loc[mask, "deviation"] = (
        (close.loc[mask] - new_theoretical.loc[mask]) / new_theoretical.loc[mask]
    )
    adjusted.loc[mask, "value_gap_amount"] = (
        (new_theoretical.loc[mask] - close.loc[mask])
        * adjusted.loc[mask, "buy_qty"].astype(float)
    )
    adjusted.loc[mask, "value_gap_pct_of_cash"] = (
        adjusted.loc[mask, "value_gap_amount"]
        / adjusted.loc[mask, "position_cash"].astype(float)
    )
    adjusted = adjusted.sort_values(
        ["trade_date", "value_gap_amount"],
        ascending=[True, False],
    ).reset_index(drop=True)
    adjusted["rank"] = adjusted.groupby("trade_date").cumcount()

    touched_weight = weight.loc[mask]
    return adjusted, {
        "name": cfg["name"],
        "adjusted_rows": int(mask.sum()),
        "avg_option_weight": round(float(touched_weight.mean()), 6) if not touched_weight.empty else 1.0,
        "min_option_weight": round(float(touched_weight.min()), 6) if not touched_weight.empty else 1.0,
        "max_option_weight": round(float(touched_weight.max()), 6) if not touched_weight.empty else 1.0,
    }


def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    cfg: dict[str, Any],
    params: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    row = {
        "name": name,
        "description": description,
        "period": period,
        "start": start,
        "end": end,
        "haircut_config_json": json.dumps(cfg, sort_keys=True),
        "params_json": json.dumps(params, sort_keys=True),
        **result["metrics"],
    }
    row["score"] = _score(result["metrics"])
    return row


def _source_rows(name: str, result: dict[str, Any], ranks_by_key: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in result["trades"]:
        key = (str(trade["entry_date"]), str(trade["cb_code"]))
        rank_row = ranks_by_key.get(key)
        source = "missing"
        bond_share = 0.0
        option_share = 0.0
        close_to_bond_floor = None
        moneyness = None
        if rank_row is not None:
            source, bond_share, option_share = _gap_source_shares(rank_row, {})
            if float(getattr(rank_row, "bond_floor", 0.0) or 0.0) > 0:
                close_to_bond_floor = float(rank_row.close) / float(rank_row.bond_floor)
            value = getattr(rank_row, "moneyness_stock_to_conv", None)
            try:
                moneyness = float(value)
            except (TypeError, ValueError):
                moneyness = None
        grouped.setdefault(source, []).append(
            {
                **trade,
                "bond_share": bond_share,
                "option_share": option_share,
                "close_to_bond_floor": close_to_bond_floor,
                "moneyness_stock_to_conv": moneyness,
            }
        )

    rows: list[dict[str, Any]] = []
    for source, trades in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in trades]
        pnl_amount = [float(t["pnl_amount"]) for t in trades]
        ratios = [
            float(t["close_to_bond_floor"])
            for t in trades
            if t["close_to_bond_floor"] is not None
        ]
        moneyness_values = [
            float(t["moneyness_stock_to_conv"])
            for t in trades
            if t["moneyness_stock_to_conv"] is not None
        ]
        rows.append(
            {
                "name": name,
                "source": source,
                "count": len(trades),
                "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 6),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "wins": sum(1 for v in pnl_pct if v > 0),
                "avg_close_to_bond_floor": round(sum(ratios) / len(ratios), 6) if ratios else None,
                "avg_moneyness_stock_to_conv": (
                    round(sum(moneyness_values) / len(moneyness_values), 6)
                    if moneyness_values
                    else None
                ),
            }
        )
    return rows


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_option_value_haircut"
    output_dir.mkdir(parents=True, exist_ok=True)

    base_ranks = _load_base_ranks(args, output_dir)
    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    source_2020_rows: list[dict[str, Any]] = []
    source_test_rows: list[dict[str, Any]] = []
    adjustment_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        adjusted, adjustment = _adjust_ranks(base_ranks, cfg)
        adjustment_rows.append(adjustment)
        adjusted.to_parquet(output_dir / f"daily_value_gap_amounts_{name}.parquet", index=False)
        ranks_by_key = {
            (str(r.trade_date), str(r.ts_code)): r
            for r in adjusted.itertuples(index=False)
        }
        params = _with_cost_params(dict(BASE_PARAMS), args)
        train = _run_value_gap_backtest(
            adjusted[(adjusted["trade_date"] >= args.train_start) & (adjusted["trade_date"] <= args.train_end)],
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        test = _run_value_gap_backtest(
            adjusted[(adjusted["trade_date"] >= args.test_start) & (adjusted["trade_date"] <= args.test_end)],
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, cfg, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, cfg, params, test))
        source_test_rows.extend(_source_rows(name, test, ranks_by_key))
        print(
            f"[option_value_haircut] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']} "
            f"adjusted_rows={adjustment['adjusted_rows']}",
            flush=True,
        )

        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            y = _run_value_gap_backtest(
                adjusted[(adjusted["trade_date"] >= start) & (adjusted["trade_date"] <= end)],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            yearly_rows.append(_row(name, description, str(year), start, end, cfg, params, y))
            if year == 2020:
                source_2020_rows.extend(_source_rows(name, y, ranks_by_key))

    _write_csv(output_dir / "summary_option_value_haircut.csv", summary_rows)
    _write_csv(output_dir / "yearly_option_value_haircut.csv", yearly_rows)
    _write_csv(output_dir / "entry_source_2020_option_value_haircut.csv", source_2020_rows)
    _write_csv(output_dir / "entry_source_test_option_value_haircut.csv", source_test_rows)
    _write_csv(output_dir / "adjustment_option_value_haircut.csv", adjustment_rows)

    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = next((r for r in train_rows if r["name"] == "baseline_no_haircut"), {})
    baseline_test = next((r for r in test_rows if r["name"] == "baseline_no_haircut"), {})
    summary = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "candidate_count": len(CONFIGS),
        "best_train": best_train,
        "best_test": best_test,
        "baseline_train": baseline_train,
        "baseline_test": baseline_test,
        "artifacts": [
            "summary_option_value_haircut.csv",
            "yearly_option_value_haircut.csv",
            "entry_source_2020_option_value_haircut.csv",
            "entry_source_test_option_value_haircut.csv",
            "adjustment_option_value_haircut.csv",
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "date": "2026-05-17",
                "strategy_id": "cb_arb_value_gap_switch",
                "l6_exit_decision": "pending_manual_review",
                "status": "COMPLETE",
                "summary": "Option-value haircut experiment completed; pending manual review against train/yearly/test.",
                "references": summary["artifacts"],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
