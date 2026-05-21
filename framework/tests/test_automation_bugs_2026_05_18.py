"""Acceptance tests for 9 automation bugs discovered 2026-05-18.

Categorized:
- P0 (broken behavior, wastes compute):
  - Bug 1: 6 option-pnl-feedback batch summary CSV identical md5 (different hypothesis → same evaluator/grid)
  - Bug 2: position-sizing 2 VM jobs same spec → identical CSV (no dedup)
  - Bug 3: orchestrator_log only "paused" action (production runner doesn't use orchestrator)
- P1 (schema violations, preflight FAIL):
  - Bug 4: result_reviewer.py writes report.yaml missing 5 required fields
  - Bug 5: l4_ack reviewer field accepts "codex_auto" but ALLOWED_REVIEWER = {claude, codex, user}
  - Bug 6: l4_ack auto-fill missing answer + computed_at fields
- P2 (design gaps):
  - Bug 7: Loop pid restarted 4 times (no healthcheck recorded)
  - Bug 8: Codex outbox heartbeat noise (same content every 10min)
  - Bug 9: Cycle detection uses hypothesis hash, not mechanic_tag hash
  - Bug 10: Production research queue runner ignores user pause flag

Codex must make these tests pass.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from framework.autonomous.queue_remote_execution import QueueRemoteExecutionService
from framework.autonomous.queue_review_memory import QueueReviewMemoryService


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(module_path: Path, name: str):
    if not module_path.exists():
        pytest.skip(f"{module_path} not implemented yet")
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ====================== Bug 1 + 2: identical CSV across batches ======================


def test_bug_1_pnl_feedback_csvs_must_not_all_be_identical():
    """If 6 batches with DIFFERENT hypotheses produced byte-identical summary CSVs,
    the spec compiler / evaluator is not actually using the proposal mechanics."""
    pattern = "data/cb_arb_value_gap_switch_option-pnl-feedback_2026-05-18_*/summary_option_pnl_feedback.csv"
    paths = sorted(REPO_ROOT.glob(pattern))
    if len(paths) < 2:
        pytest.skip("Not enough pnl-feedback batches to test")

    hashes = set()
    for p in paths:
        hashes.add(hashlib.md5(p.read_bytes()).hexdigest())

    # If all 6 batches have different hypotheses but produce same CSV → bug
    assert len(hashes) > 1, (
        f"All {len(paths)} option-pnl-feedback batches produced identical summary CSV. "
        f"Different hypotheses should result in different variant grids or at least "
        f"different parameter values. Cycle detection or executor binding likely broken. "
        f"Common hash: {next(iter(hashes))}"
    )


def test_bug_2_position_sizing_dual_vm_not_duplicate_work():
    """If 2 position-sizing batches started ~9 min apart on different VMs produced
    byte-identical CSVs, Spec Compiler dedup failed or design intent unrecorded."""
    pattern = "data/cb_arb_value_gap_switch_option-position-sizing_2026-05-18_*/summary_option_position_sizing.csv"
    paths = sorted(REPO_ROOT.glob(pattern))
    if len(paths) < 2:
        pytest.skip("Not enough position-sizing batches")

    hashes = {hashlib.md5(p.read_bytes()).hexdigest() for p in paths}
    if len(hashes) == 1:
        # Either dedup-failure bug OR documented cross-VM verification intent.
        # If cross-VM is intent, manifests should record `cross_vm_verification: true`.
        manifests = []
        for csv_path in paths:
            run_dir = csv_path.parent
            manifest = run_dir / "manifest.yaml"
            spec = run_dir / "spec.yaml"
            for mf in (manifest, spec):
                if mf.exists():
                    data = yaml.safe_load(mf.read_text())
                    manifests.append(data)
        cross_vm_intent = any(
            (d or {}).get("cross_vm_verification") or
            (d or {}).get("automation", {}).get("cross_vm_verification")
            for d in manifests
        )
        assert cross_vm_intent, (
            f"{len(paths)} position-sizing batches with identical CSV. "
            f"Either dedup bug, or manifest must declare cross_vm_verification: true."
        )


# ====================== Bug 3: orchestrator audit log content ======================


def test_bug_3_orchestrator_log_records_more_than_paused():
    """Audit log should include all major actions: ideate / compile / run / review / digest.
    27 entries all 'paused' = audit broken or production runner doesn't use orchestrator."""
    log_path = REPO_ROOT / "data" / "research_framework" / "orchestrator_log.jsonl"
    if not log_path.exists():
        pytest.skip("orchestrator_log not yet created")

    actions = set()
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        actions.add(row.get("action"))

    # Acceptance: should see at least 3 distinct action types over real loop activity
    expected_at_least = {"paused", "ideate", "compile"}
    intersection = actions & expected_at_least
    # If only "paused" exists, real loop work was not audited (BUG)
    assert len(actions) > 1, (
        f"orchestrator_log contains only {actions}. Production runner "
        f"(research_queue_runner.py) should also write audit entries for "
        f"ideate / compile / run / review / digest, but only 'paused' is recorded. "
        f"Total entries: {len(log_path.read_text().splitlines())}"
    )


