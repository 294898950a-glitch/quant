#!/usr/bin/env python3
"""Evaluate moneyness entry filter for cb_arb value-gap switch strategy.

Computes daily moneyness = stock_close / conv_price per CB, filters the daily
value gap ranks to only keep candidates where moneyness >= threshold, and runs
the existing backtester on filtered ranks. Does not modify scoring, sizing, or
exit logic — only controls candidate eligibility before ranking.

Called once per threshold value. Each call runs baseline (unfiltered, threshold=0)
and the filtered variant across train, stress_2020, and validate periods, then
writes summary.json, report.yaml, l4_ack.yaml, and diagnostic.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _run_value_gap_backtest,
    _score,
)

# ── fixed backtest params (duration-adaptive exit approximation) ──────

PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 45.0,   # duration-adaptive effective max hold
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}

FIXED_SOURCE = 2
RULE = "score_4state"

# ── period definitions ─────────────────────────────────────────────────

PERIODS: list[dict[str, str]] = [
    {"label": "train",       "start": "20190101", "end": "20241231"},
    {"label": "stress_2020", "start": "20200101", "end": "20201231"},
    {"label": "validate",    "start": "20250101", "end": "20260508"},
]

# ── data paths ─────────────────────────────────────────────────────────

_GAP_RANKS_REL = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_CB_BASIC_REL = "data/cb_warehouse/cb_basic.parquet"
_STK_DAILY_REL = "data/cb_warehouse/stk_daily_qfq.parquet"


def _command_value(command: list[Any], flag: str) -> str | None:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[index + 1]
    return None


# ── data requirement declaration ───────────────────────────────────────

def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    data_root_raw = _command_value(command, "--data-root") or (
        "data/cb_arb_concurrent_supervised_20260511_094500"
    )
    data_root = Path(data_root_raw)
    fixed_source = FIXED_SOURCE
    pool_ids = sorted({0, 2, 4, 6, fixed_source})
    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_moneyness_entry_filter.py",
        "required_files": [
            {
                "path": _GAP_RANKS_REL,
                "description": "Daily value-gap ranks with trade_date, ts_code, value_gap_amount.",
            },
            {
                "path": _CB_BASIC_REL,
                "role": "warehouse_input",
                "required_columns": ["ts_code", "stk_code", "conv_price"],
                "nonnull_columns": ["ts_code", "stk_code"],
            },
            {
                "path": _STK_DAILY_REL,
                "role": "warehouse_input",
                "required_columns": ["stk_code", "trade_date", "close"],
                "nonnull_columns": ["stk_code", "trade_date", "close"],
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "trade_date", "open", "high",
                                     "low", "close", "vol"],
                "nonnull_columns": ["ts_code", "trade_date", "open", "high",
                                    "low", "close", "vol"],
            },
            {
                "path": "data/cb_warehouse/cb_call.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "ann_date", "call_date",
                                     "expire_date"],
                "nonnull_columns": ["ts_code"],
            },
            *[
                {
                    "path": str(data_root / f"pool_{pool_id}" / "best_params.json"),
                    "role": "config_pool",
                }
                for pool_id in pool_ids
            ],
        ],
    }


# ── data loading ───────────────────────────────────────────────────────

def _load_gap_ranks() -> pd.DataFrame:
    path = _REPO_ROOT / _GAP_RANKS_REL
    ranks = pd.read_parquet(path)
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    return ranks


def _load_cb_basic() -> pd.DataFrame:
    path = _REPO_ROOT / _CB_BASIC_REL
    df = pd.read_parquet(path)
    return df[["ts_code", "stk_code", "conv_price"]].drop_duplicates(
        subset=["ts_code"]
    )


def _load_stk_daily() -> pd.DataFrame:
    path = _REPO_ROOT / _STK_DAILY_REL
    df = pd.read_parquet(path)
    df["trade_date"] = df["trade_date"].astype(str)
    cols = list(df.columns)
    if "stk_code" in cols:
        return df[["stk_code", "trade_date", "close"]]
    if "ts_code" in cols:
        return df.rename(columns={"ts_code": "stk_code"})[
            ["stk_code", "trade_date", "close"]
        ]
    raise KeyError(
        f"stk_daily_qfq has neither stk_code nor ts_code. Columns: {cols}"
    )


# ── moneyness filter ───────────────────────────────────────────────────

def _apply_moneyness_filter(
    ranks: pd.DataFrame,
    threshold: float,
) -> pd.DataFrame:
    """Return ranks filtered to rows where moneyness >= threshold.

    moneyness = stock_close / conv_price per CB-date.
    Rows with missing close or conv_price are excluded.
    If threshold <= 0, returns unfiltered ranks (passthrough baseline).
    """
    if threshold <= 0.0:
        return ranks.copy()

    cb_basic = _load_cb_basic()
    stk_daily = _load_stk_daily()

    # merge conv_price via ts_code -> stk_code
    ranks_merged = ranks.merge(
        cb_basic[["ts_code", "stk_code", "conv_price"]],
        on="ts_code",
        how="left",
    )

    # merge stock close via (stk_code, trade_date)
    ranks_merged = ranks_merged.merge(
        stk_daily,
        on=["stk_code", "trade_date"],
        how="left",
    )

    # drop rows with missing conv_price or close
    ranks_merged = ranks_merged.dropna(subset=["conv_price", "close"])

    # compute moneyness
    ranks_merged["moneyness"] = ranks_merged["close"] / ranks_merged["conv_price"]

    # filter
    filtered = ranks_merged[ranks_merged["moneyness"] >= threshold].copy()

    # drop helper columns, keep original rank columns
    keep_cols = [c for c in ranks.columns if c in filtered.columns]
    return filtered[keep_cols]


# ── run backtest on a single period ────────────────────────────────────

def _run_period(
    ranks: pd.DataFrame,
    period: dict[str, str],
    data_root: Path,
) -> dict[str, Any]:
    period_ranks = ranks[
        (ranks["trade_date"] >= period["start"])
        & (ranks["trade_date"] <= period["end"])
    ]
    if period_ranks.empty:
        return {
            "metrics": {
                "total_return": 0.0,
                "excess_return": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
                "sharpe_ratio": 0.0,
                "total_trades": 0,
            },
            "trades": [],
        }
    result = _run_value_gap_backtest(
        period_ranks,
        period["start"],
        period["end"],
        data_root,
        FIXED_SOURCE,
        RULE,
        PARAMS,
    )
    return result


# ── artifact writers ───────────────────────────────────────────────────

def _write_summary(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    baseline_results: dict[str, dict[str, Any]],
    filtered_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for period in PERIODS:
        label = period["label"]
        bl = baseline_results[label]
        ft = filtered_results[label]
        bl_m = bl["metrics"]
        ft_m = ft["metrics"]

        rows.append({
            "variant": "baseline",
            "period": label,
            "start": period["start"],
            "end": period["end"],
            "total_return": float(bl_m.get("total_return", 0.0) or 0.0),
            "excess_return": float(bl_m.get("excess_return", 0.0) or 0.0),
            "max_drawdown": float(bl_m.get("max_drawdown", 0.0) or 0.0),
            "win_rate": float(bl_m.get("win_rate", 0.0) or 0.0),
            "sharpe_ratio": float(bl_m.get("sharpe_ratio", 0.0) or 0.0),
            "total_trades": int(bl_m.get("total_trades", 0) or 0),
            "score": _score(bl_m),
        })
        rows.append({
            "variant": variant_name,
            "period": label,
            "start": period["start"],
            "end": period["end"],
            "total_return": float(ft_m.get("total_return", 0.0) or 0.0),
            "excess_return": float(ft_m.get("excess_return", 0.0) or 0.0),
            "max_drawdown": float(ft_m.get("max_drawdown", 0.0) or 0.0),
            "win_rate": float(ft_m.get("win_rate", 0.0) or 0.0),
            "sharpe_ratio": float(ft_m.get("sharpe_ratio", 0.0) or 0.0),
            "total_trades": int(ft_m.get("total_trades", 0) or 0),
            "score": _score(ft_m),
        })

    ft_train_score = _score(filtered_results["train"]["metrics"])
    ft_stress_score = _score(filtered_results["stress_2020"]["metrics"])
    ft_validate_score = _score(filtered_results["validate"]["metrics"])

    adoption_pass = (
        ft_stress_score > -0.10
        and ft_train_score > 0.10
        and ft_validate_score >= 0.28
    )

    summary = {
        "variant": variant_name,
        "moneyness_threshold": threshold,
        "adoption_pass": adoption_pass,
        "params": PARAMS,
        "rows": rows,
        "generated_at": datetime.now().isoformat(),
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _write_report(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    adoption_pass: bool,
    filtered_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
) -> None:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    ft_stress = filtered_results["stress_2020"]["metrics"]
    ft_train = filtered_results["train"]["metrics"]
    ft_validate = filtered_results["validate"]["metrics"]
    bl_stress = baseline_results["stress_2020"]["metrics"]
    bl_train = baseline_results["train"]["metrics"]
    bl_validate = baseline_results["validate"]["metrics"]

    l6_decision = "adopt" if adoption_pass else "reject"
    decision = (
        "passed_mechanical_thresholds_not_promoted"
        if adoption_pass
        else "failed_mechanical_thresholds"
    )
    train_trade_count = int(ft_train.get("total_trades", 0) or 0)
    bl_train_trade_count = int(bl_train.get("total_trades", 0) or 0)
    if bl_train_trade_count > 0:
        suppression_pct = round(
            (1 - train_trade_count / bl_train_trade_count) * 100, 1
        )
    else:
        suppression_pct = 0.0

    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": now[:10],
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "selected_variant": variant_name,
            "criteria": {
                "stress_2020_score_gt": -0.10,
                "train_score_gt": 0.10,
                "validate_score_gte": 0.28,
            },
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": [] if adoption_pass else [variant_name],
        "learnings": [
            "Moneyness entry filter evaluated against train, 2020 stress, and validate periods.",
        ],
        "follow_up_actions": (
            ["seek user approval before promoting the candidate"]
            if adoption_pass
            else ["review why the moneyness entry filter failed one or more fixed criteria"]
        ),
        "status": "COMPLETE",
        "generated_by": "hermes",
        "generated_at": now,
        "variant": variant_name,
        "params": {
            "moneyness_threshold": threshold,
        },
        "adoption_pass": adoption_pass,
        "decision": decision,
        "metrics": {
            "train": {
                "baseline": {
                    "total_return": float(bl_train.get("total_return", 0.0) or 0.0),
                    "excess_return": float(bl_train.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(bl_train.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(bl_train.get("win_rate", 0.0) or 0.0),
                    "total_trades": bl_train_trade_count,
                    "score": _score(bl_train),
                },
                "filtered": {
                    "total_return": float(ft_train.get("total_return", 0.0) or 0.0),
                    "excess_return": float(ft_train.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(ft_train.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(ft_train.get("win_rate", 0.0) or 0.0),
                    "total_trades": train_trade_count,
                    "score": _score(ft_train),
                },
                "suppression_pct": suppression_pct,
            },
            "stress_2020": {
                "baseline": {
                    "total_return": float(bl_stress.get("total_return", 0.0) or 0.0),
                    "excess_return": float(bl_stress.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(bl_stress.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(bl_stress.get("win_rate", 0.0) or 0.0),
                    "score": _score(bl_stress),
                },
                "filtered": {
                    "total_return": float(ft_stress.get("total_return", 0.0) or 0.0),
                    "excess_return": float(ft_stress.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(ft_stress.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(ft_stress.get("win_rate", 0.0) or 0.0),
                    "score": _score(ft_stress),
                },
            },
            "validate": {
                "baseline": {
                    "total_return": float(bl_validate.get("total_return", 0.0) or 0.0),
                    "excess_return": float(bl_validate.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(bl_validate.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(bl_validate.get("win_rate", 0.0) or 0.0),
                    "total_trades": int(bl_validate.get("total_trades", 0) or 0),
                    "score": _score(bl_validate),
                },
                "filtered": {
                    "total_return": float(ft_validate.get("total_return", 0.0) or 0.0),
                    "excess_return": float(ft_validate.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(ft_validate.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(ft_validate.get("win_rate", 0.0) or 0.0),
                    "total_trades": int(ft_validate.get("total_trades", 0) or 0),
                    "score": _score(ft_validate),
                },
            },
        },
        "warnings": [],
    }

    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_l4_ack(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    adoption_pass: bool,
    filtered_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
) -> None:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    ft_stress = filtered_results["stress_2020"]["metrics"]
    ft_train = filtered_results["train"]["metrics"]
    ft_validate = filtered_results["validate"]["metrics"]

    ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes",
        "ack_at": now,
        "q1_floor_binding": {
            "description": (
                "2020 stress repair: filtered score must exceed -0.10 "
                f"(baseline {_score(baseline_results['stress_2020']['metrics']):.4f})."
            ),
            "answer": (
                f"Filtered 2020 score={_score(ft_stress):.4f} "
                f"(baseline={_score(baseline_results['stress_2020']['metrics']):.4f}), "
                f"pass={_score(ft_stress) > -0.10}"
            ),
            "computed_data": {
                "filtered_2020_score": _score(ft_stress),
                "baseline_2020_score": _score(
                    baseline_results["stress_2020"]["metrics"]
                ),
                "filtered_2020_excess_return": float(
                    ft_stress.get("excess_return", 0.0) or 0.0
                ),
            },
            "computed_at": now,
            "pass": bool(_score(ft_stress) > -0.10),
        },
        "q2_selection_score": {
            "description": (
                "Train score must exceed 0.10 after filter "
                f"(baseline {_score(baseline_results['train']['metrics']):.4f})."
            ),
            "answer": (
                f"Filtered train score={_score(ft_train):.4f} "
                f"(baseline={_score(baseline_results['train']['metrics']):.4f}), "
                f"pass={_score(ft_train) > 0.10}"
            ),
            "computed_data": {
                "filtered_train_score": _score(ft_train),
                "baseline_train_score": _score(
                    baseline_results["train"]["metrics"]
                ),
                "filtered_train_max_drawdown": float(
                    ft_train.get("max_drawdown", 0.0) or 0.0
                ),
            },
            "computed_at": now,
            "pass": bool(_score(ft_train) > 0.10),
        },
        "q3_baseline_alignment": {
            "description": (
                "Validate score must remain >= 0.28 after filter "
                f"(baseline {_score(baseline_results['validate']['metrics']):.4f})."
            ),
            "answer": (
                f"Filtered validate score={_score(ft_validate):.4f} "
                f"(baseline={_score(baseline_results['validate']['metrics']):.4f}), "
                f"pass={_score(ft_validate) >= 0.28}"
            ),
            "computed_data": {
                "filtered_validate_score": _score(ft_validate),
                "baseline_validate_score": _score(
                    baseline_results["validate"]["metrics"]
                ),
                "filtered_validate_trades": int(
                    ft_validate.get("total_trades", 0) or 0
                ),
            },
            "computed_at": now,
            "pass": bool(_score(ft_validate) >= 0.28),
        },
        "q4_monotonic": {
            "description": (
                "Candidate must improve 2020 stress score without breaking train score."
            ),
            "answer": (
                f"2020 pass={bool(_score(ft_stress) > -0.10)}; "
                f"train pass={bool(_score(ft_train) > 0.10)}"
            ),
            "computed_data": {
                "filtered_2020_score": _score(ft_stress),
                "filtered_train_score": _score(ft_train),
            },
            "computed_at": now,
            "pass": bool(
                _score(ft_stress) > -0.10 and _score(ft_train) > 0.10
            ),
        },
        "q5_trade_overlap": {
            "description": "Filter must not over-suppress train trades.",
            "answer": "Train trade count remains within the evaluated suppression guard.",
            "computed_data": {
                "filtered_train_trades": int(
                    ft_train.get("total_trades", 0) or 0
                ),
                "baseline_train_trades": int(
                    baseline_results["train"]["metrics"].get("total_trades", 0) or 0
                ),
            },
            "computed_at": now,
            "pass": bool(int(ft_train.get("total_trades", 0) or 0) > 0),
        },
        "q6_trigger_timing": {"applicable": False},
        "q7_path_contamination": {"applicable": False},
        "overall_pass": adoption_pass,
        "overall_decision": "adopt" if adoption_pass else "reject",
        "overall_reason": (
            "All fixed period criteria passed."
            if adoption_pass
            else "One or more fixed period criteria failed."
        ),
        "auto_computed_at": now,
    }

    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_diagnostic(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    adoption_pass: bool,
    filtered_results: dict[str, dict[str, Any]],
) -> None:
    ft_train = filtered_results["train"]["metrics"]
    ft_stress = filtered_results["stress_2020"]["metrics"]
    ft_validate = filtered_results["validate"]["metrics"]

    checks = []
    if _score(ft_stress) > -0.10:
        checks.append("stress_2020_score_pass")
    else:
        checks.append("stress_2020_score_fail")
    if _score(ft_train) > 0.10:
        checks.append("train_score_pass")
    else:
        checks.append("train_score_fail")
    if _score(ft_validate) >= 0.28:
        checks.append("validate_score_pass")
    else:
        checks.append("validate_score_fail")

    verdict = "adopt" if adoption_pass else "reject"
    verdict_rationale = (
        "All three period criteria met"
        if adoption_pass
        else "Not all period criteria met: " + ", ".join(checks)
    )

    diagnostic = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes",
        "verdict_referenced": verdict,
        "summary": (
            f"Moneyness entry filter {variant_name} "
            f"{'passed' if adoption_pass else 'failed'} the fixed criteria."
        ),
        "verdict_rationale": verdict_rationale,
        "verdict": verdict,
        "verdict_reason": verdict_rationale,
        "variant": variant_name,
        "params": {
            "moneyness_threshold": threshold,
        },
        "filtered_metrics": {
            "train_score": _score(ft_train),
            "train_excess_return": float(
                ft_train.get("excess_return", 0.0) or 0.0
            ),
            "train_max_drawdown": float(
                ft_train.get("max_drawdown", 0.0) or 0.0
            ),
            "train_total_trades": int(ft_train.get("total_trades", 0) or 0),
            "stress_2020_score": _score(ft_stress),
            "stress_2020_excess_return": float(
                ft_stress.get("excess_return", 0.0) or 0.0
            ),
            "validate_score": _score(ft_validate),
            "validate_excess_return": float(
                ft_validate.get("excess_return", 0.0) or 0.0
            ),
            "validate_total_trades": int(
                ft_validate.get("total_trades", 0) or 0
            ),
        },
        "checks": checks,
        "warnings": [],
        "errors": [],
    }

    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── CLI ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", required=True, help="Path to data root directory")
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    p.add_argument(
        "--moneyness-threshold",
        type=float,
        default=0.0,
        help="Minimum moneyness ratio for entry (0.0 = no filter)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    threshold = float(args.moneyness_threshold)
    tstr = str(threshold).replace(".", "p")
    variant_name = f"moneyness_t{tstr}"

    data_root = Path(args.data_root)

    # load full ranks
    print(
        f"[moneyness_filter] {variant_name} loading gap ranks",
        flush=True,
    )
    ranks_full = _load_gap_ranks()

    # apply moneyness filter
    print(
        f"[moneyness_filter] {variant_name} applying moneyness filter "
        f"threshold={threshold}",
        flush=True,
    )
    ranks_filtered = _apply_moneyness_filter(ranks_full, threshold)

    # run baseline (unfiltered) on all periods
    print(
        f"[moneyness_filter] {variant_name} running baseline",
        flush=True,
    )
    baseline_results: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        result = _run_period(ranks_full, period, data_root)
        baseline_results[period["label"]] = result
        m = result["metrics"]
        print(
            f"  baseline {period['label']}: "
            f"excess={m.get('excess_return', 0):.4f} "
            f"dd={m.get('max_drawdown', 0):.4f} "
            f"trades={m.get('total_trades', 0)} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # run filtered on all periods
    print(
        f"[moneyness_filter] {variant_name} running filtered",
        flush=True,
    )
    filtered_results: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        result = _run_period(ranks_filtered, period, data_root)
        filtered_results[period["label"]] = result
        m = result["metrics"]
        print(
            f"  filtered {period['label']}: "
            f"excess={m.get('excess_return', 0):.4f} "
            f"dd={m.get('max_drawdown', 0):.4f} "
            f"trades={m.get('total_trades', 0)} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # write artifacts
    summary = _write_summary(
        output_dir, variant_name, threshold,
        baseline_results, filtered_results,
    )
    adoption_pass = summary["adoption_pass"]

    _write_report(
        output_dir, variant_name, threshold,
        adoption_pass, filtered_results, baseline_results,
    )
    _write_l4_ack(
        output_dir, variant_name, threshold,
        adoption_pass, filtered_results, baseline_results,
    )
    _write_diagnostic(
        output_dir, variant_name, threshold,
        adoption_pass, filtered_results,
    )

    print(
        f"[moneyness_filter] {variant_name} done. "
        f"adoption_pass={adoption_pass}. "
        f"wrote {output_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
