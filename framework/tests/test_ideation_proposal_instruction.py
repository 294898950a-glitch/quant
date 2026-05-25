"""Tests for the proposal_instruction prompt-construction helper.

Mandate 2026-05-26 step 1 + 3: the ideator must show Hermes the
closed_families list (out-of-band ban derived from insight follow-up
reviews) and the recent gate-skip summary BEFORE it asks for a new
proposal. This file pins those contract points.
"""

from __future__ import annotations

import pytest

from framework.autonomous.ideation_cycle import (
    _critical_insights,
    _closed_families_from_insights,
    proposal_instruction,
)
from framework.autonomous import ideation_policy_state as ips


def _stub_inputs():
    """Minimal valid inputs that pass through proposal_instruction
    without tripping any field-level validators."""
    closed_tags: dict = {}
    digest: dict = {"recent_runs": [], "suggested_closed_families": []}
    cap_menu: dict = {}
    current: dict = {}
    data_inventory: dict = {"available": False}
    return closed_tags, digest, cap_menu, current, data_inventory


# --- _critical_insights filtering ------------------------------------


def test_critical_insights_skips_deprecated_status():
    research_insights = {
        "key_insights": [
            {"id": "active_one", "priority": "critical"},
            {"id": "demoted_via_status", "priority": "critical",
             "status": "deprecated_2026_05_26"},
            {"id": "demoted_via_status_weakened", "priority": "critical",
             "status": "weakened_by_follow_up_rejects_test"},
            {"id": "demoted_via_flag", "priority": "critical",
             "do_not_use_as_default_direction": True},
            {"id": "non_critical", "priority": "medium"},
        ]
    }
    active = _critical_insights(research_insights)
    ids = {it.get("id") for it in active}
    assert ids == {"active_one"}


# --- _closed_families_from_insights ----------------------------------


def test_closed_families_aggregates_across_insights():
    research_insights = {
        "key_insights": [
            {
                "id": "insight_one",
                "priority": "critical",
                "do_not_use_as_default_direction": True,
                "follow_up_review_results": {
                    "closed_families": ["family_a", "family_b"],
                },
            },
            {
                "id": "insight_two",
                "priority": "critical",
                "follow_up_review_results": {
                    "closed_families": ["family_c"],
                },
            },
            {
                "id": "insight_no_followup",
                "priority": "critical",
            },
        ]
    }
    closed = _closed_families_from_insights(research_insights)
    family_to_insight = {c["family"]: c["closed_by_insight"] for c in closed}
    assert family_to_insight == {
        "family_a": "insight_one",
        "family_b": "insight_one",
        "family_c": "insight_two",
    }


# --- proposal_instruction injection ----------------------------------


def test_prompt_carries_closed_families_field_and_hard_rule():
    closed_tags, digest, cap_menu, current, data_inv = _stub_inputs()
    research_insights = {
        "key_insights": [
            {
                "id": "demoted_insight",
                "priority": "critical",
                "do_not_use_as_default_direction": True,
                "follow_up_review_results": {
                    "closed_families": ["fam_x", "fam_y"],
                },
            }
        ]
    }
    prompt = proposal_instruction(
        closed_tags, digest, cap_menu, current,
        data_inventory=data_inv,
        research_insights=research_insights,
    )
    assert "closed_families_from_insights" in prompt
    families = [c["family"] for c in prompt["closed_families_from_insights"]]
    assert set(families) == {"fam_x", "fam_y"}
    # And a hard_rule that names the field must be present so Hermes
    # has explicit instruction.
    assert any("closed_families_from_insights" in r for r in prompt["hard_rules"])


def test_prompt_carries_cooldown_when_policy_state_has_repeated_family(tmp_path):
    closed_tags, digest, cap_menu, current, data_inv = _stub_inputs()
    state_path = tmp_path / "state.json"
    ips.record_skip(state_path, proposal_id="p1", family="reverse_x",
                    closing_insight="ins", reason="r")
    ips.record_skip(state_path, proposal_id="p2", family="reverse_x",
                    closing_insight="ins", reason="r")
    policy_state = ips.load_state(state_path)
    prompt = proposal_instruction(
        closed_tags, digest, cap_menu, current,
        data_inventory=data_inv,
        ideation_policy_state=policy_state,
    )
    assert "cooldown_families" in prompt
    assert "reverse_x" in prompt["cooldown_families"]
    assert any("cooldown_families" in r for r in prompt["hard_rules"])


def test_prompt_carries_recent_skip_summary_when_policy_state_has_entries(tmp_path):
    closed_tags, digest, cap_menu, current, data_inv = _stub_inputs()
    state_path = tmp_path / "state.json"
    ips.record_skip(state_path, proposal_id="p_recent", family="fam_z",
                    closing_insight="ins_a", reason="closed because xyz")
    policy_state = ips.load_state(state_path)
    prompt = proposal_instruction(
        closed_tags, digest, cap_menu, current,
        data_inventory=data_inv,
        ideation_policy_state=policy_state,
    )
    assert "recent_gate_skip_summary" in prompt
    summary = prompt["recent_gate_skip_summary"]
    assert len(summary) == 1
    assert summary[0]["proposal_id"] == "p_recent"
    assert summary[0]["family"] == "fam_z"
    assert summary[0]["closing_insight"] == "ins_a"
    assert any("recent_gate_skip_summary" in r for r in prompt["hard_rules"])


def test_prompt_omits_cooldown_hard_rule_when_no_cooldown(tmp_path):
    """The cooldown hard_rule must NOT appear when no family hit the
    threshold — otherwise the prompt grows noisy on every call."""
    closed_tags, digest, cap_menu, current, data_inv = _stub_inputs()
    state_path = tmp_path / "state.json"
    # Single skip — below threshold, no cooldown
    ips.record_skip(state_path, proposal_id="p1", family="solo",
                    closing_insight="x", reason="r")
    policy_state = ips.load_state(state_path)
    prompt = proposal_instruction(
        closed_tags, digest, cap_menu, current,
        data_inventory=data_inv,
        ideation_policy_state=policy_state,
    )
    assert prompt["cooldown_families"] == []
    assert not any("cooldown_families" in r for r in prompt["hard_rules"])


def test_prompt_omits_closed_families_hard_rule_when_no_insight_has_closed(tmp_path):
    closed_tags, digest, cap_menu, current, data_inv = _stub_inputs()
    research_insights = {
        "key_insights": [
            {"id": "x", "priority": "critical"},  # no follow_up_review_results
        ]
    }
    prompt = proposal_instruction(
        closed_tags, digest, cap_menu, current,
        data_inventory=data_inv,
        research_insights=research_insights,
    )
    assert prompt["closed_families_from_insights"] == []
    assert not any(
        "closed_families_from_insights" in r for r in prompt["hard_rules"]
    )
