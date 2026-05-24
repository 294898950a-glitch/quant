"""Tests for the install_generated_executors compliance + recompile path.

Covers the two changes added 2026-05-25:

A. After a successful install, DRAFT specs whose required_executor matches
   the just-installed target are re-run through spec_compiler. If compile
   returns READY, the spec file is rewritten by the compiler. If compile
   still returns DRAFT/REJECT, the result lands in spec_recompile_blocked
   so the install summary surfaces the reason instead of silently absorbing.

B. The handoff-time validator rejects an executor that does not import
   GateKeeper as compliance_failed (not generic "skipped"), writes a
   compliance_repair_request.yaml next to the generated_executor source,
   and flips the handoff task back to status=needs_compliance_repair so
   the next handoff tick can re-claim it.

The tests do not depend on Hermes, Tencent CVM, or any external state.
They construct a fake repo layout under tmp_path and invoke the install
script's helpers directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import scripts.install_generated_executors as install_mod


GATEKEEPER_IMPORT = "from scripts.gatekeeper import GateKeeper"


def _write_executor(path: Path, *, with_gatekeeper: bool, with_main: bool = True,
                     with_declare: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/usr/bin/env python3"]
    if with_gatekeeper:
        lines.append(GATEKEEPER_IMPORT)
    if with_main:
        lines.append("def main():\n    return 0\n")
    if with_declare:
        lines.append("def declare_data_requirements(*a, **k):\n    return {}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_compliance_pass_executor_returns_no_errors(tmp_path):
    src = tmp_path / "executor.py"
    _write_executor(src, with_gatekeeper=True)
    errors = install_mod.validate_executor(src)
    assert errors == [], f"compliant executor should produce no errors, got {errors}"


def test_compliance_fail_missing_gatekeeper_is_flagged(tmp_path):
    src = tmp_path / "executor.py"
    _write_executor(src, with_gatekeeper=False)
    errors = install_mod.validate_executor(src)
    assert any(e.startswith("compliance_failed:") for e in errors), (
        f"missing GateKeeper must surface compliance_failed, got {errors}"
    )


def test_compliance_fail_writes_repair_request_marker(tmp_path, monkeypatch):
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data" / "run_x"
    run_dir.mkdir(parents=True)
    src = run_dir / "generated_executor" / "executor.py"
    _write_executor(src, with_gatekeeper=False)
    task = {
        "id": "task_x",
        "target_script_path": "scripts/evaluate_x.py",
        "run_dir": "data/run_x",
        "descriptor_path": "data/run_x/executor_tool_request.yaml",
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "compliance_failed"
    marker = run_dir / "generated_executor" / "compliance_repair_request.yaml"
    assert marker.exists(), "compliance_failed must write a repair_request marker"
    payload = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert payload["handoff_id"] == "task_x"
    assert any("GateKeeper" in e for e in payload["compliance_errors"])
    assert "GateKeeper" in payload["required_fix"]


def test_recompile_drafts_after_install_routes_correct_specs(tmp_path, monkeypatch):
    """recompile_drafts_after_install must (a) pick up DRAFT specs whose
    required_executor matches an installed target, (b) call spec_compiler
    on them, (c) propagate READY status into the result dict.

    We monkey-patch spec_compiler.compile to return a deterministic READY so
    the test isolates the routing logic from the (separately-tested)
    compiler internals.
    """
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    monkeypatch.setattr(install_mod, "REGISTRY_PATH",
                          repo / "data/research_framework/executor_registry.yaml")
    registry_dir = repo / "data/research_framework"
    registry_dir.mkdir(parents=True)
    (registry_dir / "executor_registry.yaml").write_text(
        yaml.safe_dump({
            "executors": {
                "evaluate_x": {
                    "id": "evaluate_x",
                    "script_path": "scripts/evaluate_x.py",
                    "status": "implemented",
                },
            },
        }, allow_unicode=True),
        encoding="utf-8",
    )
    run_dir = repo / "data/run_y"
    run_dir.mkdir()
    (run_dir / "proposal.yaml").write_text(
        yaml.safe_dump({"proposal_id": "run_y", "required_executor": "evaluate_x"},
                         allow_unicode=True),
        encoding="utf-8",
    )
    (run_dir / "spec.yaml").write_text(
        yaml.safe_dump({"status": "DRAFT", "required_executor": "evaluate_x"},
                         allow_unicode=True),
        encoding="utf-8",
    )
    # Provide a fake spec_compiler.compile that always returns READY.
    import sys as _sys
    real_repo = Path(__file__).resolve().parents[2]
    if str(real_repo) not in _sys.path:
        _sys.path.insert(0, str(real_repo))
    import framework.autonomous.spec_compiler as sc

    class _FakeResult:
        def __init__(self):
            self.status = "READY"
            self.reason = "fake_ok"
    fake_calls = []

    def fake_compile(*, proposal, registry, closed_tags, recent_proposals, output_dir):
        fake_calls.append({"proposal_id": proposal.get("proposal_id"),
                              "required_executor": proposal.get("required_executor"),
                              "output_dir": str(output_dir)})
        # Real compiler rewrites spec.yaml; emulate that here so the post-
        # condition can assert the file was touched.
        Path(output_dir, "spec.yaml").write_text(
            yaml.safe_dump({"status": "READY", "required_executor": "evaluate_x"},
                             allow_unicode=True),
            encoding="utf-8",
        )
        return _FakeResult()

    monkeypatch.setattr(sc, "compile", fake_compile)
    outcome = install_mod.recompile_drafts_after_install(
        {"scripts/evaluate_x.py"}, dry_run=False
    )
    assert fake_calls, "spec_compiler.compile should have been invoked"
    assert fake_calls[0]["required_executor"] == "evaluate_x"
    assert any(r.get("new_status") == "READY" for r in outcome.get("recompiled", [])), (
        f"routing should record READY in recompiled bucket; got {outcome}"
    )
    new_spec = yaml.safe_load((run_dir / "spec.yaml").read_text(encoding="utf-8"))
    assert new_spec.get("status") == "READY"


def test_recompile_drafts_skips_ready_specs(tmp_path, monkeypatch):
    """READY specs are never re-touched even if their executor was just installed."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    monkeypatch.setattr(install_mod, "REGISTRY_PATH",
                          repo / "data/research_framework/executor_registry.yaml")
    (repo / "data/research_framework").mkdir(parents=True)
    (repo / "data/research_framework/executor_registry.yaml").write_text(
        yaml.safe_dump({"executors": {"evaluate_x": {"script_path": "scripts/evaluate_x.py"}}}),
        encoding="utf-8",
    )
    run_dir = repo / "data/run_ready"
    run_dir.mkdir()
    ready_spec_text = yaml.safe_dump(
        {"status": "READY", "required_executor": "evaluate_x", "important": "preserved"},
        allow_unicode=True,
    )
    (run_dir / "spec.yaml").write_text(ready_spec_text, encoding="utf-8")
    # No proposal.yaml; recompile would normally skip on this anyway, but
    # the key invariant is that READY specs never reach the compile call.
    outcome = install_mod.recompile_drafts_after_install(
        {"scripts/evaluate_x.py"}, dry_run=False
    )
    # READY spec must not appear in recompiled or blocked or skipped — it was
    # filtered before any compile attempt.
    for bucket in ("recompiled", "blocked", "skipped"):
        for r in outcome.get(bucket, []) or []:
            assert "run_ready" not in r.get("spec", ""), (
                f"READY spec must not be touched by recompile; appeared in {bucket}: {r}"
            )
    # File on disk untouched.
    after_text = (run_dir / "spec.yaml").read_text(encoding="utf-8")
    assert after_text == ready_spec_text


