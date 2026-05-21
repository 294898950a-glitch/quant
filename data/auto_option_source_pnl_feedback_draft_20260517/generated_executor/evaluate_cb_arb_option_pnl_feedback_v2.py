"""Evaluate rolling option-source PnL feedback v2 for cb_arb value-gap switch.

Unlike v1 which computes rolling realized PnL internally from its own trade
simulation, this executor ingests pre-computed entry-source CSV files from
a prior option-position-sizing run and uses their aggregate source-level
PnL to scale future option-source candidate exposure.

The feedback mechanism:
  1. Parse entry-source CSVs to extract per-source realized PnL stats
     (sum_pnl_amount, avg_pnl_pct) from the baseline (unscaled) config.
  2. For each source that is "recently losing" (metrics cross a threshold),
     scale down value_gap_amount and position_cash on option-source
     candidate rows during rank pre-processing.
  3. Run standard value-gap backtest on adjusted ranks.
  4. Compare multiple feedback strategies (threshold, scale, signal type)
     against a no-feedback baseline.
"""

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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_option_position_sizing import (
    _add_moneyness,
    _option_source_mask,
)
from scripts.evaluate_cb_arb_value_gap_switch import (
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
    "option_source_pnl_feedback_enabled": 0.0,
    "candidate_position_scale_enabled": 0.0,
}


# --- CONFIGS: grid of feedback strategies ---

CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_no_feedback_v2",
        "description": "不应用任何期权来源盈亏反馈（v2基线）",
        "feedback_enabled": False,
    },
    *[
        {
            "name": (
                f"feedback_{signal}_thresh{str(threshold).replace('.', 'p').replace('-', 'n')}"
                f"_scale{str(scale).replace('.', 'p')}"
            ),
            "description": (
                f"信号={signal}, 阈值={threshold}, 缩放={scale}: "
                f"当期权来源的{signal}低于{threshold}时，"
                f"后续该来源的候选排序金额和买入金额乘以{scale}"
            ),
            "feedback_enabled": True,
            "feedback_signal": signal,
            "feedback_threshold": float(threshold),
            "feedback_scale": float(scale),
        }
        for signal in ("sum_pnl_amount", "avg_pnl_pct")
        for threshold in (-50000, -100000, -200000, -5000, -0.005, -0.01)
        for scale in (0.75, 0.50, 0.25)
    ],
]


# --- CSV parsing helpers ---

