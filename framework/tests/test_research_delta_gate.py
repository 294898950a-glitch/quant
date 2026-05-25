"""Tests for the Phase 1 Research Delta Gate (2026-05-25, V1).

The gate's contract — see `framework/autonomous/research_delta_gate.py`:
emit exactly one of advance / skip / watch for any Hermes proposal, with
a single human-readable reason and a pointer-style evidence dict.

These tests pin the five user-facing acceptance criteria from the
project memory:

  1. Clear duplicate → skip, reason points at the overlapping run.
  2. Proposal with new critical-insight evidence → advance OR watch
     (never silently skip).
  3. Weak-signal / queue-pressure proposal → watch.
  4. Skipped proposals carry a status that the caller can refuse to
     compile / enqueue. (Tested via the DeltaDecision contract; the
     wiring is asserted in test_ideation_cycle / integration tests.)
  5. Every decision exposes a human-readable reason naming the concrete
     piece of evidence the call relied on.
"""

from __future__ import annotations

import pytest

from framework.autonomous.research_delta_gate import (
    ACTIONS,
    CLOSED_FAMILY_REJECT_THRESHOLD,
    QUEUE_SATURATION_DEPTH,
    DeltaDecision,
    evaluate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal(**overrides):
    base = {
        "proposal_id": "test_prop",
        "strategy_id": "cb_arb_value_gap_switch",
        "family": "test_family",
        "capability_ids": ["C001"],
        "source_insight": "",
    }
    base.update(overrides)
    return base


def _digest(*, recent_runs=None, closed_families=None):
    return {
        "recent_runs": recent_runs or [],
        "suggested_closed_families": closed_families or [],
    }


def _insights(*, critical=None, others=None, deprecated_with_closed_families=None):
    """Build a research_insights-style dict.

    deprecated_with_closed_families: list of (insight_id, [family, ...]) tuples.
        Each insight is emitted as priority=critical but with
        do_not_use_as_default_direction=true and a
        follow_up_review_results.closed_families list — i.e. the 2026-05-26
        mandate "this insight has been disproven by follow-up reviews,
        these families are now closed".
    """
    items = []
    for cid in critical or []:
        items.append({"id": cid, "priority": "critical"})
    for oid in others or []:
        items.append({"id": oid, "priority": "medium"})
    for entry in deprecated_with_closed_families or []:
        if isinstance(entry, tuple) and len(entry) == 2:
            iid, fams = entry
        else:
            iid = entry["id"]
            fams = entry["closed_families"]
        items.append({
            "id": iid,
            "priority": "critical",
            "do_not_use_as_default_direction": True,
            "status": "weakened_by_follow_up_rejects_test",
            "follow_up_review_results": {
                "closed_families": list(fams),
            },
        })
    return {"key_insights": items}


def _queue(*, active=0):
    return {"queue": [{"status": "queued"} for _ in range(active)]}


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_decision_must_use_known_action():
    with pytest.raises(ValueError):
        DeltaDecision(action="merge", reason="not in V1")


def test_actions_set_is_exactly_three_v1_values():
    assert ACTIONS == frozenset({"advance", "skip", "watch"})


# ---------------------------------------------------------------------------
# Acceptance: 1. Clear duplicate → skip
# ---------------------------------------------------------------------------


def test_duplicate_family_and_capabilities_skips_with_run_id_reason():
    """If history has a settled non-adopted run with the same family AND
    capability_ids AND the proposal cites no new critical insight, the
    gate must skip and the reason must name the overlapping run."""
    prior = {
        "run_id": "prior_run_xyz",
        "family": "test_family",
        "capability_ids": ["C001"],
        "verdict": "failed_mechanical_thresholds",
    }
    decision = evaluate(
        _proposal(family="test_family", capability_ids=["C001"]),
        recent_digest=_digest(recent_runs=[prior]),
    )
    assert decision.action == "skip"
    assert "prior_run_xyz" in decision.reason
    assert decision.evidence["duplicate_of"] == "prior_run_xyz"
    assert decision.evidence["verdict"] == "failed_mechanical_thresholds"


def test_duplicate_but_adopted_does_not_skip():
    """If the prior run was adopted, that family + capabilities is the
    accepted shape — a new proposal in that same shape may be a tweak
    worth running, not a duplicate. Gate must NOT skip on it."""
    prior = {
        "run_id": "prior_adopted",
        "family": "test_family",
        "capability_ids": ["C001"],
        "verdict": "adopted",
    }
    decision = evaluate(
        _proposal(family="test_family", capability_ids=["C001"]),
        recent_digest=_digest(recent_runs=[prior]),
    )
    assert decision.action != "skip"


# ---------------------------------------------------------------------------
# Acceptance: 2. New critical-insight evidence → never silently skip
# ---------------------------------------------------------------------------


def test_duplicate_with_critical_insight_reference_goes_to_watch_not_skip():
    """Same family + capabilities as a prior failure, BUT the proposal
    cites a critical insight in its source_insight. The gate must NOT
    skip — it must keep the proposal as an artifact (watch) for review."""
    prior = {
        "run_id": "prior_failed",
        "family": "test_family",
        "capability_ids": ["C001"],
        "verdict": "rejected",
    }
    decision = evaluate(
        _proposal(
            family="test_family",
            capability_ids=["C001"],
            source_insight="value_gap_rank_is_anti_alpha_2026_05_24 — see this",
        ),
        recent_digest=_digest(recent_runs=[prior]),
        research_insights=_insights(critical=["value_gap_rank_is_anti_alpha_2026_05_24"]),
    )
    assert decision.action == "watch"
    assert "prior_failed" in decision.reason
    assert "value_gap_rank_is_anti_alpha_2026_05_24" in decision.reason
    assert decision.evidence["cites_critical"] == "value_gap_rank_is_anti_alpha_2026_05_24"


def test_closed_family_with_critical_insight_reference_advances():
    """A family in suggested_closed_families is normally a skip, but
    citing a current critical insight should clear that block — the
    user-level intent is "new evidence may legitimately re-open closed
    directions". Falls through to the default advance branch (assuming
    no other dupe/queue rules apply)."""
    decision = evaluate(
        _proposal(
            family="closed_family",
            capability_ids=["C002"],
            source_insight="value_gap_rank_is_anti_alpha_2026_05_24",
        ),
        recent_digest=_digest(
            closed_families=[{"tag": "closed_family", "reject_count": 5}]
        ),
        research_insights=_insights(
            critical=["value_gap_rank_is_anti_alpha_2026_05_24"]
        ),
    )
    assert decision.action == "advance"


# ---------------------------------------------------------------------------
# Acceptance: skip — closed family without new evidence
# ---------------------------------------------------------------------------


def test_closed_family_without_critical_insight_skips():
    decision = evaluate(
        _proposal(family="closed_family", capability_ids=["C003"]),
        recent_digest=_digest(
            closed_families=[
                {"tag": "closed_family", "reject_count": CLOSED_FAMILY_REJECT_THRESHOLD}
            ]
        ),
    )
    assert decision.action == "skip"
    assert "closed_family" in decision.reason
    assert "suggested_closed_families" in decision.reason
    assert decision.evidence["closed_family"] == "closed_family"


def test_closed_family_below_threshold_does_not_skip():
    """One reject does not make a closed family. Threshold is
    CLOSED_FAMILY_REJECT_THRESHOLD = 2."""
    decision = evaluate(
        _proposal(family="rarely_failed", capability_ids=["C004"]),
        recent_digest=_digest(
            closed_families=[
                {"tag": "rarely_failed", "reject_count": CLOSED_FAMILY_REJECT_THRESHOLD - 1}
            ]
        ),
    )
    assert decision.action == "advance"


# ---------------------------------------------------------------------------
# Acceptance: 3. Weak signal / queue pressure → watch
# ---------------------------------------------------------------------------


def test_queue_saturation_downgrades_advance_to_watch():
    """When the queue already holds >= QUEUE_SATURATION_DEPTH active
    items, a brand-new proposal that would normally advance is
    throttled to watch so the queue does not balloon."""
    decision = evaluate(
        _proposal(family="fresh_family", capability_ids=["C005"]),
        queue_state=_queue(active=QUEUE_SATURATION_DEPTH),
    )
    assert decision.action == "watch"
    assert "queue" in decision.reason.lower()
    assert decision.evidence["queue_depth"] == QUEUE_SATURATION_DEPTH


def test_queue_below_saturation_advances():
    decision = evaluate(
        _proposal(family="fresh_family", capability_ids=["C005"]),
        queue_state=_queue(active=QUEUE_SATURATION_DEPTH - 1),
    )
    assert decision.action == "advance"


# ---------------------------------------------------------------------------
# Default: advance
# ---------------------------------------------------------------------------


def test_novel_proposal_advances_with_explicit_reason():
    decision = evaluate(_proposal(family="brand_new", capability_ids=["C006"]))
    assert decision.action == "advance"
    assert "brand_new" in decision.reason
    assert "C006" in decision.reason or "['C006']" in decision.reason


# ---------------------------------------------------------------------------
# Acceptance: 5. Reason is always non-empty and human-readable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        # Skip — duplicate
        {
            "proposal": _proposal(family="f", capability_ids=["C001"]),
            "recent_digest": _digest(
                recent_runs=[{"run_id": "r1", "family": "f", "capability_ids": ["C001"],
                                 "verdict": "rejected"}]
            ),
            "expected": "skip",
        },
        # Skip — closed family
        {
            "proposal": _proposal(family="closed", capability_ids=["C002"]),
            "recent_digest": _digest(
                closed_families=[{"tag": "closed", "reject_count": 5}]
            ),
            "expected": "skip",
        },
        # Watch — queue saturated
        {
            "proposal": _proposal(family="fresh", capability_ids=["C003"]),
            "queue_state": _queue(active=QUEUE_SATURATION_DEPTH),
            "expected": "watch",
        },
        # Advance — novel
        {
            "proposal": _proposal(family="x", capability_ids=["C007"]),
            "expected": "advance",
        },
    ],
)
def test_every_decision_has_human_readable_reason(scenario):
    expected = scenario.pop("expected")
    decision = evaluate(**scenario)
    assert decision.action == expected
    # Reason is a single non-empty sentence; not just a code, not empty.
    assert isinstance(decision.reason, str)
    assert len(decision.reason) >= 30, decision.reason
    assert decision.reason.endswith("."), (
        f"reason should be a sentence ending with '.': {decision.reason!r}"
    )
    # Evidence is a dict with at least one pointer.
    assert isinstance(decision.evidence, dict)
    assert decision.evidence, "evidence dict must not be empty"


