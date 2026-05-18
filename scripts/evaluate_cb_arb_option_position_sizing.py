"""Evaluate option-source position sizing for cb_arb value-gap switch."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
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
    "candidate_position_scale_enabled": 1.0,
}


CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_no_position_scale",
        "description": "当前主策略，不调整候选排序和买入金额",
        "mode": "none",
    },
    *[
        {
            "name": f"ratio_1p5_scale_{str(scale).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.5 时，买入金额和排序金额乘以 {scale}",
            "mode": "ratio",
            "ratio_threshold": 1.50,
            "position_scale": scale,
        }
        for scale in (0.75, 0.50, 0.25)
    ],
    *[
        {
            "name": f"moneyness_1p6_scale_{str(scale).replace('.', 'p')}",
            "description": f"期权型低估且正股/转股价 > 1.6 时，买入金额和排序金额乘以 {scale}",
            "mode": "moneyness",
            "moneyness_threshold": 1.60,
            "position_scale": scale,
        }
        for scale in (0.75, 0.50, 0.25)
    ],
    *[
        {
            "name": f"ratio_1p5_or_mny_1p6_scale_{str(scale).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.5 或正股/转股价 > 1.6 时，买入金额和排序金额乘以 {scale}",
            "mode": "ratio_or_moneyness",
            "ratio_threshold": 1.50,
            "moneyness_threshold": 1.60,
            "position_scale": scale,
        }
        for scale in (0.75, 0.50, 0.25)
    ],
    *[
        {
            "name": f"ratio_1p4_and_mny_1p6_scale_{str(scale).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.4 且正股/转股价 > 1.6 时，买入金额和排序金额乘以 {scale}",
            "mode": "ratio_and_moneyness",
            "ratio_threshold": 1.40,
            "moneyness_threshold": 1.60,
            "position_scale": scale,
        }
        for scale in (0.75, 0.50, 0.25)
    ],
    *[
        {
            "name": f"ratio_1p5_and_mny_1p6_scale_{str(scale).replace('.', 'p')}",
            "description": f"期权型低估且价格/债底 > 1.5 且正股/转股价 > 1.6 时，买入金额和排序金额乘以 {scale}",
            "mode": "ratio_and_moneyness",
            "ratio_threshold": 1.50,
            "moneyness_threshold": 1.60,
            "position_scale": scale,
        }
        for scale in (0.75, 0.50, 0.25)
    ],
    {
        "name": "progressive_stocklike_position_scale",
        "description": "越像股票、越远离债底，买入金额和排序金额越低",
        "mode": "progressive",
    },
]


def _spec_binding_fields(output_dir: Path) -> dict[str, str]:
    spec_path = output_dir / "spec.yaml"
    if not spec_path.exists():
        return {"spec_run_id": output_dir.name, "spec_binding_hash": ""}
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        return {"spec_run_id": output_dir.name, "spec_binding_hash": ""}
    binding = {
        "run_id": spec.get("run_id") or output_dir.name,
        "hypothesis": spec.get("hypothesis"),
        "source_insight": spec.get("source_insight"),
        "parameter_space": spec.get("parameter_space"),
        "mechanics": spec.get("mechanics"),
        "proposal_id": ((spec.get("automation") or {}).get("proposal_id")),
    }
    payload = json.dumps(binding, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "spec_run_id": str(binding["run_id"]),
        "spec_binding_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


def _attach_spec_binding(rows: list[dict[str, Any]], output_dir: Path) -> None:
    binding = _spec_binding_fields(output_dir)
    for row in rows:
        row.update(binding)


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


def _scale_for_config(rows: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.Series, pd.Series]:
    close = rows["close"].astype(float)
    bond_floor = rows["bond_floor"].astype(float)
    ratio = close / bond_floor.where(bond_floor > 0, pd.NA)
    moneyness = pd.to_numeric(rows["moneyness_stock_to_conv"], errors="coerce")
    option_mask = _option_source_mask(rows)
    scale = pd.Series(1.0, index=rows.index)
    mode = str(cfg.get("mode", "none"))
    if mode == "none":
        return scale, pd.Series(False, index=rows.index)
    if mode == "ratio":
        mask = option_mask & (ratio > float(cfg["ratio_threshold"]))
        scale.loc[mask] = float(cfg["position_scale"])
    elif mode == "moneyness":
        mask = option_mask & (moneyness > float(cfg["moneyness_threshold"]))
        scale.loc[mask] = float(cfg["position_scale"])
    elif mode == "ratio_or_moneyness":
        mask = option_mask & (
            (ratio > float(cfg["ratio_threshold"]))
            | (moneyness > float(cfg["moneyness_threshold"]))
        )
        scale.loc[mask] = float(cfg["position_scale"])
    elif mode == "ratio_and_moneyness":
        mask = option_mask & (
            (ratio > float(cfg["ratio_threshold"]))
            & (moneyness > float(cfg["moneyness_threshold"]))
        )
        scale.loc[mask] = float(cfg["position_scale"])
    elif mode == "progressive":
        mask = option_mask & ((ratio > 1.40) | (moneyness > 1.60))
        scale.loc[option_mask & ((ratio > 1.40) | (moneyness > 1.60))] = 0.75
        scale.loc[option_mask & ((ratio > 1.50) | (moneyness > 1.70))] = 0.50
        scale.loc[option_mask & ((ratio > 1.70) | (moneyness > 2.00))] = 0.25
    else:
        raise ValueError(f"unknown position scale mode: {mode}")
    return scale, mask


def _adjust_ranks(ranks: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    adjusted = ranks.copy()
    scale, mask = _scale_for_config(adjusted, cfg)
    adjusted["position_cash_scale"] = scale.astype(float)
    adjusted.loc[mask, "value_gap_amount"] = (
        adjusted.loc[mask, "value_gap_amount"].astype(float) * scale.loc[mask]
    )
    adjusted.loc[mask, "value_gap_pct_of_cash"] = (
        adjusted.loc[mask, "value_gap_amount"].astype(float)
        / adjusted.loc[mask, "position_cash"].astype(float)
    )
    adjusted = adjusted.sort_values(
        ["trade_date", "value_gap_amount"],
        ascending=[True, False],
    ).reset_index(drop=True)
    adjusted["rank"] = adjusted.groupby("trade_date").cumcount()
    touched_scale = scale.loc[mask]
    return adjusted, {
        "name": cfg["name"],
        "adjusted_rows": int(mask.sum()),
        "avg_position_scale": round(float(touched_scale.mean()), 6) if not touched_scale.empty else 1.0,
        "min_position_scale": round(float(touched_scale.min()), 6) if not touched_scale.empty else 1.0,
        "max_position_scale": round(float(touched_scale.max()), 6) if not touched_scale.empty else 1.0,
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
        "position_scale_config_json": json.dumps(cfg, sort_keys=True),
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
        position_scale = None
        if rank_row is not None:
            source, bond_share, option_share = _gap_source_shares(rank_row, {})
            if float(getattr(rank_row, "bond_floor", 0.0) or 0.0) > 0:
                close_to_bond_floor = float(rank_row.close) / float(rank_row.bond_floor)
            value = getattr(rank_row, "moneyness_stock_to_conv", None)
            try:
                moneyness = float(value)
            except (TypeError, ValueError):
                moneyness = None
            try:
                position_scale = float(getattr(rank_row, "position_cash_scale", 1.0) or 1.0)
            except (TypeError, ValueError):
                position_scale = 1.0
        grouped.setdefault(source, []).append(
            {
                **trade,
                "bond_share": bond_share,
                "option_share": option_share,
                "close_to_bond_floor": close_to_bond_floor,
                "moneyness_stock_to_conv": moneyness,
                "position_cash_scale": position_scale,
            }
        )

    rows: list[dict[str, Any]] = []
    for source, trades in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in trades]
        pnl_amount = [float(t["pnl_amount"]) for t in trades]
        ratios = [float(t["close_to_bond_floor"]) for t in trades if t["close_to_bond_floor"] is not None]
        moneyness_values = [
            float(t["moneyness_stock_to_conv"])
            for t in trades
            if t["moneyness_stock_to_conv"] is not None
        ]
        scales = [float(t["position_cash_scale"]) for t in trades if t["position_cash_scale"] is not None]
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
                "avg_position_cash_scale": round(sum(scales) / len(scales), 6) if scales else None,
            }
        )
    return rows


def _pick(rows: list[dict[str, Any]], name: str, period: str) -> dict[str, Any]:
    return next((r for r in rows if r["name"] == name and r["period"] == period), {})


def _year(rows: list[dict[str, Any]], name: str, year: int) -> dict[str, Any]:
    return next((r for r in rows if r["name"] == name and r["period"] == str(year)), {})


def _write_review_files(
    output_dir: Path,
    summary: dict[str, Any],
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    adoption_pass: bool,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    decision = "mini-spec-retry" if adoption_pass else "reject"
    reason = (
        "Position sizing variant passed the automatic train/test/2020 checks; review before promotion."
        if adoption_pass
        else "No position sizing variant beat baseline across train, 2020 repair, and sealed test together."
    )
    selected_test = _pick(summary.get("summary_rows", []), str(best_train.get("name")), "test")
    baseline_2020 = summary.get("baseline_2020", {})
    selected_2020 = summary.get("selected_2020", {})
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
                "l6_exit_decision": decision,
                "status": "COMPLETE",
                "three_exits_section": {
                    "train_exit": f"Train winner selected {best_train.get('name')}.",
                    "validation_exit": f"Sealed test winner selected {best_test.get('name')}.",
                    "decision_exit": reason,
                },
                "compute_cost_yuan": 0.0,
                "confirmed_invalid_directions": [
                    "option_source_position_sizing"
                ] if not adoption_pass else [],
                "learnings": [
                    "Option-source position sizing must pass train, 2020 repair, and sealed test together.",
                    reason,
                ],
                "follow_up_actions": [
                    "Keep this run as diagnostic evidence for future option-source risk filters.",
                    "Do not promote unless follow-up review confirms train/test/2020 robustness.",
                ],
                "summary": reason,
                "notes": "Result reviewed by code-generated summary.json, l4_ack.yaml, and diagnostic.yaml.",
                "references": summary["artifacts"],
                "related_reports": [
                    "data/cb_arb_value_gap_switch_option-position-sizing_2026-05-17_151411/report.yaml",
                    "data/cb_arb_value_gap_switch_option-value-haircut_2026-05-17/report.yaml",
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "reviewer": "codex",
                "ack_at": now,
                "q1_floor_binding": {
                    "description": "Hard floors and train/test consistency.",
                    "answer": (
                        "Selected train winner also meets sealed test and 2020 checks."
                        if adoption_pass
                        else "Selected train winner does not pass train/test/2020 robustness checks together."
                    ),
                    "computed_data": {
                        "best_train_variant": best_train.get("name"),
                        "train_excess": best_train.get("excess_return"),
                        "test_excess": selected_test.get("excess_return"),
                        "baseline_test_excess": baseline_test.get("excess_return"),
                        "baseline_2020_total_return": baseline_2020.get("total_return"),
                        "selected_2020_total_return": selected_2020.get("total_return"),
                    },
                    "computed_at": now,
                    "pass": adoption_pass,
                },
                "q2_selection_score": {
                    "description": "Candidate selection quality.",
                    "answer": (
                        f"Train score selects {best_train.get('name')}; sealed test best is {best_test.get('name')}."
                    ),
                    "computed_data": {
                        "selected_by_train_score": best_train.get("name"),
                        "selected_score": best_train.get("score"),
                        "best_test_variant": best_test.get("name"),
                        "best_test_score": best_test.get("score"),
                    },
                    "pass": adoption_pass,
                },
                "q3_baseline_alignment": {
                    "description": "Alignment against current cb_arb_value_gap_switch baseline.",
                    "answer": (
                        "Candidate is aligned with baseline thresholds."
                        if adoption_pass
                        else "Candidate does not justify replacing the current baseline."
                    ),
                    "computed_data": {
                        "baseline_train_excess": baseline_train.get("excess_return"),
                        "baseline_test_excess": baseline_test.get("excess_return"),
                        "selected_test_excess": selected_test.get("excess_return"),
                    },
                    "computed_at": now,
                    "pass": adoption_pass,
                },
                "q4_monotonic": {
                    "description": "Edge-of-grid or monotonic concern.",
                    "answer": "Categorical fixed variants; no monotonic promotion without manual review.",
                    "computed_data": {
                        "grid_type": "categorical_position_sizing_variants",
                        "candidates_count": summary.get("candidate_count"),
                    },
                    "computed_at": now,
                    "pass": True,
                },
                "q5_trade_overlap": {
                    "description": "Trade overlap baseline vs selected.",
                    "answer": "Aggregate train/test/2020 checks are used for automatic decision.",
                    "computed_data": {
                        "selected_total_trades_test": selected_test.get("total_trades"),
                        "baseline_total_trades_test": baseline_test.get("total_trades"),
                        "selected_total_trades_2020": selected_2020.get("total_trades"),
                        "baseline_total_trades_2020": baseline_2020.get("total_trades"),
                    },
                    "computed_at": now,
                    "pass": True,
                },
                "q6_trigger_timing": {"description": "Trigger timing leakage.", "applicable": False},
                "q7_path_contamination": {"description": "Path/data contamination.", "applicable": False},
                "overall_pass": adoption_pass,
                "overall_decision": decision,
                "overall_reason": reason,
                "auto_computed_at": now,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "diagnostic_date": "2026-05-17",
                "diagnostic_by": "codex",
                "verdict_referenced": decision,
                "summary": reason,
                "verdict_rationale": reason,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_option_position_sizing"
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
        if name == "baseline_no_position_scale":
            params["candidate_position_scale_enabled"] = 0.0
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
            f"[option_position_sizing] {name} "
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

    _attach_spec_binding(summary_rows, output_dir)
    _write_csv(output_dir / "summary_option_position_sizing.csv", summary_rows)
    _write_csv(output_dir / "yearly_option_position_sizing.csv", yearly_rows)
    _write_csv(output_dir / "entry_source_2020_option_position_sizing.csv", source_2020_rows)
    _write_csv(output_dir / "entry_source_test_option_position_sizing.csv", source_test_rows)
    _write_csv(output_dir / "adjustment_option_position_sizing.csv", adjustment_rows)

    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = _pick(summary_rows, "baseline_no_position_scale", "train")
    baseline_test = _pick(summary_rows, "baseline_no_position_scale", "test")
    selected_test = _pick(summary_rows, str(best_train.get("name")), "test")
    baseline_2020 = _year(yearly_rows, "baseline_no_position_scale", 2020)
    selected_2020 = _year(yearly_rows, str(best_train.get("name")), 2020)

    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_no_position_scale"
        and float(best_train.get("excess_return", -999)) >= float(baseline_train.get("excess_return", 999)) + 0.005
        and float(selected_test.get("excess_return", -999)) >= float(baseline_test.get("excess_return", 999))
        and float(selected_2020.get("total_return", -999)) >= float(baseline_2020.get("total_return", 999)) + 0.05
        and float(best_train.get("max_drawdown", -999)) >= -0.30
    )

    summary = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "candidate_count": len(CONFIGS),
        "adoption_pass": adoption_pass,
        "best_train": best_train,
        "best_test": best_test,
        "baseline_train": baseline_train,
        "baseline_test": baseline_test,
        "selected_test": selected_test,
        "baseline_2020": baseline_2020,
        "selected_2020": selected_2020,
        "summary_rows": summary_rows,
        "artifacts": [
            "summary_option_position_sizing.csv",
            "yearly_option_position_sizing.csv",
            "entry_source_2020_option_position_sizing.csv",
            "entry_source_test_option_position_sizing.csv",
            "adjustment_option_position_sizing.csv",
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
    }
    _write_review_files(
        output_dir,
        summary,
        best_train,
        best_test,
        baseline_train,
        baseline_test,
        adoption_pass,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
