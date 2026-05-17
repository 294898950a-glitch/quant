"""Tests for protected_action_audit mechanism added in cb_arb_value_gap_switch promotion.

validate_truth_sync.append_protected_action_audit auto-traces every change to:
- data/research_framework/current.yaml
- data/research_framework/baseline_registry.yaml
- data/research_framework/strategies.yaml
- data/research_framework/truth_sync_waivers/*.yaml

Records to data/research_framework/protected_action_audit.jsonl (append-only).
Event-hash dedup prevents duplicate rows for the same diff.

The audit is informational only: it does NOT grant approval, it records every
attempt regardless of whether errors blocked it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load_module():
    path = SCRIPTS / "validate_truth_sync.py"
    spec = importlib.util.spec_from_file_location("validate_truth_sync", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ==================== A. audit_relevant_paths ====================


def test_audit_relevant_paths_includes_truth_sync_paths():
    """truth_sync paths (current/baseline) always relevant."""
    v = _load_module()
    paths = [
        "data/research_framework/current.yaml",
        "scripts/some_unrelated.py",
    ]
    triggers = []
    relevant = v.audit_relevant_paths(paths, triggers)
    assert "data/research_framework/current.yaml" in relevant
    assert "scripts/some_unrelated.py" not in relevant


def test_audit_relevant_paths_includes_trigger_paths():
    """Trigger paths join relevant even if not in TRUTH_SYNC_PATHS."""
    v = _load_module()
    paths = ["strategies/cb_arb/verifier.py"]
    triggers = [{"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}]
    relevant = v.audit_relevant_paths(paths, triggers)
    assert "strategies/cb_arb/verifier.py" in relevant


def test_audit_relevant_paths_includes_waivers():
    """Waiver yaml paths considered relevant."""
    v = _load_module()
    paths = ["data/research_framework/truth_sync_waivers/test.yaml"]
    triggers = []
    relevant = v.audit_relevant_paths(paths, triggers)
    assert "data/research_framework/truth_sync_waivers/test.yaml" in relevant


def test_audit_relevant_paths_filters_unrelated():
    """Paths that don't match anything → not relevant."""
    v = _load_module()
    paths = ["README.txt", "src/some.py", "scripts/foo.py"]
    triggers = []
    relevant = v.audit_relevant_paths(paths, triggers)
    assert relevant == []


def test_audit_relevant_paths_strategies_yaml_included():
    """strategies.yaml is in the audit-relevant superset."""
    v = _load_module()
    paths = ["data/research_framework/strategies.yaml"]
    triggers = []
    relevant = v.audit_relevant_paths(paths, triggers)
    assert "data/research_framework/strategies.yaml" in relevant


# ==================== B. audit_event_hash ====================


def test_audit_event_hash_deterministic():
    """Same input → same hash."""
    v = _load_module()
    h1 = v.audit_event_hash(
        source="working-tree",
        relevant_paths=["data/research_framework/current.yaml"],
        triggers=[{"path": "x", "reason": "y"}],
        status="accepted_by_mechanical_sync",
        diff_text="diff",
    )
    h2 = v.audit_event_hash(
        source="working-tree",
        relevant_paths=["data/research_framework/current.yaml"],
        triggers=[{"path": "x", "reason": "y"}],
        status="accepted_by_mechanical_sync",
        diff_text="diff",
    )
    assert h1 == h2
    assert len(h1) == 24


def test_audit_event_hash_differs_on_diff_change():
    """Same paths/triggers but different diff text → different hash."""
    v = _load_module()
    common = dict(
        source="working-tree",
        relevant_paths=["x"],
        triggers=[],
        status="accepted_by_mechanical_sync",
    )
    h1 = v.audit_event_hash(**common, diff_text="old")
    h2 = v.audit_event_hash(**common, diff_text="new")
    assert h1 != h2


def test_audit_event_hash_differs_on_status():
    """blocked vs accepted → different hash even on same diff."""
    v = _load_module()
    common = dict(
        source="working-tree",
        relevant_paths=["x"],
        triggers=[],
        diff_text="diff",
    )
    h1 = v.audit_event_hash(**common, status="accepted_by_mechanical_sync")
    h2 = v.audit_event_hash(**common, status="blocked")
    assert h1 != h2


def test_audit_event_hash_differs_on_source():
    """working-tree vs staged → different hash."""
    v = _load_module()
    common = dict(
        relevant_paths=["x"],
        triggers=[],
        status="accepted_by_mechanical_sync",
        diff_text="diff",
    )
    h1 = v.audit_event_hash(**common, source="working-tree")
    h2 = v.audit_event_hash(**common, source="staged")
    assert h1 != h2


# ==================== C. existing_audit_hashes ====================


def test_existing_audit_hashes_empty_file(tmp_path: Path):
    """Empty/missing audit file → empty set."""
    v = _load_module()
    missing = tmp_path / "nope.jsonl"
    assert v.existing_audit_hashes(missing) == set()
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert v.existing_audit_hashes(empty) == set()


def test_existing_audit_hashes_parses_jsonl(tmp_path: Path):
    """Each line's event_hash extracted into set."""
    v = _load_module()
    f = tmp_path / "audit.jsonl"
    f.write_text(
        json.dumps({"event_hash": "abc123", "x": 1}) + "\n"
        + json.dumps({"event_hash": "def456", "y": 2}) + "\n"
    )
    assert v.existing_audit_hashes(f) == {"abc123", "def456"}