# ---------------------------------------------------------------------------
# Robustness — bad/empty inputs do not crash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Acceptance: 2026-05-26 mandate — reject-chain defence
# Family is in an insight's follow_up_review_results.closed_families
# ---------------------------------------------------------------------------


def test_insight_closed_family_skips_when_no_active_critical_insight():
    """The v1_8 scenario: a deprecated insight has been augmented with
    closed_families that name the family the proposal carries. The
    proposal cites only the deprecated insight (its priority is still
    'critical' on disk but it has do_not_use_as_default_direction=true
    AND status starts with 'weakened/deprecated'), so the gate must
    treat it as not citing a current critical insight and skip."""
    insights = _insights(
        deprecated_with_closed_families=[
            ("disproven_insight_2026_05_24",
             ["reverse_moneyness_entry_filter",
              "volatility_position_scaling_reverse"]),
        ],
    )
    proposal = _proposal(
        family="volatility_position_scaling_reverse",
        capability_ids=["C001"],
        source_insight="disproven_insight_2026_05_24 — citing the original probe",
    )
    decision = evaluate(proposal, research_insights=insights)
    assert decision.action == "skip"
    assert "disproven_insight_2026_05_24" in decision.reason
    assert decision.evidence["insight_closed_family"] == "volatility_position_scaling_reverse"
    assert decision.evidence["closing_insight"] == "disproven_insight_2026_05_24"