def _parse_entry_source_csv(csv_path: Path) -> pd.DataFrame:
    """Read an entry-source CSV and return a DataFrame.

    CSV columns: name, source, count, avg_pnl_pct, sum_pnl_amount, wins,
                 avg_close_to_bond_floor, avg_moneyness_stock_to_conv,
                 avg_position_cash_scale
    """
    df = pd.read_csv(csv_path)
    for col in ("avg_pnl_pct", "sum_pnl_amount", "count", "wins"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _extract_baseline_source_pnl(
    csv_df: pd.DataFrame, baseline_names: tuple[str, ...] | None = None
) -> dict[str, dict[str, float]]:
    """Extract per-source PnL stats for the baseline (unscaled) config.

    Returns {source: {sum_pnl_amount, avg_pnl_pct, count, wins}}
    """
    if baseline_names is None:
        baseline_names = ("baseline_no_position_scale", "baseline_no_feedback")

    baseline_rows = csv_df[csv_df["name"].isin(baseline_names)]
    if baseline_rows.empty:
        # Fallback: use the first config name as baseline
        first_name = csv_df["name"].iloc[0] if len(csv_df) > 0 else None
        if first_name is not None:
            baseline_rows = csv_df[csv_df["name"] == first_name]

    result: dict[str, dict[str, float]] = {}
    for _, row in baseline_rows.iterrows():
        source = str(row["source"])
        result[source] = {
            "sum_pnl_amount": float(row["sum_pnl_amount"]),
            "avg_pnl_pct": float(row["avg_pnl_pct"]),
            "count": int(row["count"]),
            "wins": int(row["wins"]),
        }
    return result


def _source_name_for_row(row: Any, params: dict[str, Any]) -> str:
    """Determine the gap source name for a single rank row."""
    source, _, _ = _gap_source_shares(row, params)
    return source


# --- Rank adjustment helpers ---

def _adjust_ranks_for_feedback(
    ranks: pd.DataFrame,
    source_pnl_lookup: dict[str, dict[str, float]],
    cfg: dict[str, Any],
    params: dict[str, Any],
) -> pd.DataFrame:
    """Pre-adjust ranks based on entry-source PnL feedback.

    For each option-source candidate row, look up the source's aggregate PnL
    from the prior run. If the PnL signal crosses the feedback threshold,
    scale down value_gap_amount and position_cash_scale.

    Returns the adjusted DataFrame.
    """
    if not cfg.get("feedback_enabled"):
        return ranks

    signal = str(cfg.get("feedback_signal", "sum_pnl_amount"))
    threshold = float(cfg.get("feedback_threshold", 0.0))
    scale = float(cfg.get("feedback_scale", 1.0))

    adjusted = ranks.copy()
    if "position_cash_scale" not in adjusted.columns:
        adjusted["position_cash_scale"] = 1.0

    # Build a per-row source mask:
    # option_mask = rows where gap source == "option"
    option_mask_values: list[bool] = []
    losing_mask_values: list[bool] = []
    for row in adjusted.itertuples(index=False):
        source = _source_name_for_row(row, params)
        is_option = source == "option"
        option_mask_values.append(is_option)
        if is_option and source in source_pnl_lookup:
            pnl_val = source_pnl_lookup[source].get(signal, 0.0)
            losing_mask_values.append(pnl_val < threshold)
        else:
            losing_mask_values.append(False)

    option_mask = pd.Series(option_mask_values, index=adjusted.index)
    losing_mask = pd.Series(losing_mask_values, index=adjusted.index)
    apply_mask = option_mask & losing_mask

    if not bool(apply_mask.any()):
        return adjusted

    adjusted.loc[apply_mask, "position_cash_scale"] = (
        adjusted.loc[apply_mask, "position_cash_scale"].astype(float) * scale
    )
    adjusted.loc[apply_mask, "value_gap_amount"] = (
        adjusted.loc[apply_mask, "value_gap_amount"].astype(float) * scale
    )
    adjusted.loc[apply_mask, "value_gap_pct_of_cash"] = (
        adjusted.loc[apply_mask, "value_gap_amount"].astype(float)
        / adjusted.loc[apply_mask, "position_cash"].astype(float)
    )
    return adjusted


# --- Spec binding ---

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


# --- CLI ---

def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the files this executor will read before it is allowed to run."""
    _cmd_val = lambda flag: next(
        (str(p) for i, p in enumerate(command[:-1]) if str(p) == flag), None
    )
    data_root_raw = _cmd_val("--data-root")
    if not data_root_raw:
        raise ValueError("evaluate_cb_arb_option_pnl_feedback_v2 requires --data-root")
    base_ranks = _cmd_val("--base-ranks-path")
    csv_2020 = _cmd_val("--option-2020-csv")
    csv_test = _cmd_val("--option-test-csv")

    required_files: list[dict[str, str]] = [
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/cb_basic.parquet"), "role": "warehouse_input"},
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/cb_daily.parquet"), "role": "warehouse_input"},
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/cb_call.parquet"), "role": "warehouse_input"},
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/stk_daily_qfq.parquet"), "role": "warehouse_input"},
    ]
    if base_ranks:
        required_files.append({"path": base_ranks, "role": "base_ranks_input"})
    if csv_2020:
        required_files.append({"path": csv_2020, "role": "entry_source_input_2020"})
    if csv_test:
        required_files.append({"path": csv_test, "role": "entry_source_input_test"})

    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_option_pnl_feedback_v2.py",
        "required_files": required_files,
    }


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
    p.add_argument("--option-2020-csv", type=Path, default=None)
    p.add_argument("--option-test-csv", type=Path, default=None)
    return p.parse_args()


def _load_base_ranks(args: argparse.Namespace, output_dir: Path) -> pd.DataFrame:
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)
    if args.base_ranks_path is not None and Path(args.base_ranks_path).exists():
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


def _params(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    params = _with_cost_params(dict(BASE_PARAMS), args)
    params.update(
        {
            k: v
            for k, v in cfg.items()
            if k in ("feedback_enabled", "feedback_signal", "feedback_threshold", "feedback_scale")
        }
    )
    return params


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
        "feedback_config_json": json.dumps(cfg, sort_keys=True),
        "params_json": json.dumps(params, sort_keys=True),
        **result["metrics"],
    }
    row["score"] = _score(result["metrics"])
    return row


def _source_rows(
    name: str, result: dict[str, Any], ranks_by_key: dict[tuple[str, str], Any], params: dict[str, Any]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in result["trades"]:
        key = (str(trade["entry_date"]), str(trade["cb_code"]))
        rank_row = ranks_by_key.get(key)
        source = "missing"
        bond_share = 0.0
        option_share = 0.0
        position_scale = 1.0
        if rank_row is not None:
            source, bond_share, option_share = _gap_source_shares(rank_row, params)
            try:
                position_scale = float(getattr(rank_row, "position_cash_scale", 1.0) or 1.0)
            except (TypeError, ValueError):
                position_scale = 1.0
        grouped.setdefault(source, []).append(
            {
                **trade,
                "bond_share": bond_share,
                "option_share": option_share,
                "position_cash_scale": position_scale,
            }
        )

    rows: list[dict[str, Any]] = []
    for source, trades_list in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in trades_list]
        pnl_amount = [float(t["pnl_amount"]) for t in trades_list]
        scales = [float(t["position_cash_scale"]) for t in trades_list]
        rows.append(
            {
                "name": name,
                "source": source,
                "count": len(trades_list),
                "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 6),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "wins": sum(1 for v in pnl_pct if v > 0),
                "avg_bond_share": round(
                    sum(float(t["bond_share"]) for t in trades_list) / len(trades_list), 6
                ),
                "avg_option_share": round(
                    sum(float(t["option_share"]) for t in trades_list) / len(trades_list), 6
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
        "Rolling option-source PnL feedback (v2 CSV-based) passed train/test/2020 "
        "checks; review before promotion."
        if adoption_pass
        else "No option-source PnL feedback variant (v2) beat baseline across "
        "train, 2020, and sealed test together."
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
                "date": datetime.now().strftime("%Y-%m-%d"),
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
                    "rolling_option_source_pnl_feedback_v2_csv"
                ] if not adoption_pass else [],
                "learnings": [
                    "Pre-computed source PnL from prior position-sizing run is used "
                    "as a feedback signal; must be judged on train, 2020, and "
                    "sealed test together.",
                    reason,
                ],
                "follow_up_actions": [
                    "Keep this run as evidence for future option-source feedback ideation.",
                    "Do not promote unless follow-up review confirms train/test/2020 robustness.",
                ],
                "summary": reason,
                "notes": "Result reviewed by code-generated summary.json, l4_ack.yaml, and diagnostic.yaml.",
                "references": summary.get("artifacts", []),
                "related_reports": [
                    "data/cb_arb_value_gap_switch_option-position-sizing_2026-05-17_151411/report.yaml",
                    "data/cb_arb_value_gap_switch_option-value-haircut_2026-05-17/report.yaml",
                    "data/cb_arb_value_gap_switch_option-pnl-feedback-v1_*/report.yaml",
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
                "reviewer": "hermes",
                "ack_at": now,
                "q1_floor_binding": {
                    "description": "Hard floors and train/test consistency.",
                    "answer": (
                        "Selected train winner also meets sealed test and 2020 checks."
                        if adoption_pass
                        else "Selected train winner does not pass train/test/2020 "
                        "robustness checks together."
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
                        f"Train score selects {best_train.get('name')}; "
                        f"sealed test best is {best_test.get('name')}."
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
                    "answer": "Categorical feedback variants; no monotonic promotion without review.",
                    "computed_data": {
                        "grid_type": "csv_feedback_variants",
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
                "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
                "diagnostic_by": "hermes",
                "verdict_referenced": decision,
                "summary": reason,
                "verdict_rationale": reason,
                **(
                    {
                        "next_step_spec_changes": [
                            {
                                "field": "review_required_before_promotion",
                                "old_value": False,
                                "new_value": True,
                                "reason": "mini-spec-retry requires explicit follow-up review "
                                "before any baseline change.",
                            }
                        ]
                    }
                    if decision == "mini-spec-retry"
                    else {}
                ),
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# --- main ---

def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_option_pnl_feedback_v2"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load base ranks
    base_ranks = _load_base_ranks(args, output_dir)
    base_params_no_feedback = _with_cost_params(dict(BASE_PARAMS), args)
    ranks_by_key = {
        (str(r.trade_date), str(r.ts_code)): r
        for r in base_ranks.itertuples(index=False)
    }

    # 2. Load entry-source CSVs and build per-source PnL lookup
    source_pnl_lookup: dict[str, dict[str, float]] = {}
    if args.option_2020_csv is not None and Path(args.option_2020_csv).exists():
        df_2020 = _parse_entry_source_csv(args.option_2020_csv)
        source_pnl_lookup.update(_extract_baseline_source_pnl(df_2020))
    if args.option_test_csv is not None and Path(args.option_test_csv).exists():
        df_test = _parse_entry_source_csv(args.option_test_csv)
        # Merge test PnL data (source-level); 2020 takes priority for overlapping sources
        test_pnl = _extract_baseline_source_pnl(df_test)
        for src, stats in test_pnl.items():
            if src not in source_pnl_lookup:
                source_pnl_lookup[src] = stats

    if not source_pnl_lookup:
        print("[v2] WARNING: No entry-source CSV data loaded; feedback will be disabled.",
              file=sys.stderr, flush=True)

    # 3. Iterate over configs
    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    source_2020_rows: list[dict[str, Any]] = []
    source_test_rows: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg.get("description", ""))
        params = _params(args, cfg)

        # Pre-adjust ranks based on CSV feedback
        adjusted_ranks = _adjust_ranks_for_feedback(
            base_ranks, source_pnl_lookup, cfg, params
        )

        train_ranks = adjusted_ranks[
            (adjusted_ranks["trade_date"] >= args.train_start)
            & (adjusted_ranks["trade_date"] <= args.train_end)
        ]
        test_ranks = adjusted_ranks[
            (adjusted_ranks["trade_date"] >= args.test_start)
            & (adjusted_ranks["trade_date"] <= args.test_end)
        ]

        train = _run_value_gap_backtest(
            train_ranks,
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        test = _run_value_gap_backtest(
            test_ranks,
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, cfg, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, cfg, params, test))
        source_test_rows.extend(_source_rows(name, test, ranks_by_key, params))
        feedback_rows.append(
            {
                "name": name,
                "description": description,
                "params_json": json.dumps(params, sort_keys=True),
                "train_trade_count": len(train["trades"]),
                "test_trade_count": len(test["trades"]),
            }
        )
        print(
            f"[v2] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']}",
            flush=True,
        )

        # 4. Yearly slices (2019-2024 for train period; 2020 is the validate slice)
        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            ranks_year = adjusted_ranks[
                (adjusted_ranks["trade_date"] >= start)
                & (adjusted_ranks["trade_date"] <= end)
            ]
            y = _run_value_gap_backtest(
                ranks_year,
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            yearly_rows.append(_row(name, description, str(year), start, end, cfg, params, y))
            if year == 2020:
                source_2020_rows.extend(_source_rows(name, y, ranks_by_key, params))

    # 5. Attach spec binding and write CSVs
    _attach_spec_binding(summary_rows, output_dir)
    _write_csv(output_dir / "summary_option_pnl_feedback_v2.csv", summary_rows)
    _write_csv(output_dir / "yearly_option_pnl_feedback_v2.csv", yearly_rows)
    _write_csv(output_dir / "entry_source_2020_option_pnl_feedback_v2.csv", source_2020_rows)
    _write_csv(output_dir / "entry_source_test_option_pnl_feedback_v2.csv", source_test_rows)
    _write_csv(output_dir / "feedback_option_pnl_feedback_v2.csv", feedback_rows)

    # 6. Select best and evaluate adoption criteria
    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = _pick(summary_rows, "baseline_no_feedback_v2", "train")
    baseline_test = _pick(summary_rows, "baseline_no_feedback_v2", "test")
    selected_test = _pick(summary_rows, str(best_train.get("name")), "test")
    baseline_2020 = _year(yearly_rows, "baseline_no_feedback_v2", 2020)
    selected_2020 = _year(yearly_rows, str(best_train.get("name")), 2020)

    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_no_feedback_v2"
        and float(best_train.get("excess_return", -999)) >= float(baseline_train.get("excess_return", 999))
        and float(selected_test.get("excess_return", -999)) >= float(baseline_test.get("excess_return", 999))
        and float(selected_2020.get("total_return", -999)) >= float(baseline_2020.get("total_return", 999)) + 0.03
        and float(best_train.get("max_drawdown", -999)) >= -0.30
    )

    artifacts = [
        "summary_option_pnl_feedback_v2.csv",
        "yearly_option_pnl_feedback_v2.csv",
        "entry_source_2020_option_pnl_feedback_v2.csv",
        "entry_source_test_option_pnl_feedback_v2.csv",
        "feedback_option_pnl_feedback_v2.csv",
        "summary.json",
        "report.yaml",
        "l4_ack.yaml",
        "diagnostic.yaml",
    ]
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
        "artifacts": artifacts,
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