def test_bug_3b_production_runner_writes_audit():
    """research_queue_runner.py should write orchestrator_log entries
    when it ideates / compiles / runs / reviews."""
    runner_path = REPO_ROOT / "scripts" / "research_queue_runner.py"
    if not runner_path.exists():
        pytest.skip("research_queue_runner.py not present")
    source = runner_path.read_text()
    # Source must reference orchestrator_log or audit log path
    has_audit = (
        "orchestrator_log" in source
        or ("audit" in source.lower() and "log" in source.lower())
    )
    assert has_audit, (
        "research_queue_runner.py does not reference orchestrator_log or audit log. "
        "Production runner should record each major action per acceptance criteria guard E."
    )


def test_bug_10_research_queue_runner_respects_pause_flag(tmp_path: Path, monkeypatch):
    """Production research queue runner must stop before ideation or VM scheduling
    when the user pause flag exists."""
    runner = _load(REPO_ROOT / "scripts" / "research_queue_runner.py", "research_queue_runner")
    pause_flag = tmp_path / "orchestrator_paused.flag"
    pause_flag.write_text("paused by regression test", encoding="utf-8")
    missing_state = tmp_path / "missing_research_queue.yaml"
    status_path = tmp_path / "research_queue_status.json"
    audit_path = tmp_path / "orchestrator_log.jsonl"

    monkeypatch.setattr(runner, "PAUSE_FLAG_PATH", pause_flag)
    monkeypatch.setattr(runner, "STATE_PATH", missing_state)
    monkeypatch.setattr(runner, "STATUS_PATH", status_path)
    monkeypatch.setattr(runner, "AUDIT_LOG_PATH", audit_path)

    assert runner.tick() == "paused"

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "paused"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "paused"


