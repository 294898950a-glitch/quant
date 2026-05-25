"""Tests for ideation_policy_state — the recent-skip ledger that pre-
injects gate feedback into the next ideation prompt.

Mandate 2026-05-26: stop letting Hermes burn ideation API calls on
families that just got auto-skipped. After two same-family skips in
the rolling window, the family enters cooldown and the ideator prompt
must show it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import framework.autonomous.ideation_policy_state as ips


def test_load_state_missing_file_returns_skeleton(tmp_path):
    state = ips.load_state(tmp_path / "absent.json")
    assert state["schema_version"] == ips.SCHEMA_VERSION
    assert state["recent_skips"] == []


def test_load_state_malformed_file_returns_skeleton(tmp_path):
    p = tmp_path / "garbage.json"
    p.write_text("not json\n")
    state = ips.load_state(p)
    assert state["recent_skips"] == []


def test_record_skip_appends_to_recent_skips(tmp_path):
    p = tmp_path / "state.json"
    state = ips.record_skip(
        p,
        proposal_id="p1",
        family="family_a",
        closing_insight="insight_x",
        reason="closed by insight",
    )
    assert len(state["recent_skips"]) == 1
    entry = state["recent_skips"][0]
    assert entry["proposal_id"] == "p1"
    assert entry["family"] == "family_a"
    assert entry["closing_insight"] == "insight_x"
    # Round-trip persistence
    reloaded = ips.load_state(p)
    assert reloaded["recent_skips"][0]["family"] == "family_a"


def test_record_skip_trims_to_max_recent_skips(tmp_path):
    p = tmp_path / "state.json"
    for i in range(ips.MAX_RECENT_SKIPS + 5):
        ips.record_skip(
            p, proposal_id=f"p{i}", family=f"fam_{i}",
            closing_insight="x", reason="r",
        )
    state = ips.load_state(p)
    assert len(state["recent_skips"]) == ips.MAX_RECENT_SKIPS
    # newest preserved, oldest dropped
    assert state["recent_skips"][-1]["proposal_id"] == f"p{ips.MAX_RECENT_SKIPS + 4}"
    assert state["recent_skips"][0]["proposal_id"] == "p5"


def test_cooldown_families_single_skip_does_not_trigger(tmp_path):
    p = tmp_path / "state.json"
    ips.record_skip(p, proposal_id="p1", family="fam_a",
                    closing_insight="x", reason="r")
    state = ips.load_state(p)
    assert ips.cooldown_families(state) == []


def test_cooldown_families_two_same_family_skips_in_window_triggers(tmp_path):
    """The v1_8 / v1_6 / v1_9 scenario: same family hit two cron ticks
    in a row, both gate-skipped. Cooldown must list that family."""
    p = tmp_path / "state.json"
    ips.record_skip(p, proposal_id="p1", family="reverse_x",
                    closing_insight="ins_y", reason="closed")
    ips.record_skip(p, proposal_id="p2", family="reverse_x",
                    closing_insight="ins_y", reason="closed")
    state = ips.load_state(p)
    assert "reverse_x" in ips.cooldown_families(state)


def test_cooldown_families_different_families_no_trigger(tmp_path):
    p = tmp_path / "state.json"
    ips.record_skip(p, proposal_id="p1", family="fam_a",
                    closing_insight="x", reason="r")
    ips.record_skip(p, proposal_id="p2", family="fam_b",
                    closing_insight="x", reason="r")
    state = ips.load_state(p)
    assert ips.cooldown_families(state) == []


def test_cooldown_families_window_only_inspects_recent(tmp_path):
    """If the two same-family skips are outside the rolling window,
    cooldown does not fire."""
    p = tmp_path / "state.json"
    # Fill window with unrelated families, then put two same-family
    # skips far back in history.
    ips.record_skip(p, proposal_id="old1", family="reverse_x",
                    closing_insight="x", reason="r")
    ips.record_skip(p, proposal_id="old2", family="reverse_x",
                    closing_insight="x", reason="r")
    for i in range(ips.COOLDOWN_WINDOW):
        ips.record_skip(p, proposal_id=f"new{i}", family=f"unrelated_{i}",
                        closing_insight="x", reason="r")
    state = ips.load_state(p)
    # The two reverse_x skips are now outside the most-recent COOLDOWN_WINDOW
    assert "reverse_x" not in ips.cooldown_families(state)


def test_recent_skip_summary_default_count(tmp_path):
    p = tmp_path / "state.json"
    for i in range(8):
        ips.record_skip(p, proposal_id=f"p{i}", family=f"fam_{i}",
                        closing_insight="x", reason=f"reason_{i}")
    state = ips.load_state(p)
    summary = ips.recent_skip_summary(state)
    assert len(summary) == ips.RECENT_SUMMARY_DEFAULT
    # newest entries are at the end of the recent_skips list, and the
    # summary preserves that order
    assert summary[-1]["proposal_id"] == "p7"


def test_recent_skip_summary_custom_n(tmp_path):
    p = tmp_path / "state.json"
    for i in range(5):
        ips.record_skip(p, proposal_id=f"p{i}", family=f"fam_{i}",
                        closing_insight="x", reason="r")
    state = ips.load_state(p)
    assert len(ips.recent_skip_summary(state, n=2)) == 2
    assert len(ips.recent_skip_summary(state, n=10)) == 5


def test_recent_skip_summary_returns_required_fields(tmp_path):
    p = tmp_path / "state.json"
    ips.record_skip(p, proposal_id="p1", family="f1",
                    closing_insight="insight_a", reason="explained")
    state = ips.load_state(p)
    summary = ips.recent_skip_summary(state)
    entry = summary[0]
    assert "ts" in entry
    assert entry["proposal_id"] == "p1"
    assert entry["family"] == "f1"
    assert entry["closing_insight"] == "insight_a"
    assert entry["reason"] == "explained"


def test_state_file_is_human_readable_json(tmp_path):
    p = tmp_path / "state.json"
    ips.record_skip(p, proposal_id="p1", family="f1",
                    closing_insight="x", reason="r")
    text = p.read_text(encoding="utf-8")
    # Should be indented JSON (2-space), not a one-liner
    assert "\n" in text
    assert "  " in text
    # Parses cleanly
    data = json.loads(text)
    assert data["schema_version"] == ips.SCHEMA_VERSION
