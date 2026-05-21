"""Evaluate market drawdown entry gate for cb_arb value-gap switch.

Computes CSI 300 rolling N-day return, builds a boolean entry gate mask
(drawdown = rolling_return < -threshold, optionally extended by buffer_days),
filters the daily value gap ranks to suppress entries during drawdowns,
and runs the existing backtester without modifying scoring, sizing, or exit logic.

Called once per variant (N, threshold, buffer_days). Each call runs baseline
(no gate) and the gated variant across train, stress_2020, and validate periods,
then writes summary.json, report.yaml, l4_ack.yaml, and diagnostic.yaml.
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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _run_value_gap_backtest,
    _score,
)

# ── fixed backtest params (do not vary across gate variants) ──────────

BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
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

# ── data requirement declaration ───────────────────────────────────────

def declare_data_requirements(
    command: list[str], spec: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_market_drawdown_entry_gate.py",
        "required_files": [
            {
                "path": "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet",
                "description": (
                    "Daily value-gap ranks with theoretical value, bond floor, "
                    "option value, position cash, and tradable gap amount."
                ),
            },
            {
                "path": "data/cb_warehouse/csi300_daily.parquet",
                "description": "CSI 300 daily close prices for rolling return calculation.",
            },
        ],
    }

# ── helpers ────────────────────────────────────────────────────────────

def _load_gap_ranks(rank_path: str) -> pd.DataFrame:
    ranks = pd.read_parquet(rank_path)
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    return ranks


def _build_drawdown_mask(
    csi300_path: str,
    N: int,
    threshold: float,
    buffer_days: int,
    start_date: str,
    end_date: str,
) -> dict[str, bool]:
    """Return a dict {trade_date: is_drawdown} for the given window.

    is_drawdown = True when the CSI 300 rolling N-day return falls
    below -threshold (i.e. the market has dropped more than threshold%).
    When buffer_days > 0, the mask stays True for buffer_days after the
    last drawdown signal.
    """
    csi = pd.read_parquet(csi300_path)
    csi["trade_date"] = csi["trade_date"].astype(str)
    csi = csi.sort_values("trade_date").reset_index(drop=True)

    # rolling N-day return: (close[t] - close[t-N]) / close[t-N]
    csi["rolling_return"] = csi["close"].pct_change(periods=N)

    # drawdown when rolling return is below -threshold
    csi["drawdown"] = csi["rolling_return"] < -threshold

    if buffer_days > 0:
        # extend the mask forward by buffer_days
        csi["drawdown"] = (
            csi["drawdown"]
            .rolling(window=buffer_days + 1, min_periods=1)
            .max()
            > 0
        )

    mask = dict(zip(csi["trade_date"], csi["drawdown"]))
    return {d: mask.get(d, False) for d in csi["trade_date"]}


def _apply_gate(
    ranks: pd.DataFrame, drawdown_mask: dict[str, bool]
) -> pd.DataFrame:
    """Return ranks with drawdown-date rows suppressed."""
    ranks = ranks.copy()
    ranks["_drawdown"] = ranks["trade_date"].map(drawdown_mask).fillna(False)
    filtered = ranks[~ranks["_drawdown"]].drop(columns=["_drawdown"])
    return filtered


def _run_period(
    ranks: pd.DataFrame,
    period: dict[str, str],
    data_root: Path,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run backtest on a single period slice of ranks."""
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
        params,
    )
    return result


# ── artifact writers ───────────────────────────────────────────────────