def test_insight_closed_family_allows_when_proposal_cites_fresh_critical():
    """If a proposal in a closed family also cites a *different*,
    still-active critical insight, the gate must allow it through —
    fresh evidence is what re-opens a closed direction."""
    insights = _insights(
        critical=["fresh_critical_2026_06_01"],
        deprecated_with_closed_families=[
            ("disproven_insight",
             ["volatility_position_scaling_reverse"]),
        ],
    )
    proposal = _proposal(
        family="volatility_position_scaling_reverse",
        capability_ids=["C001"],
        source_insight="fresh_critical_2026_06_01 — new mechanism diagnostic",
    )
    decision = evaluate(proposal, research_insights=insights)
    assert decision.action == "advance"
    assert "fresh_critical_2026_06_01" in decision.reason


def test_critical_insight_marked_deprecated_does_not_count_as_evidence():
    """Citing an insight that has been demoted to deprecated /
    do_not_use_as_default_direction must not unlock anything. The
    proposal-cites-critical check used by Rules 1, 1b, and 2 must
    treat deprecated insights as if they did not exist."""
    insights = _insights(
        deprecated_with_closed_families=[
            ("disproven_insight",
             ["volatility_position_scaling_reverse"]),
        ],
    )
    proposal = _proposal(
        family="volatility_position_scaling_reverse",
        capability_ids=["C001"],
        source_insight="disproven_insight",
    )
    decision = evaluate(proposal, research_insights=insights)
    assert decision.action == "skip"


def test_insight_closed_family_does_not_affect_other_families():
    """A proposal whose family is NOT in any closed_families list
    behaves normally (advance, given no other gates fire)."""
    insights = _insights(
        deprecated_with_closed_families=[
            ("disproven_insight",
             ["reverse_moneyness_entry_filter"]),
        ],
    )
    proposal = _proposal(
        family="completely_unrelated_family",
        capability_ids=["C099"],
    )
    decision = evaluate(proposal, research_insights=insights)
    assert decision.action == "advance"


def test_insight_closed_family_records_evidence_for_audit():
    """Audit pointers must include the family + closing_insight id so
    a reader can reproduce the skip decision."""
    insights = _insights(
        deprecated_with_closed_families=[
            ("disproven_x",
             ["family_a", "family_b"]),
        ],
    )
    proposal = _proposal(family="family_b")
    decision = evaluate(proposal, research_insights=insights)
    assert decision.action == "skip"
    assert decision.evidence["insight_closed_family"] == "family_b"
    assert decision.evidence["closing_insight"] == "disproven_x"


def test_empty_history_advances_by_default():
    decision = evaluate(_proposal())
    assert decision.action == "advance"


def test_handles_missing_proposal_fields_gracefully():
    decision = evaluate({})
    assert decision.action in ACTIONS
    assert decision.reason


def test_rejects_non_dict_proposal():
    with pytest.raises(TypeError):
        evaluate("not a dict")  # type: ignore[arg-type]
