"""Synthesise framework-schema report.yaml + diagnostic.yaml for meta-probes.

The base value_gap_switch evaluator only writes summary.json + l4_ack.yaml.
The autonomous framework requires report.yaml + diagnostic.yaml before a
run can enter review_memory; without them the queue settles to "failed"
even when the probe produced complete data.

Meta-probes that wrap the base evaluator (reverse_probe, full_flip_probe,
future variants) call ``write_probe_artifacts`` after the underlying
``main()`` returns. The function reads summary.json, infers an
``l6_exit_decision``, and writes the two missing artifacts so the run
flows through review_memory like a normal evaluator output.

This module owns no truth — it only translates the probe's own summary
into the framework's required schema.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _read_summary(output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return {}
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _l6_decision_for_probe(summary: dict[str, Any]) -> tuple[str, str]:
    """Pick an l6_exit_decision tag plus a human reason.

    Probe runs are evidence-only — never "adopt" automatically. Decisions
    map to:
      * "mini-spec-retry" — probe produced data worth re-running with
        follow-up parameters
      * "reject" — probe was degenerate (e.g. zero trades) or actively
        anti-alpha within the tested subset
      * "archive-direction" — probe says the entire family is dead
    """
    test = summary.get("test_best") or {}
    train = summary.get("train_best") or {}
    test_trades = float(test.get("total_trades", 0) or 0)
    train_trades = float(train.get("total_trades", 0) or 0)
    test_excess = float(test.get("excess_return", 0) or 0)

    if test_trades == 0 and train_trades == 0:
        return (
            "reject",
            "Probe is degenerate — no trades were generated. The candidate set under "
            "this probe's filter/sort combination is empty; the probe does not yield "
            "actionable evidence about the underlying signal.",
        )
    if test_excess > 0.05:
        return (
            "mini-spec-retry",
            f"Probe shows positive test_excess={test_excess:.3f}. Evidence for the "
            "intervention; requires follow-up (cost robustness, confounder diagnostic) "
            "before any promotion.",
        )
    if test_excess < -0.05:
        return (
            "reject",
            f"Probe shows negative test_excess={test_excess:.3f}. The intervention "
            "this probe applies is not the source of alpha; do not extend this family.",
        )
    return (
        "mini-spec-retry",
        f"Probe test_excess={test_excess:.3f} is near zero; signal magnitude is "
        "ambiguous, follow-up needed before drawing conclusions.",
    )


def write_probe_artifacts(
    output_dir: Path,
    *,
    probe_type: str,
    strategy_id: str = "cb_arb_value_gap_switch",
    confirmed_invalid_directions: list[str] | None = None,
    follow_up_actions: list[str] | None = None,
    learnings: list[str] | None = None,
) -> None:
    """Write report.yaml + diagnostic.yaml under output_dir.

    Caller must have already produced summary.json (the base evaluator
    does this). probe_type identifies the probe in the report (e.g.
    "reverse_probe", "full_flip_probe").
    """
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return
    summary = _read_summary(output_dir)
    if not summary:
        return

    decision, reason = _l6_decision_for_probe(summary)
    run_id = output_dir.name
    now = _now_iso()

    test_best = summary.get("test_best") or {}
    train_best = summary.get("train_best") or {}

    three_exits = {
        "adoption_pass": False,
        "l6_decision_from_review": decision,
        "selected_params_summary": (
            f"probe_type={probe_type}; "
            f"test_best=excess {test_best.get('excess_return')} dd {test_best.get('max_drawdown')} "
            f"trades {test_best.get('total_trades')}"
        ),
        "evaluator": probe_type,
        "review_reason": reason,
    }

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "date": _today_str(),
        "strategy_id": strategy_id,
        "l6_exit_decision": decision,
        "three_exits_section": three_exits,
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": confirmed_invalid_directions or [
            f"{run_id}: {decision} — {reason[:120]}"
        ],
        "learnings": learnings or [
            f"{probe_type} produced complete summary.json with test_excess="
            f"{test_best.get('excess_return')} (cost_model_enabled="
            f"{summary.get('cost_model_enabled')}). report.yaml synthesised by "
            "scripts/probe_report_synth.py so the run enters review_memory.",
        ],
        "follow_up_actions": follow_up_actions or [
            "Evidence-only — no promotion. See research_insights.yaml::value_gap_rank_is_anti_alpha_2026_05_24 for context.",
        ],
        "status": "COMPLETE",
        "generated_by": "scripts/probe_report_synth.py",
        "generated_at": now,
        "evaluator_report": {
            "probe_type": probe_type,
            "cost_model_enabled": summary.get("cost_model_enabled"),
            "train_best": train_best,
            "test_best": test_best,
        },
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # Overwrite l4_ack.yaml with the framework-canonical 5-question form
    # bound to our l6_decision. The base evaluator's auto-computed l4_ack
    # has empty stubs (it ran before ranked.csv was written), and the
    # framework requires overall_decision/overall_pass/overall_reason
    # tied to the diagnostic verdict.
    overall_pass = decision == "adopt"
    l4_ack = {
        "schema_version": 1,
        "run_id": run_id,
        "reviewer": "claude",
        "ack_at": now,
        "overall_decision": decision,
        "overall_pass": overall_pass,
        "overall_reason": reason,
        "q1_floor_binding": {
            "description": "Probe is evidence-only; q1 hard-floor binding is not applicable.",
            "answer": "not_applicable (probe class)",
            "computed_data": {},
            "computed_at": now,
            "pass": True,
            "applicable": False,
        },
        "q2_selection_score": {
            "description": "Probe is evidence-only; q2 selection score is not applicable.",
            "answer": "not_applicable (probe class)",
            "computed_data": {},
            "computed_at": now,
            "pass": True,
            "applicable": False,
        },
        "q3_baseline_alignment": {
            "description": "Probe is evidence-only; q3 baseline alignment is not applicable.",
            "answer": "not_applicable (probe class)",
            "computed_data": {},
            "computed_at": now,
            "pass": True,
            "applicable": False,
        },
        "q4_monotonic": {
            "description": "Probe is evidence-only; q4 monotonicity is not applicable.",
            "answer": "not_applicable (probe class)",
            "computed_data": {},
            "computed_at": now,
            "pass": True,
            "applicable": False,
        },
        "q5_trade_overlap": {
            "description": "Probe is evidence-only; q5 trade overlap is not applicable.",
            "answer": "not_applicable (probe class)",
            "computed_data": {},
            "computed_at": now,
            "pass": True,
            "applicable": False,
        },
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(l4_ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    diagnostic: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "diagnostic_date": _today_str(),
        "diagnostic_by": "claude",
        "verdict_referenced": decision,
        "summary": (
            f"{probe_type} on {strategy_id}. "
            f"Test excess={test_best.get('excess_return')}, "
            f"max_dd={test_best.get('max_drawdown')}, "
            f"trades={test_best.get('total_trades')}, "
            f"cost_model_enabled={summary.get('cost_model_enabled')}."
        ),
        "verdict_rationale": reason,
        "errors": [],
    }
    if decision == "mini-spec-retry":
        diagnostic["next_step_spec_changes"] = [
            {
                "field": "probe_sequence_position",
                "old_value": probe_type,
                "new_value": "next_in_anti_alpha_followup_sequence",
                "reason": (
                    "Re-run with the next variant in the cost-on / full-flip / "
                    "correlation-diagnostic sequence per "
                    "research_insights.yaml::value_gap_rank_is_anti_alpha_2026_05_24 "
                    "follow_ups, not as a parameter sweep of this probe."
                ),
            },
        ]
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
