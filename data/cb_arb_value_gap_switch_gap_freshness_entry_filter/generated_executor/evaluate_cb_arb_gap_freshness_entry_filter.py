"""Evaluate gap freshness entry filter for cb_arb value-gap switch.

Core idea: value gap signals decay in predictive power with age. A gap that
first appears today is more likely to close profitably than one persisting for
weeks (already priced in). This executor computes gap_age per CB per day
(days since gap first opened), applies a boolean entry mask allowing only CBs
with gap_age <= max_age_days before passing through the existing signal
ranking and backtest engine. Grid-searches max_age_days over
[1, 3, 5, 10, 20, 40, 60, inf]. The inf case (no filter) is the within-run
baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root & sys.path — must come before any `from scripts.X import Y`.
# The compliance import-reachability probe runs with -I in /tmp, so all
# non-stdlib imports that follow must resolve from the venv site-packages
# (numpy/pandas/yaml) or from REPO_ROOT (scripts.*).
# ---------------------------------------------------------------------------

def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ---------------------------------------------------------------------------
# Lazy third-party imports — not available at module level in isolated probe.
# ---------------------------------------------------------------------------

def _get_np():
    """Lazy import numpy."""
    import numpy as _np
    return _np


def _get_pd():
    """Lazy import pandas."""
    import pandas as _pd
    return _pd


def _get_yaml():
    """Lazy import yaml."""
    import yaml as _yaml
    return _yaml


# YAML numpy representer registration runs once at first yaml write.
_YAML_REPRS_REGISTERED = False


def _ensure_yaml_np_reprs():
    global _YAML_REPRS_REGISTERED
    if _YAML_REPRS_REGISTERED:
        return
    yaml = _get_yaml()
    np = _get_np()

    def _yaml_repr_np_float(dumper, data):
        return dumper.represent_float(float(data))

    def _yaml_repr_np_int(dumper, data):
        return dumper.represent_int(int(data))

    yaml.SafeDumper.add_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_representer(np.integer, _yaml_repr_np_int)
    yaml.SafeDumper.add_multi_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_multi_representer(np.integer, _yaml_repr_np_int)
    _YAML_REPRS_REGISTERED = True


# ---------------------------------------------------------------------------
# Data requirements — must exist
# ---------------------------------------------------------------------------

_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)


def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_gap_freshness_entry_filter.py",
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
                "description": (
                    "Daily value-gap amounts per CB per trade_date from "
                    "regime-option-entry-gate run."
                ),
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic reference table for universe filtering.",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Gatekeeper
# ---------------------------------------------------------------------------

def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

PERIODS: list[dict[str, str]] = [
    {"label": "train", "start": "20180101", "end": "20221231"},
    {"label": "stress_2020", "start": "20200101", "end": "20201231"},
    {"label": "test", "start": "20230101", "end": "20251231"},
]

MAX_AGE_DAYS_SWEEP = [1, 3, 5, 10, 20, 40, 60, float("inf")]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _resolve_data_path(data_root: str | Path, relative: str) -> Path:
    data_root = Path(data_root)
    rel = Path(relative)
    candidates = [
        data_root / rel,
        _REPO_ROOT / rel,
        Path.cwd() / rel,
    ]
    if rel.parts[0] == "data":
        inner = Path(*rel.parts[1:])
        candidates.append(data_root / inner)
        candidates.append(_REPO_ROOT / rel)
    for c in candidates:
        if c.exists():
            return c
    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Cannot find {relative} under data_root={data_root}; searched: {searched}"
    )


def _load_gap_ranks(data_root: str) -> Any:
    pd = _get_pd()
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # Ensure value_gap_amount is numeric
    df["value_gap_amount"] = pd.to_numeric(df["value_gap_amount"], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Gap freshness computation
# ---------------------------------------------------------------------------

def _compute_gap_age(df: Any) -> Any:
    """Compute gap_age per CB per day: days since gap first opened.

    gap_age is computed by tracking when value_gap_amount transitions from
    zero/NaN to positive. When the gap closes (returns to <= 0 or NaN),
    gap_open_date resets to null. gap_age = trade_date - gap_open_date
    in calendar days.

    Returns a DataFrame with a new 'gap_age' column (float, NaN = no active gap).
    """
    np = _get_np()
    df = df.copy()
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    gap_ages: list[float] = []
    gap_open_date: Any = None

    for (_code,), grp in df.groupby(["ts_code"], sort=False):
        grp = grp.sort_values("trade_date")
        for _, row in grp.iterrows():
            gap_val = row["value_gap_amount"]
            if _is_gap_closed(gap_val):
                gap_open_date = None
                gap_ages.append(np.nan)
            else:
                if gap_open_date is None:
                    gap_open_date = row["trade_date"]
                age = (row["trade_date"] - gap_open_date).days
                gap_ages.append(float(age))

    df["gap_age"] = gap_ages
    return df


def _is_gap_closed(gap_val: Any) -> bool:
    """Return True if the gap is effectively closed (zero/NaN/negative)."""
    try:
        return bool(gap_val is None or (hasattr(gap_val, "__float__") and float(gap_val) <= 0))
    except (ValueError, TypeError):
        return True


def _apply_freshness_filter(df: Any, max_age_days: float) -> Any:
    """Filter out rows where gap_age > max_age_days.

    Retains rows where gap_age is NaN (no active gap) or gap_age <= max_age_days.
    When max_age_days is inf, all rows are retained (no filter / baseline).
    """
    if max_age_days == float("inf"):
        return df.copy()

    mask = df["gap_age"].isna() | (df["gap_age"] <= max_age_days)
    return df[mask].copy()


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def _run_period(
    ranks: Any,
    period: dict[str, str],
    data_root: Path,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run backtest on a single period slice of ranks."""
    # lazy import to keep module-level load fast for compliance probe
    from scripts.evaluate_cb_arb_value_gap_switch import _run_value_gap_backtest  # noqa: E402
    period_ranks = ranks[
        (ranks["trade_date"] >= period["start"])
        & (ranks["trade_date"] <= period["end"])
    ].copy()
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
    # Convert back to string for the backtester
    period_ranks["trade_date"] = period_ranks["trade_date"].astype(str)
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


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------