def test_research_queue_runner_respects_protected_user_escalation_block(tmp_path: Path, monkeypatch):
    """If state explicitly requires a protected user decision, runner must stop."""
    runner = _load(REPO_ROOT / "scripts" / "research_queue_runner.py", "research_queue_runner")
    state_path = tmp_path / "research_queue.yaml"
    status_path = tmp_path / "research_queue_status.json"
    audit_path = tmp_path / "orchestrator_log.jsonl"
    pause_flag = tmp_path / "missing_pause.flag"
    state_path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "escalation": {
                    "status": "blocked_awaiting_user",
                    "reason": "all directions exhausted",
                    "requires_user_decision": True,
                    "user_options": {"A": "archive", "B": "new strategy"},
                },
                "queue": [{"id": "done", "status": "complete"}],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runner, "STATE_PATH", state_path)
    monkeypatch.setattr(runner, "STATUS_PATH", status_path)
    monkeypatch.setattr(runner, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(runner, "PAUSE_FLAG_PATH", pause_flag)

    assert runner.tick() == "blocked_awaiting_user"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "blocked_awaiting_user"
    assert "all directions exhausted" in status["reason"]
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "blocked_awaiting_user"


def test_research_queue_runner_auto_resolves_exhaustion_block(tmp_path: Path, monkeypatch):
    """Exhausted old directions are not a hard stop; they should enter project ideation."""
    runner = _load(REPO_ROOT / "scripts" / "research_queue_runner.py", "research_queue_runner_auto_resolve")
    state_path = tmp_path / "research_queue.yaml"
    status_path = tmp_path / "research_queue_status.json"
    audit_path = tmp_path / "orchestrator_log.jsonl"
    pause_flag = tmp_path / "missing_pause.flag"
    state_path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "escalation": {
                    "status": "blocked_awaiting_user",
                    "reason": "old directions exhausted",
                },
                "queue": [{"id": "done", "status": "complete"}],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runner, "STATE_PATH", state_path)
    monkeypatch.setattr(runner, "STATUS_PATH", status_path)
    monkeypatch.setattr(runner, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(runner, "PAUSE_FLAG_PATH", pause_flag)
    class FakeIdeationService:
        def generate_until_actionable(self, state):
            return "queued_ideation_spec"

    monkeypatch.setattr(runner, "ideation_service", lambda: FakeIdeationService())

    assert runner.tick() == "queued_ideation_spec"


def test_research_queue_completion_requires_review_and_digest(tmp_path: Path, monkeypatch):
    """A remote run is not complete until artifacts are reviewed and digest is refreshed."""
    runner = _load(REPO_ROOT / "scripts" / "research_queue_runner.py", "research_queue_runner_review_gate")
    run_dir = tmp_path / "data" / "run_review_gate"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_review_gate",
                "strategy_id": "cb_arb_value_gap_switch",
                "artifacts_required": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for name in ("summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"):
        (run_dir / name).write_text("{}\n", encoding="utf-8")

    calls: list[str] = []
    state = {}
    item = {"id": "run_review_gate", "status": "running", "spec_path": "data/run_review_gate/spec.yaml"}
    queue = [item]
    rel = lambda path: str(path.resolve().relative_to(tmp_path))
    remote = QueueRemoteExecutionService(
        repo_root=tmp_path,
        save_state=lambda state: None,
        write_status=lambda status, extra=None: None,
        audit=lambda action, payload=None: None,
        log=lambda message: None,
        mark_history=runner.mark_history,
        rel=rel,
        now_iso=lambda: "2026-05-21T00:00:00",
        issue_ticket=lambda purpose: {"path": "/tmp/ticket", "token": "token"},
        remote_running_on_vm=lambda vm, pattern: False,
        sync_remote_run_dir=lambda state, item, run_dir: calls.append("sync"),
        item_vm_config=lambda state, item: {"id": "vm1", "host": "vm", "remote_repo": "/repo"},
    )
    review = QueueReviewMemoryService(
        repo_root=tmp_path,
        run=lambda *args, **kwargs: None,
        save_state=lambda state: None,
        audit=lambda action, payload=None: None,
        mark_history=runner.mark_history,
        rel=rel,
        now_iso=lambda: "2026-05-21T00:00:00",
    )
    monkeypatch.setattr(
        review,
        "review_and_digest_run",
        lambda run_path: calls.append("review_digest") or {
            "review_path": "data/run_review_gate/review.yaml",
            "digest_path": "data/research_framework/recent_results_digest.yaml",
        },
    )

    assert remote.settle_running_items(state, queue) == 1
    assert item["status"] == "review_pending"
    assert review.review_pending_items(state, queue) == 1
    assert calls == ["sync", "review_digest"]
    assert item["status"] == "complete"
    assert item["workflow_stage"] == "digested"
    assert item["review_path"] == "data/run_review_gate/review.yaml"


def test_ideation_uses_current_yaml_strategy_not_hardcoded():
    """The ideator must read the current main strategy instead of hard-coding cb_arb."""
    source = (REPO_ROOT / "framework" / "autonomous" / "ideation_cycle.py").read_text(encoding="utf-8")
    assert '"allowed_strategy_id": "cb_arb_value_gap_switch"' not in source
    assert "current_main_strategy_id" in source
    assert "self.paths.current" in source


# ====================== Bug 4-6: schema violations in auto-fill ======================


def test_bug_4_report_yaml_includes_all_required_fields(tmp_path: Path, monkeypatch):
    """When result_reviewer.py writes report.yaml, it must include all
    REQUIRED_FIELDS from scripts/validate_report.py.

    Current bug: position-sizing batches' report.yaml miss 5 fields.
    """
    validate_report = _load(REPO_ROOT / "scripts" / "validate_report.py", "validate_report")
    required = validate_report.REQUIRED_FIELDS

    # Sample a real autogen report.yaml from position-sizing batch
    candidates = list((REPO_ROOT / "data").glob(
        "cb_arb_value_gap_switch_option-position-sizing_2026-05-18_*/report.yaml"
    ))
    if not candidates:
        pytest.skip("no autogen report.yaml to test")

    for path in candidates:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        missing = required - set(data.keys())
        assert not missing, (
            f"{path.relative_to(REPO_ROOT)} missing required fields {missing}. "
            f"result_reviewer auto-fill must include all schema-required fields "
            f"per scripts/validate_report.py."
        )


def test_bug_5_l4_ack_reviewer_field_uses_allowed_token():
    """l4_ack.yaml reviewer must be in ALLOWED_REVIEWER set.
    Current bug: position-sizing l4_ack.yaml uses 'codex_auto' but only
    {codex, claude, user} allowed."""
    validate_l4 = _load(REPO_ROOT / "scripts" / "validate_l4_ack.py", "validate_l4_ack")
    allowed = validate_l4.ALLOWED_REVIEWER

    candidates = list((REPO_ROOT / "data").glob(
        "cb_arb_value_gap_switch_option-position-sizing_2026-05-18_*/l4_ack.yaml"
    ))
    if not candidates:
        pytest.skip("no autogen l4_ack.yaml")

    for path in candidates:
        data = yaml.safe_load(path.read_text())
        reviewer = (data or {}).get("reviewer")
        if reviewer is None:
            continue
        # Either: reviewer is in allowed, OR allowed has been extended to include _auto
        in_set = reviewer in allowed
        assert in_set, (
            f"{path.relative_to(REPO_ROOT)} reviewer='{reviewer}' not in "
            f"ALLOWED_REVIEWER={allowed}. Fix: either reviewer string normalization in "
            f"result_reviewer.py, or add 'codex_auto'/'claude_auto' to ALLOWED_REVIEWER."
        )


def test_bug_6_l4_ack_has_answer_and_computed_at(tmp_path: Path):
    """Auto-filled l4_ack questions must have answer field and computed_at timestamp."""
    candidates = list((REPO_ROOT / "data").glob(
        "cb_arb_value_gap_switch_option-position-sizing_2026-05-18_*/l4_ack.yaml"
    ))
    if not candidates:
        pytest.skip("no autogen l4_ack.yaml")

    for path in candidates:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        for qkey in ("q1_floor_binding", "q3_baseline_alignment", "q4_monotonic", "q5_trade_overlap"):
            q = data.get(qkey)
            if not isinstance(q, dict):
                continue
            assert "answer" in q, f"{path.relative_to(REPO_ROOT)} {qkey} missing 'answer' field (auto fill incomplete)"
            assert "computed_at" in q, f"{path.relative_to(REPO_ROOT)} {qkey} missing 'computed_at' timestamp"


# ====================== Bug 9: Cycle detection by mechanic_tag ======================


def test_bug_9_cycle_detection_uses_mechanic_tag_not_only_hypothesis():
    """Spec Compiler cycle_detection should consider mechanic_tag overlap,
    not just hypothesis text hash.

    Current bug: 6 option-pnl-feedback batches with DIFFERENT hypothesis text but SAME
    mechanic family passed cycle detection.
    """
    compiler_path = REPO_ROOT / "framework" / "autonomous" / "spec_compiler.py"
    if not compiler_path.exists():
        pytest.skip("spec_compiler not present")
    source = compiler_path.read_text().lower()

    # Source should hash mechanic_tag in cycle detection logic,
    # not only hypothesis text.
    has_mechanic_in_cycle = (
        ("mechanic" in source and "cycle" in source)
        or "mechanics_hash" in source
        or ("hypothesis" in source and "mechanics" in source and "hash" in source)
    )
    assert has_mechanic_in_cycle, (
        "spec_compiler.py cycle detection should hash (hypothesis + mechanics) "
        "or include mechanic_tag overlap check, not only hypothesis text. "
        "Current production behavior: 6 distinct hypotheses all passed cycle "
        "detection but produced identical CSV (same mechanics family)."
    )


# ====================== Bug 7 + 8 (advisory tests, design checks) ======================


def test_bug_7_loop_runner_has_no_restart_mode():
    """The production runner is now tick-only, so restart history is unnecessary."""
    runner_path = REPO_ROOT / "scripts" / "research_queue_runner.py"
    if not runner_path.exists():
        pytest.skip()
    source = runner_path.read_text().lower()
    assert "daemon_loop" not in source
    assert "pid_path" not in source
    assert "restart_log_path" not in source


def test_bug_8_heartbeat_should_dedup_unchanged_status():
    """Heartbeat 'status_unchanged' should be a short single-line entry, not
    full 1669-byte content repeated every 10 minutes."""
    runner_path = REPO_ROOT / "scripts" / "research_queue_runner.py"
    if not runner_path.exists():
        pytest.skip()
    source = runner_path.read_text().lower()
    has_dedup = (
        "status_changed" in source
        or "previous_status" in source
        or "dedup" in source
        or "unchanged" in source
    )
    assert has_dedup, (
        "research queue runner should emit minimal heartbeat when status unchanged. "
        "Currently emits full ~1669-byte content every 10 minutes regardless. "
        "Recommendation: full content only when status flips; otherwise 1 line."
    )