def test_existing_audit_hashes_skips_malformed_json(tmp_path: Path):
    """Bad json lines silently skipped (not crash)."""
    v = _load_module()
    f = tmp_path / "audit.jsonl"
    f.write_text(
        json.dumps({"event_hash": "ok1"}) + "\n"
        + "not-valid-json\n"
        + json.dumps({"event_hash": "ok2"}) + "\n"
    )
    assert v.existing_audit_hashes(f) == {"ok1", "ok2"}


# ==================== D. append_protected_action_audit ====================


def test_append_audit_writes_row_when_relevant(tmp_path: Path, monkeypatch):
    """Relevant path → audit row written."""
    v = _load_module()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(v, "diff_for_paths", lambda paths, source: "fake-diff")

    h = v.append_protected_action_audit(
        paths=["data/research_framework/current.yaml"],
        source="working-tree",
        triggers=[],
        errors=[],
        waiver_covered=[],
        audit_path=audit_path,
    )
    assert h is not None
    assert audit_path.exists()
    lines = audit_path.read_text().strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event_hash"] == h
    assert row["status"] == "accepted_by_mechanical_sync"
    assert row["source"] == "working-tree"
    assert "current.yaml" in row["truth_sync_paths_changed"][0]
    assert row["actor"] == "validate_truth_sync.py"
    # informational note
    assert "does not grant approval" in row["note"]


def test_append_audit_skips_irrelevant_paths(tmp_path: Path, monkeypatch):
    """No relevant paths → no row written, returns None."""
    v = _load_module()
    audit_path = tmp_path / "audit.jsonl"

    result = v.append_protected_action_audit(
        paths=["README.txt", "scripts/unrelated.py"],
        source="working-tree",
        triggers=[],
        errors=[],
        waiver_covered=[],
        audit_path=audit_path,
    )
    assert result is None
    assert not audit_path.exists()


def test_append_audit_dedups_same_event(tmp_path: Path, monkeypatch):
    """Calling twice with same diff/triggers → second call no-op (dedup)."""
    v = _load_module()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(v, "diff_for_paths", lambda paths, source: "same-diff")

    h1 = v.append_protected_action_audit(
        paths=["data/research_framework/current.yaml"],
        source="working-tree",
        triggers=[],
        errors=[],
        waiver_covered=[],
        audit_path=audit_path,
    )
    h2 = v.append_protected_action_audit(
        paths=["data/research_framework/current.yaml"],
        source="working-tree",
        triggers=[],
        errors=[],
        waiver_covered=[],
        audit_path=audit_path,
    )
    assert h1 == h2  # same hash returned both times
    lines = audit_path.read_text().strip().split("\n")
    assert len(lines) == 1  # only one row actually written


def test_append_audit_blocked_status_when_errors(tmp_path: Path, monkeypatch):
    """If errors list non-empty → status='blocked' (not accepted)."""
    v = _load_module()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(v, "diff_for_paths", lambda paths, source: "diff")

    v.append_protected_action_audit(
        paths=["data/research_framework/current.yaml"],
        source="working-tree",
        triggers=[{"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}],
        errors=["truth-affecting change without waiver"],
        waiver_covered=[],
        audit_path=audit_path,
    )
    row = json.loads(audit_path.read_text().strip())
    assert row["status"] == "blocked"
    assert row["errors"] == ["truth-affecting change without waiver"]


def test_append_audit_records_waiver_paths(tmp_path: Path, monkeypatch):
    """waiver_covered paths preserved sorted."""
    v = _load_module()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(v, "diff_for_paths", lambda paths, source: "diff")

    v.append_protected_action_audit(
        paths=["data/research_framework/baseline_registry.yaml"],
        source="staged",
        triggers=[{"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}],
        errors=[],
        waiver_covered=["strategies/cb_arb/verifier.py", "strategies/cb_arb/orchestrator_main.py"],
        audit_path=audit_path,
    )
    row = json.loads(audit_path.read_text().strip())
    # Sorted alphabetically
    assert row["waiver_covered_paths"] == [
        "strategies/cb_arb/orchestrator_main.py",
        "strategies/cb_arb/verifier.py",
    ]


def test_append_audit_changes_hash_on_status_flip(tmp_path: Path, monkeypatch):
    """Same diff + paths but status flips (block → accept after fix) → new row."""
    v = _load_module()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(v, "diff_for_paths", lambda paths, source: "diff")

    h_blocked = v.append_protected_action_audit(
        paths=["data/research_framework/current.yaml"],
        source="working-tree",
        triggers=[{"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}],
        errors=["block reason"],
        waiver_covered=[],
        audit_path=audit_path,
    )
    h_accepted = v.append_protected_action_audit(
        paths=["data/research_framework/current.yaml"],
        source="working-tree",
        triggers=[{"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}],
        errors=[],
        waiver_covered=["strategies/cb_arb/verifier.py"],
        audit_path=audit_path,
    )
    assert h_blocked != h_accepted
    lines = audit_path.read_text().strip().split("\n")
    assert len(lines) == 2


# ==================== E. integration with real repo audit log ====================


def test_real_audit_log_loadable():
    """The real protected_action_audit.jsonl on disk is parseable jsonl."""
    audit_path = REPO_ROOT / "data" / "research_framework" / "protected_action_audit.jsonl"
    if not audit_path.exists():
        pytest.skip("audit log not yet created")
    for line in audit_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        assert "event_hash" in row
        assert "status" in row
        assert "recorded_at" in row
        assert "actor" in row
        assert "note" in row
        assert "does not grant approval" in row["note"]