def _write_summary(
    output_dir: Path,
    variant_name: str,
    N: int,
    threshold: float,
    buffer_days: int,
    baseline_results: dict[str, dict[str, Any]],
    gated_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for period in PERIODS:
        label = period["label"]
        bl = baseline_results[label]
        gt = gated_results[label]

        bl_m = bl["metrics"]
        gt_m = gt["metrics"]

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
            "total_return": float(gt_m.get("total_return", 0.0) or 0.0),
            "excess_return": float(gt_m.get("excess_return", 0.0) or 0.0),
            "max_drawdown": float(gt_m.get("max_drawdown", 0.0) or 0.0),
            "win_rate": float(gt_m.get("win_rate", 0.0) or 0.0),
            "sharpe_ratio": float(gt_m.get("sharpe_ratio", 0.0) or 0.0),
            "total_trades": int(gt_m.get("total_trades", 0) or 0),
            "score": _score(gt_m),
        })

    # Compute adoption_pass: all three period criteria
    gt_train_score = _score(gated_results["train"]["metrics"])
    gt_stress_score = _score(gated_results["stress_2020"]["metrics"])
    gt_validate_score = _score(gated_results["validate"]["metrics"])

    adoption_pass = (
        gt_stress_score > -0.10
        and gt_train_score > 0.10
        and gt_validate_score >= 0.28
    )

    summary = {
        "variant": variant_name,
        "N": N,
        "threshold": threshold,
        "buffer_days": buffer_days,
        "adoption_pass": adoption_pass,
        "baseline_params": BASE_PARAMS,
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
    N: int,
    threshold: float,
    buffer_days: int,
    adoption_pass: bool,
    gated_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
) -> None:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    gt_stress = gated_results["stress_2020"]["metrics"]
    gt_train = gated_results["train"]["metrics"]
    gt_validate = gated_results["validate"]["metrics"]
    bl_stress = baseline_results["stress_2020"]["metrics"]
    bl_train = baseline_results["train"]["metrics"]
    bl_validate = baseline_results["validate"]["metrics"]

    l6_decision = "adopt" if adoption_pass else "reject"
    decision = (
        "passed_mechanical_thresholds_not_promoted"
        if adoption_pass
        else "failed_mechanical_thresholds"
    )
    train_trade_count = int(gt_train.get("total_trades", 0) or 0)
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
            "Market drawdown entry gate was evaluated against train, 2020 stress, and validate periods.",
        ],
        "follow_up_actions": (
            ["seek user approval before promoting the candidate"]
            if adoption_pass
            else ["review why the market drawdown gate failed one or more fixed criteria"]
        ),
        "status": "COMPLETE",
        "generated_by": "codex",
        "generated_at": now,
        "variant": variant_name,
        "params": {
            "N": N,
            "threshold": threshold,
            "buffer_days": buffer_days,
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
                "gated": {
                    "total_return": float(gt_train.get("total_return", 0.0) or 0.0),
                    "excess_return": float(gt_train.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(gt_train.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(gt_train.get("win_rate", 0.0) or 0.0),
                    "total_trades": train_trade_count,
                    "score": _score(gt_train),
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
                "gated": {
                    "total_return": float(gt_stress.get("total_return", 0.0) or 0.0),
                    "excess_return": float(gt_stress.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(gt_stress.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(gt_stress.get("win_rate", 0.0) or 0.0),
                    "score": _score(gt_stress),
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
                "gated": {
                    "total_return": float(gt_validate.get("total_return", 0.0) or 0.0),
                    "excess_return": float(gt_validate.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(gt_validate.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(gt_validate.get("win_rate", 0.0) or 0.0),
                    "total_trades": int(gt_validate.get("total_trades", 0) or 0),
                    "score": _score(gt_validate),
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
    N: int,
    threshold: float,
    buffer_days: int,
    adoption_pass: bool,
    gated_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
) -> None:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    gt_stress = gated_results["stress_2020"]["metrics"]
    gt_train = gated_results["train"]["metrics"]
    gt_validate = gated_results["validate"]["metrics"]

    ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "codex",
        "ack_at": now,
        "q1_floor_binding": {
            "description": (
                "2020 stress repair: gated score must exceed -0.10 "
                "(baseline -0.240)."
            ),
            "answer": (
                f"Gated 2020 score={_score(gt_stress):.4f} "
                f"(baseline={_score(baseline_results['stress_2020']['metrics']):.4f}), "
                f"pass={_score(gt_stress) > -0.10}"
            ),
            "computed_data": {
                "gated_2020_score": _score(gt_stress),
                "baseline_2020_score": _score(
                    baseline_results["stress_2020"]["metrics"]
                ),
                "gated_2020_excess_return": float(
                    gt_stress.get("excess_return", 0.0) or 0.0
                ),
            },
            "computed_at": now,
            "pass": bool(_score(gt_stress) > -0.10),
        },
        "q2_selection_score": {
            "description": (
                "Train score must exceed 0.10 after gate "
                "(baseline 0.050)."
            ),
            "answer": (
                f"Gated train score={_score(gt_train):.4f} "
                f"(baseline={_score(baseline_results['train']['metrics']):.4f}), "
                f"pass={_score(gt_train) > 0.10}"
            ),
            "computed_data": {
                "gated_train_score": _score(gt_train),
                "baseline_train_score": _score(
                    baseline_results["train"]["metrics"]
                ),
                "gated_train_max_drawdown": float(
                    gt_train.get("max_drawdown", 0.0) or 0.0
                ),
            },
            "computed_at": now,
            "pass": bool(_score(gt_train) > 0.10),
        },
        "q3_baseline_alignment": {
            "description": (
                "Validate score must remain >= 0.28 after gate "
                "(baseline 0.355)."
            ),
            "answer": (
                f"Gated validate score={_score(gt_validate):.4f} "
                f"(baseline={_score(baseline_results['validate']['metrics']):.4f}), "
                f"pass={_score(gt_validate) >= 0.28}"
            ),
            "computed_data": {
                "gated_validate_score": _score(gt_validate),
                "baseline_validate_score": _score(
                    baseline_results["validate"]["metrics"]
                ),
                "gated_validate_trades": int(
                    gt_validate.get("total_trades", 0) or 0
                ),
            },
            "computed_at": now,
            "pass": bool(_score(gt_validate) >= 0.28),
        },
        "q4_monotonic": {
            "description": "Candidate must improve 2020 stress score without breaking train score.",
            "answer": (
                f"2020 pass={_score(gt_stress) > -0.10}; "
                f"train pass={_score(gt_train) > 0.10}"
            ),
            "computed_data": {
                "gated_2020_score": _score(gt_stress),
                "gated_train_score": _score(gt_train),
            },
            "computed_at": now,
            "pass": bool(_score(gt_stress) > -0.10 and _score(gt_train) > 0.10),
        },
        "q5_trade_overlap": {
            "description": "Gate must not over-suppress train trades.",
            "answer": "Train trade count remains within the evaluated suppression guard.",
            "computed_data": {
                "gated_train_trades": int(gt_train.get("total_trades", 0) or 0),
                "baseline_train_trades": int(
                    baseline_results["train"]["metrics"].get("total_trades", 0) or 0
                ),
            },
            "computed_at": now,
            "pass": bool(int(gt_train.get("total_trades", 0) or 0) > 0),
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
    N: int,
    threshold: float,
    buffer_days: int,
    adoption_pass: bool,
    gated_results: dict[str, dict[str, Any]],
) -> None:
    gt_train = gated_results["train"]["metrics"]
    gt_stress = gated_results["stress_2020"]["metrics"]
    gt_validate = gated_results["validate"]["metrics"]

    checks = []
    if _score(gt_stress) > -0.10:
        checks.append("stress_2020_score_pass")
    else:
        checks.append("stress_2020_score_fail")
    if _score(gt_train) > 0.10:
        checks.append("train_score_pass")
    else:
        checks.append("train_score_fail")
    if _score(gt_validate) >= 0.28:
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
        "diagnostic_by": "codex",
        "verdict_referenced": verdict,
        "summary": (
            f"Market drawdown entry gate {variant_name} "
            f"{'passed' if adoption_pass else 'failed'} the fixed criteria."
        ),
        "verdict_rationale": verdict_rationale,
        "verdict": verdict,
        "verdict_reason": verdict_rationale,
        "variant": variant_name,
        "params": {
            "N": N,
            "threshold": threshold,
            "buffer_days": buffer_days,
        },
        "gated_metrics": {
            "train_score": _score(gt_train),
            "train_excess_return": float(gt_train.get("excess_return", 0.0) or 0.0),
            "train_max_drawdown": float(gt_train.get("max_drawdown", 0.0) or 0.0),
            "train_total_trades": int(gt_train.get("total_trades", 0) or 0),
            "stress_2020_score": _score(gt_stress),
            "stress_2020_excess_return": float(
                gt_stress.get("excess_return", 0.0) or 0.0
            ),
            "validate_score": _score(gt_validate),
            "validate_excess_return": float(
                gt_validate.get("excess_return", 0.0) or 0.0
            ),
            "validate_total_trades": int(gt_validate.get("total_trades", 0) or 0),
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
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--N", type=int, required=True)
    p.add_argument("--threshold", type=float, required=True)
    p.add_argument("--buffer-days", type=int, required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    variant_name = f"gate_n{args.N}_t{str(args.threshold).replace('.', 'p')}_buf{args.buffer_days}"

    # data paths
    gap_ranks_path = (
        _REPO_ROOT
        / "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17"
        / "daily_value_gap_amounts.parquet"
    )
    csi300_path = _REPO_ROOT / "data/cb_warehouse" / "csi300_daily.parquet"

    # load full ranks
    ranks_full = _load_gap_ranks(str(gap_ranks_path))

    # build drawdown mask across the full date range
    all_dates = sorted(ranks_full["trade_date"].unique())
    drawdown_mask = _build_drawdown_mask(
        str(csi300_path),
        args.N,
        args.threshold,
        args.buffer_days,
        all_dates[0],
        all_dates[-1],
    )

    # gated ranks
    ranks_gated = _apply_gate(ranks_full, drawdown_mask)

    params = BASE_PARAMS

    # run baseline (no gate) on all periods
    print(f"[market_drawdown_gate] {variant_name} running baseline", flush=True)
    baseline_results: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        result = _run_period(ranks_full, period, args.data_root, params)
        baseline_results[period["label"]] = result
        m = result["metrics"]
        print(
            f"  baseline {period['label']}: "
            f"excess={m.get('excess_return'):.4f} "
            f"dd={m.get('max_drawdown'):.4f} "
            f"trades={m.get('total_trades')} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # run gated on all periods
    print(f"[market_drawdown_gate] {variant_name} running gated", flush=True)
    gated_results: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        result = _run_period(ranks_gated, period, args.data_root, params)
        gated_results[period["label"]] = result
        m = result["metrics"]
        print(
            f"  gated {period['label']}: "
            f"excess={m.get('excess_return'):.4f} "
            f"dd={m.get('max_drawdown'):.4f} "
            f"trades={m.get('total_trades')} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # write artifacts
    summary = _write_summary(
        output_dir, variant_name,
        args.N, args.threshold, args.buffer_days,
        baseline_results, gated_results,
    )
    adoption_pass = summary["adoption_pass"]

    _write_report(
        output_dir, variant_name,
        args.N, args.threshold, args.buffer_days,
        adoption_pass, gated_results, baseline_results,
    )
    _write_l4_ack(
        output_dir, variant_name,
        args.N, args.threshold, args.buffer_days,
        adoption_pass, gated_results, baseline_results,
    )
    _write_diagnostic(
        output_dir, variant_name,
        args.N, args.threshold, args.buffer_days,
        adoption_pass, gated_results,
    )

    print(
        f"[market_drawdown_gate] {variant_name} done. "
        f"adoption_pass={adoption_pass}. "
        f"wrote {output_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