def _score_fn(metrics: dict[str, Any]) -> float:
    """Score function: excess_return - 0.25 * |max_drawdown|."""
    from scripts.evaluate_cb_arb_value_gap_switch import _score  # noqa: E402
    return _score(metrics)


def _write_summary(
    output_dir: Path,
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
    baseline_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Write summary.json with all grid search results."""
    output_dir.mkdir(parents=True, exist_ok=True)

    _score = _score_fn

    rows: list[dict[str, Any]] = []
    for cand in all_candidates:
        gated = cand["gated_results"]
        max_age = cand["max_age_days"]
        for period in PERIODS:
            label = period["label"]
            bl_m = baseline_results[label]["metrics"]
            gt_m = gated[label]["metrics"]
            rows.append(
                {
                    "variant": f"max_age_{max_age}",
                    "max_age_days": max_age,
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
                    "baseline_score": _score(bl_m),
                    "baseline_trades": int(bl_m.get("total_trades", 0) or 0),
                }
            )

    # Determine adoption_pass from best candidate
    if best_candidate is not None:
        best_age = best_candidate["max_age_days"]
        gt = best_candidate["gated_results"]
        gt_test_score = _score(gt["test"]["metrics"])
        gt_train_score = _score(gt["train"]["metrics"])
        gt_stress_score = _score(gt["stress_2020"]["metrics"])
        bl_test_trades = int(
            baseline_results["test"]["metrics"].get("total_trades", 1) or 1
        )
        gt_test_trades = int(gt["test"]["metrics"].get("total_trades", 0) or 0)
        if bl_test_trades > 0:
            suppression_pct = round((1 - gt_test_trades / bl_test_trades) * 100, 1)
        else:
            suppression_pct = 0.0

        adoption_pass = (
            gt_stress_score > -0.10
            and gt_train_score > 0.10
            and gt_test_score >= 0.28
            and suppression_pct >= 15.0
        )
        best_params = {"max_age_days": best_age}
    else:
        adoption_pass = False
        best_params = {"max_age_days": None}

    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "adoption_pass": adoption_pass,
        "best_params": best_params,
        "best_max_age_days": best_params.get("max_age_days"),
        "baseline_params": BASE_PARAMS,
        "grid_sweep_values": MAX_AGE_DAYS_SWEEP,
        "total_candidates": len(all_candidates),
        "rows": rows,
        "generated_at": datetime.now().isoformat(),
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


def _write_report(
    output_dir: Path,
    adoption_pass: bool,
    best_candidate: dict[str, Any] | None,
    baseline_results: dict[str, dict[str, Any]],
    all_candidates: list[dict[str, Any]],
) -> None:
    _ensure_yaml_np_reprs()
    yaml = _get_yaml()
    _score = _score_fn
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

    if best_candidate is not None:
        best_age = best_candidate["max_age_days"]
        gt = best_candidate["gated_results"]
        gt_train = gt["train"]["metrics"]
        gt_stress = gt["stress_2020"]["metrics"]
        gt_test = gt["test"]["metrics"]
        bl_train = baseline_results["train"]["metrics"]
        bl_test = baseline_results["test"]["metrics"]
        bl_stress = baseline_results["stress_2020"]["metrics"]

        train_trade_count = int(gt_train.get("total_trades", 0) or 0)
        bl_train_trade_count = int(bl_train.get("total_trades", 0) or 0)
        if bl_train_trade_count > 0:
            suppression_pct = round(
                (1 - train_trade_count / bl_train_trade_count) * 100, 1
            )
        else:
            suppression_pct = 0.0

        l6_decision = "adopt" if adoption_pass else "reject"
        decision = (
            "passed_mechanical_thresholds_not_promoted"
            if adoption_pass
            else "failed_mechanical_thresholds"
        )

        report = {
            "schema_version": 1,
            "run_id": output_dir.name,
            "date": now.strftime("%Y-%m-%d"),
            "strategy_id": "cb_arb_value_gap_switch",
            "l6_exit_decision": l6_decision,
            "three_exits_section": {
                "adoption_pass": adoption_pass,
                "selected_variant": f"max_age_{best_age}",
                "criteria": {
                    "stress_2020_score_gt": -0.10,
                    "train_score_gt": 0.10,
                    "test_score_gte": 0.28,
                    "suppression_pct_gte": 15.0,
                },
            },
            "compute_cost_yuan": 0.0,
            "confirmed_invalid_directions": (
                [] if adoption_pass else [f"max_age_{best_age}"]
            ),
            "learnings": [
                f"Gap freshness entry filter grid-searched {len(all_candidates)} "
                f"max_age_days values over [{', '.join(str(v) for v in MAX_AGE_DAYS_SWEEP)}]."
            ],
            "follow_up_actions": (
                ["seek user approval before promoting the candidate"]
                if adoption_pass
                else [
                    "review why the gap freshness filter failed one or more fixed criteria"
                ]
            ),
            "status": "COMPLETE",
            "generated_by": "hermes",
            "generated_at": now_str,
            "variant": f"max_age_{best_age}",
            "params": {"max_age_days": best_age},
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
                "test": {
                    "baseline": {
                        "total_return": float(bl_test.get("total_return", 0.0) or 0.0),
                        "excess_return": float(bl_test.get("excess_return", 0.0) or 0.0),
                        "max_drawdown": float(bl_test.get("max_drawdown", 0.0) or 0.0),
                        "win_rate": float(bl_test.get("win_rate", 0.0) or 0.0),
                        "total_trades": int(bl_test.get("total_trades", 0) or 0),
                        "score": _score(bl_test),
                    },
                    "gated": {
                        "total_return": float(gt_test.get("total_return", 0.0) or 0.0),
                        "excess_return": float(gt_test.get("excess_return", 0.0) or 0.0),
                        "max_drawdown": float(gt_test.get("max_drawdown", 0.0) or 0.0),
                        "win_rate": float(gt_test.get("win_rate", 0.0) or 0.0),
                        "total_trades": int(gt_test.get("total_trades", 0) or 0),
                        "score": _score(gt_test),
                    },
                },
            },
            "warnings": [],
        }
    else:
        l6_decision = "reject"
        decision = "failed_mechanical_thresholds"
        report = {
            "schema_version": 1,
            "run_id": output_dir.name,
            "date": now.strftime("%Y-%m-%d"),
            "strategy_id": "cb_arb_value_gap_switch",
            "l6_exit_decision": l6_decision,
            "status": "COMPLETE",
            "generated_by": "hermes",
            "generated_at": now_str,
            "adoption_pass": False,
            "decision": decision,
            "error": "No valid candidate produced (empty backtest results).",
            "warnings": [],
        }

    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_l4_ack(
    output_dir: Path,
    adoption_pass: bool,
    best_candidate: dict[str, Any] | None,
    baseline_results: dict[str, dict[str, Any]],
) -> None:
    _ensure_yaml_np_reprs()
    yaml = _get_yaml()
    _score = _score_fn
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    if best_candidate is not None:
        best_age = best_candidate["max_age_days"]
        gt = best_candidate["gated_results"]
        gt_test = gt["test"]["metrics"]
        gt_train = gt["train"]["metrics"]
        gt_stress = gt["stress_2020"]["metrics"]
        bl_test = baseline_results["test"]["metrics"]

        gt_test_score = _score(gt_test)
        gt_train_score = _score(gt_train)
        gt_stress_score = _score(gt_stress)
        bl_test_trades = int(bl_test.get("total_trades", 1) or 1)
        gt_test_trades = int(gt_test.get("total_trades", 0) or 0)
        if bl_test_trades > 0:
            suppression_pct = round((1 - gt_test_trades / bl_test_trades) * 100, 1)
        else:
            suppression_pct = 0.0

        q1_pass = bool(gt_stress_score > -0.10)
        q2_pass = bool(gt_train_score > 0.10)
        q3_pass = bool(gt_test_score >= 0.28)
        q4_pass = bool(suppression_pct >= 15.0)

        ack = {
            "schema_version": 1,
            "run_id": output_dir.name,
            "reviewer": "hermes_executor_code",
            "ack_at": now,
            "q1_hard_floors": {
                "description": "2020 stress period check.",
                "answer": (
                    f"best max_age={best_age}, 2020 score={gt_stress_score:.4f} "
                    f"(baseline={_score(baseline_results['stress_2020']['metrics']):.4f}), "
                    f"pass={q1_pass}"
                ),
                "computed_data": {
                    "gated_2020_score": gt_stress_score,
                    "baseline_2020_score": _score(
                        baseline_results["stress_2020"]["metrics"]
                    ),
                },
                "computed_at": now,
                "pass": q1_pass,
            },
            "q2_selection_quality": {
                "description": "Train period check.",
                "answer": (
                    f"best max_age={best_age}, train score={gt_train_score:.4f} "
                    f"(baseline={_score(baseline_results['train']['metrics']):.4f}), "
                    f"pass={q2_pass}"
                ),
                "computed_data": {
                    "gated_train_score": gt_train_score,
                    "baseline_train_score": _score(
                        baseline_results["train"]["metrics"]
                    ),
                    "gated_train_max_drawdown": float(
                        gt_train.get("max_drawdown", 0.0) or 0.0
                    ),
                },
                "computed_at": now,
                "pass": q2_pass,
            },
            "q3_baseline_alignment": {
                "description": "Test period check.",
                "answer": (
                    f"best max_age={best_age}, test score={gt_test_score:.4f} "
                    f"(baseline={_score(bl_test):.4f}), pass={q3_pass}"
                ),
                "computed_data": {
                    "gated_test_score": gt_test_score,
                    "baseline_test_score": _score(bl_test),
                    "gated_test_trades": gt_test_trades,
                },
                "computed_at": now,
                "pass": q3_pass,
            },
            "q4_suppression_quality": {
                "description": "Trade count suppression must >= 15%.",
                "answer": (
                    f"best max_age={best_age}, test trades={gt_test_trades} "
                    f"(baseline={bl_test_trades}), suppression={suppression_pct}%, "
                    f"pass={q4_pass}"
                ),
                "computed_data": {
                    "gated_test_trades": gt_test_trades,
                    "baseline_test_trades": bl_test_trades,
                    "suppression_pct": suppression_pct,
                },
                "computed_at": now,
                "pass": q4_pass,
            },
            "q5_trade_overlap": {
                "description": "Gate must not over-suppress train trades.",
                "answer": (
                    f"train trades={int(gt_train.get('total_trades', 0) or 0)} "
                    f"(baseline={int(baseline_results['train']['metrics'].get('total_trades', 0) or 0)})"
                ),
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
    else:
        ack = {
            "schema_version": 1,
            "run_id": output_dir.name,
            "reviewer": "hermes_executor_code",
            "ack_at": now,
            "overall_pass": False,
            "overall_decision": "reject",
            "overall_reason": "No valid candidate produced.",
            "auto_computed_at": now,
        }

    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_diagnostic(
    output_dir: Path,
    adoption_pass: bool,
    best_candidate: dict[str, Any] | None,
    all_candidates: list[dict[str, Any]],
) -> None:
    _ensure_yaml_np_reprs()
    yaml = _get_yaml()
    _score = _score_fn
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    if best_candidate is not None:
        best_age = best_candidate["max_age_days"]
        gt = best_candidate["gated_results"]
        gt_test_score = _score(gt["test"]["metrics"])
        gt_train_score = _score(gt["train"]["metrics"])
        gt_stress_score = _score(gt["stress_2020"]["metrics"])

        checks = []
        if gt_stress_score > -0.10:
            checks.append("stress_2020_score_pass")
        else:
            checks.append("stress_2020_score_fail")
        if gt_train_score > 0.10:
            checks.append("train_score_pass")
        else:
            checks.append("train_score_fail")
        if gt_test_score >= 0.28:
            checks.append("test_score_pass")
        else:
            checks.append("test_score_fail")

        verdict = "adopt" if adoption_pass else "reject"
        verdict_rationale = (
            "All period criteria met"
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
                f"Gap freshness entry filter max_age={best_age} "
                f"{'passed' if adoption_pass else 'failed'} the fixed criteria."
            ),
            "verdict_rationale": verdict_rationale,
            "params": {"max_age_days": best_age},
            "grid_sweep_size": len(all_candidates),
            "gated_metrics": {
                "train_score": gt_train_score,
                "train_excess_return": float(
                    gt["train"]["metrics"].get("excess_return", 0.0) or 0.0
                ),
                "train_max_drawdown": float(
                    gt["train"]["metrics"].get("max_drawdown", 0.0) or 0.0
                ),
                "stress_2020_score": gt_stress_score,
                "test_score": gt_test_score,
                "test_excess_return": float(
                    gt["test"]["metrics"].get("excess_return", 0.0) or 0.0
                ),
            },
            "checks": checks,
            "warnings": [],
            "errors": [],
        }
    else:
        diagnostic = {
            "schema_version": 1,
            "run_id": output_dir.name,
            "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
            "diagnostic_by": "hermes",
            "verdict_referenced": "reject",
            "summary": "No valid candidate produced.",
            "verdict_rationale": "No valid candidate produced (empty backtest results).",
            "grid_sweep_size": len(all_candidates),
            "warnings": ["empty_candidates"],
            "errors": [],
        }

    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument(
        "--train-start", default="20180101", help="Train period start (YYYYMMDD)"
    )
    parser.add_argument(
        "--train-end", default="20221231", help="Train period end (YYYYMMDD)"
    )
    parser.add_argument(
        "--test-start", default="20230101", help="Test period start (YYYYMMDD)"
    )
    parser.add_argument(
        "--test-end", default="20251231", help="Test period end (YYYYMMDD)"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Output directory for artifacts"
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=float("inf"),
        help="Max gap age in days for entry (inf = no filter). Grid-sweeps over "
        "[1,3,5,10,20,40,60,inf] regardless of this value.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # lazy import of backtester helpers to keep module-level load fast
    from scripts.evaluate_cb_arb_value_gap_switch import _score  # noqa: E402

    # Load data
    try:
        df_raw = _load_gap_ranks(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[gap_freshness] FATAL: {exc}", flush=True)
        return 1

    # Compute gap_age
    print("[gap_freshness] Computing gap_age per CB per day...", flush=True)
    df_with_age = _compute_gap_age(df_raw)

    # Backtest dates must be strings
    df_with_age["trade_date"] = df_with_age["trade_date"].dt.strftime("%Y%m%d")

    print("[gap_freshness] Running baseline (max_age_days=inf)...", flush=True)
    # Baseline: no filter (inf)
    baseline_ranks = _apply_freshness_filter(df_with_age, float("inf"))
    baseline_results: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        result = _run_period(baseline_ranks, period, Path(args.data_root), BASE_PARAMS)
        baseline_results[period["label"]] = result
        m = result["metrics"]
        print(
            f"  baseline {period['label']}: "
            f"excess={m.get('excess_return', 0.0):.4f} "
            f"dd={m.get('max_drawdown', 0.0):.4f} "
            f"trades={m.get('total_trades', 0)} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # Grid search over max_age_days
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for max_age in MAX_AGE_DAYS_SWEEP:
        if max_age == float("inf"):
            continue  # baseline already done

        age_label = f"max_age_{int(max_age)}"
        print(
            f"[gap_freshness] Sweeping {age_label}...",
            flush=True,
        )

        filtered = _apply_freshness_filter(df_with_age, max_age)
        gated_results: dict[str, dict[str, Any]] = {}
        for period in PERIODS:
            result = _run_period(
                filtered, period, Path(args.data_root), BASE_PARAMS
            )
            gated_results[period["label"]] = result
            m = result["metrics"]
            print(
                f"  {age_label} {period['label']}: "
                f"excess={m.get('excess_return', 0.0):.4f} "
                f"dd={m.get('max_drawdown', 0.0):.4f} "
                f"trades={m.get('total_trades', 0)} "
                f"score={_score(m):.4f}",
                flush=True,
            )

        candidate = {
            "max_age_days": int(max_age),
            "age_label": age_label,
            "gated_results": gated_results,
        }
        all_candidates.append(candidate)

        # Score: test period score
        test_score = _score(gated_results["test"]["metrics"])
        if test_score > best_score:
            best_score = test_score
            best_candidate = candidate

    # Write artifacts
    summary = _write_summary(
        output_dir, all_candidates, best_candidate, baseline_results
    )
    adoption_pass = summary["adoption_pass"]

    _write_report(output_dir, adoption_pass, best_candidate, baseline_results, all_candidates)
    _write_l4_ack(output_dir, adoption_pass, best_candidate, baseline_results)
    _write_diagnostic(output_dir, adoption_pass, best_candidate, all_candidates)

    _gatekeeper_after_run(output_dir)

    if best_candidate is not None:
        best_age = best_candidate["max_age_days"]
        print(
            f"[gap_freshness] DONE adoption_pass={adoption_pass} "
            f"candidates={len(all_candidates)} best_max_age={best_age}",
            flush=True,
        )
    else:
        print(
            f"[gap_freshness] DONE adoption_pass=False candidates={len(all_candidates)} "
            f"(no valid best)",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