def test_install_one_compliant_executor_returns_installed(tmp_path, monkeypatch):
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data/run_z"
    run_dir.mkdir(parents=True)
    src = run_dir / "generated_executor" / "executor.py"
    _write_executor(src, with_gatekeeper=True)
    (repo / "scripts").mkdir()
    task = {
        "id": "task_z",
        "target_script_path": "scripts/evaluate_z.py",
        "run_dir": "data/run_z",
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "installed", outcome
    assert (repo / "scripts/evaluate_z.py").exists()


# ---------------------------------------------------------------------------
# Repair-flow overwrite tests (2026-05-25 round 3)
#
# After install_one started flipping noncompliant tasks to
# needs_compliance_repair and Hermes started actually rewriting the
# generated executor, install hit a third漏接 point: install_one's
# refuse-overwrite guard treated the repair's healthy new source as if it
# were a Hermes draft trying to clobber a hand-edited destination.
#
# The fix splits "refuse" into two cases:
#   - Normal flow (no compliance_failed_at history): keep refusing. The
#     destination is presumed hand-edited.
#   - Repair flow (task.compliance_failed_at present + new source already
#     passed validate_executor including the GateKeeper check): allow
#     overwrite. The destination is the known-bad noncompliant version we
#     explicitly asked Hermes to rewrite.
# ---------------------------------------------------------------------------


def test_normal_flow_hash_diff_still_refuses_overwrite(tmp_path, monkeypatch):
    """Plain hash-mismatch with no compliance history must still refuse
    overwrite. This protects hand-edited destinations from being clobbered
    by stale Hermes drafts."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data/run_a"
    run_dir.mkdir(parents=True)
    src = run_dir / "generated_executor" / "executor.py"
    _write_executor(src, with_gatekeeper=True)
    (repo / "scripts").mkdir()
    # Pre-existing hand-edited target.
    hand_edited = (repo / "scripts/evaluate_a.py")
    hand_edited.write_text(
        f"# hand-edited content\n{GATEKEEPER_IMPORT}\n"
        "def main():\n    return 1\n"
        "def declare_data_requirements(*a, **k):\n    return {}\n",
        encoding="utf-8",
    )
    task = {
        "id": "task_a_executor_code",
        "target_script_path": "scripts/evaluate_a.py",
        "run_dir": "data/run_a",
        # NOTE: no compliance_failed_at — this is a plain (non-repair) flow.
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "skipped"
    assert "refusing to overwrite" in outcome["reason"]
    # Hand-edited file untouched.
    after = hand_edited.read_text(encoding="utf-8")
    assert "hand-edited content" in after


def test_repair_flow_hash_diff_overwrites_when_source_compliant(tmp_path, monkeypatch):
    """Repair flow: task carries compliance_failed_at AND new source has
    GateKeeper import → install must overwrite the known-bad target."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data/run_b"
    run_dir.mkdir(parents=True)
    # The new repaired source has GateKeeper.
    src = run_dir / "generated_executor" / "executor.py"
    _write_executor(src, with_gatekeeper=True)
    (repo / "scripts").mkdir()
    # The old destination does NOT have GateKeeper — it's the noncompliant
    # version we are repairing.
    old_target = repo / "scripts/evaluate_b.py"
    old_target.write_text(
        "# noncompliant old version\n"
        "def main():\n    return 0\n"
        "def declare_data_requirements(*a, **k):\n    return {}\n",
        encoding="utf-8",
    )
    task = {
        "id": "task_b_executor_code",
        "target_script_path": "scripts/evaluate_b.py",
        "run_dir": "data/run_b",
        "compliance_failed_at": "2026-05-24T18:10:05+00:00",
        "compliance_errors": ["compliance_failed: missing GateKeeper import"],
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "overwritten_after_compliance_repair", outcome
    assert outcome["overwrite_reason"] == "compliance_repair"
    assert "previous_target_sha256" in outcome
    # Destination now matches the repaired source — must contain GateKeeper.
    after = old_target.read_text(encoding="utf-8")
    assert GATEKEEPER_IMPORT in after
    assert "noncompliant old version" not in after


def test_repair_flow_noncompliant_source_still_blocked(tmp_path, monkeypatch):
    """Repair flow guard: even if task has compliance_failed_at, a source
    that is still missing GateKeeper must NOT overwrite. validate_executor
    runs before the overwrite branch, so the result is the same
    compliance_failed path with a repair_request marker."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data/run_c"
    run_dir.mkdir(parents=True)
    src = run_dir / "generated_executor" / "executor.py"
    # Hermes "tried" but the new source is STILL noncompliant.
    _write_executor(src, with_gatekeeper=False)
    (repo / "scripts").mkdir()
    (repo / "scripts/evaluate_c.py").write_text(
        "# previous noncompliant\n"
        "def main():\n    return 0\n"
        "def declare_data_requirements(*a, **k):\n    return {}\n",
        encoding="utf-8",
    )
    task = {
        "id": "task_c_executor_code",
        "target_script_path": "scripts/evaluate_c.py",
        "run_dir": "data/run_c",
        "compliance_failed_at": "2026-05-24T18:10:05+00:00",
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "compliance_failed", (
        f"noncompliant repair source must NOT overwrite via the repair branch; "
        f"got {outcome}"
    )
    # Target untouched.
    after = (repo / "scripts/evaluate_c.py").read_text(encoding="utf-8")
    assert "previous noncompliant" in after


def test_main_clears_stale_compliance_fields_after_repair_overwrite(tmp_path, monkeypatch):
    """After a successful overwrite_after_compliance_repair, main() must:
      - set installed_at + repair_installed_at + installed_sha256
      - move compliance_failed_at → previous_compliance_failed_at
      - move compliance_errors → previous_compliance_errors
      - set last_compliance_status: passed
    so the task is no longer presented as currently noncompliant."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    handoffs = repo / "data/research_framework/hermes_executor_handoffs.yaml"
    handoffs.parent.mkdir(parents=True)
    run_dir = repo / "data/run_d"
    run_dir.mkdir(parents=True)
    src = run_dir / "generated_executor" / "executor.py"
    _write_executor(src, with_gatekeeper=True)
    (repo / "scripts").mkdir()
    (repo / "scripts/evaluate_d.py").write_text("# noncompliant", encoding="utf-8")
    handoffs.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "tasks": [{
                "id": "task_d_executor_code",
                "status": "completed",
                "target_script_path": "scripts/evaluate_d.py",
                "run_dir": "data/run_d",
                "compliance_failed_at": "2026-05-24T18:10:05+00:00",
                "compliance_errors": ["compliance_failed: missing GateKeeper import"],
            }],
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(install_mod, "HANDOFFS_PATH", handoffs)
    # Avoid hitting the real registry / data inventory for this main() run.
    (repo / "data/research_framework/executor_registry.yaml").write_text(
        yaml.safe_dump({"executors": {}}, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(install_mod, "REGISTRY_PATH",
                          repo / "data/research_framework/executor_registry.yaml")
    import sys
    sys.argv = ["install_generated_executors.py"]
    rc = install_mod.main()
    assert rc == 0
    doc = yaml.safe_load(handoffs.read_text(encoding="utf-8"))
    task = doc["tasks"][0]
    assert task["install_action"] == "overwritten_after_compliance_repair"
    assert task["last_compliance_status"] == "passed"
    assert task.get("installed_at"), task
    assert task.get("repair_installed_at") == task.get("installed_at")
    assert task.get("installed_sha256")
    # Stale current-state fields moved into history.
    assert "compliance_failed_at" not in task
    assert "compliance_errors" not in task
    assert task.get("previous_compliance_failed_at") == "2026-05-24T18:10:05+00:00"
    assert isinstance(task.get("previous_compliance_errors"), list)


# ---------------------------------------------------------------------------
# Executor regeneration tests (2026-05-25 round 4)
#
# When a previously-installed task loses its generated_executor source on
# disk (e.g., the run dir's generated_executor/ directory was cleaned up
# manually), install_one used to fall into the generic
# "skipped: no generated_executor source found" branch. That left the task
# perpetually pending, blocking new ideation. The needs_executor_regeneration
# branch routes the task back to the Hermes handoff layer so Hermes can
# regenerate from spec + executor_tool_request, without anyone hand-writing
# the source.
#
# Constraints on this branch:
#   - Trigger ONLY when there is real evidence of prior install
#     (installed_at present AND target still exists in scripts/). A brand-
#     new task that simply has no generated_executor source yet must
#     continue to follow the original "skipped: no source" path; flipping
#     it to needs_executor_regeneration would break the normal new-task
#     flow.
# ---------------------------------------------------------------------------


def test_no_source_for_new_task_still_returns_plain_skipped(tmp_path, monkeypatch):
    """A new task that simply has not been installed yet AND has no
    generated_executor source must keep the old skipped behaviour, not
    silently get re-routed into the regeneration flow."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data/run_new"
    run_dir.mkdir(parents=True)
    # No generated_executor/ at all.
    (repo / "scripts").mkdir()
    task = {
        "id": "task_new",
        "target_script_path": "scripts/evaluate_new.py",
        "run_dir": "data/run_new",
        # No installed_at — this task has never been through install.
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "skipped"
    assert "no generated_executor source" in outcome["reason"]


def test_no_source_for_previously_installed_task_triggers_regeneration(tmp_path, monkeypatch):
    """The reverse_bond_floor case: task was installed, target file
    exists in scripts/, but the generated_executor source was deleted.
    Must return action="needs_executor_regeneration" and write a
    regeneration_request marker."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    run_dir = repo / "data/run_lost"
    run_dir.mkdir(parents=True)
    # No generated_executor/.
    (repo / "scripts").mkdir()
    # Target still exists in scripts/ from the previous install.
    target = repo / "scripts/evaluate_lost.py"
    target.write_text(
        f"{GATEKEEPER_IMPORT}\n"
        "def main():\n    return 0\n"
        "def declare_data_requirements(*a, **k):\n    return {}\n",
        encoding="utf-8",
    )
    task = {
        "id": "task_lost",
        "target_script_path": "scripts/evaluate_lost.py",
        "run_dir": "data/run_lost",
        "installed_at": "2026-05-24T07:35:01+00:00",
        "installed_source": "data/run_lost/generated_executor/evaluate_lost.py",
        "installed_sha256": "old_hash_16",
    }
    outcome = install_mod.install_one(task, dry_run=False)
    assert outcome["action"] == "needs_executor_regeneration", outcome
    assert outcome["installed_at"] == "2026-05-24T07:35:01+00:00"
    marker = (run_dir / "generated_executor" / "executor_regeneration_request.yaml")
    assert marker.exists()
    payload = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert payload["handoff_id"] == "task_lost"
    assert payload["previous_installed_at"] == "2026-05-24T07:35:01+00:00"
    assert "Regenerate" in payload["required_action"]
    assert "GateKeeper" in payload["required_action"]


def test_main_flips_task_to_needs_executor_regeneration(tmp_path, monkeypatch):
    """Main() must flip the task's status to needs_executor_regeneration
    and move the installed_* fields into previous_* history so the next
    install of the regenerated source can land cleanly."""
    repo = tmp_path
    monkeypatch.setattr(install_mod, "REPO_ROOT", repo)
    handoffs = repo / "data/research_framework/hermes_executor_handoffs.yaml"
    handoffs.parent.mkdir(parents=True)
    run_dir = repo / "data/run_lost2"
    run_dir.mkdir(parents=True)
    (repo / "scripts").mkdir()
    target = repo / "scripts/evaluate_lost2.py"
    target.write_text(f"{GATEKEEPER_IMPORT}\n", encoding="utf-8")
    handoffs.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "tasks": [{
                "id": "task_lost2",
                "status": "completed",
                "target_script_path": "scripts/evaluate_lost2.py",
                "run_dir": "data/run_lost2",
                "installed_at": "2026-05-24T07:35:01+00:00",
                "installed_source": "data/run_lost2/generated_executor/evaluate_lost2.py",
                "installed_sha256": "lost_hash",
            }],
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(install_mod, "HANDOFFS_PATH", handoffs)
    (repo / "data/research_framework/executor_registry.yaml").write_text(
        yaml.safe_dump({"executors": {}}, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(install_mod, "REGISTRY_PATH",
                          repo / "data/research_framework/executor_registry.yaml")
    import sys
    sys.argv = ["install_generated_executors.py"]
    rc = install_mod.main()
    assert rc == 0
    doc = yaml.safe_load(handoffs.read_text(encoding="utf-8"))
    task = doc["tasks"][0]
    assert task["status"] == "needs_executor_regeneration"
    assert task["source_lost_detected_at"]
    assert task["regeneration_request"]
    # Audit history preserved.
    assert task["previous_installed_at"] == "2026-05-24T07:35:01+00:00"
    assert task["previous_installed_sha256"] == "lost_hash"
    # Stale current-state pointers cleared so the next install of the
    # regenerated source sees a clean slate.
    assert "installed_at" not in task
    assert "installed_sha256" not in task
