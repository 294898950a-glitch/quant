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
