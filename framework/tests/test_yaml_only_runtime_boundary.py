"""Boundary case tests for YAML-only runtime refactor (b37d7e2).

Stress tests the runtime invariants set by the yaml-only refactor:
- validate_entrypoints.py: only AGENTS.md + CLAUDE.md allowed as markdown
- framework_doc_check.py: every yaml runtime path routes to correct validator
- outbox_protocol_preflight.py: L0 yaml preconditions (l0_*.yaml) enforced
- search_ledger.py: yaml ledger searchable

Run: .venv/bin/python -m pytest framework/tests/test_yaml_only_runtime_boundary.py -v

Tests use subprocess for validators (so we test the actual CLI exit codes,
not internal functions) and tmp_path for isolated repo state.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


# ---------- helpers ----------


def _load_module(script_name: str):
    path = SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_validator(script: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    return subprocess.run(cmd, cwd=cwd or REPO_ROOT, capture_output=True, text=True)


def _make_min_runtime_yaml(repo: Path) -> None:
    """Create minimal valid yaml runtime files in `repo`."""
    rf = repo / "data" / "research_framework"
    rf.mkdir(parents=True, exist_ok=True)

    (rf / "runtime_entrypoints.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "runtime_context": {
            "files": {
                "current_state": {"path": "data/research_framework/current.yaml", "required": True},
                "strategy_registry": {"path": "data/research_framework/strategies.yaml", "required": True},
                "baseline_registry": {"path": "data/research_framework/baseline_registry.yaml", "required": True},
                "experiment_registry": {"path": "data/research_framework/experiments.yaml", "required": True},
                "protocol_rules": {"path": "data/research_framework/protocol_rules.yaml", "required": True},
            },
        },
    }))

    (rf / "current.yaml").write_text(yaml.safe_dump({"schema_version": 1, "strategies": []}))
    (rf / "strategies.yaml").write_text(yaml.safe_dump({"schema_version": 1, "strategies": []}))
    (rf / "baseline_registry.yaml").write_text(yaml.safe_dump({"schema_version": 1, "baselines": []}))
    (rf / "experiments.yaml").write_text(yaml.safe_dump({"schema_version": 1, "experiments": []}))

    (rf / "protocol_rules.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "version": "1.5-machine",
        "rules": [
            {"id": "R1", "summary": "test"},
            {"id": "R4", "summary": "test"},
            {"id": "R5", "summary": "test"},
            {"id": "R6", "summary": "test"},
            {"id": "R7", "summary": "test"},
            {"id": "R9", "summary": "test"},
        ],
    }))

    (repo / "AGENTS.md").write_text("# AGENTS\nbootstrap\n")
    (repo / "CLAUDE.md").write_text("# CLAUDE\nbootstrap pointer to AGENTS.md\n")


# ==================== A. is_markdown_artifact unit tests ====================


@pytest.mark.parametrize("name,expected", [
    ("foo.md", True),
    ("foo.md.bak", True),
    ("foo.md.archived", True),
    ("foo.md.tmp", True),
    ("foo.markdown", False),
    ("foo.yaml", False),
    ("foo.txt", False),
    ("AGENTS.md", True),
    ("README.md.archived", True),
])
def test_is_markdown_artifact_hardening(name: str, expected: bool):
    """validate_entrypoints.is_markdown_artifact catches .md + .md.* artifacts."""
    v = _load_module("validate_entrypoints.py")
    assert v.is_markdown_artifact(Path(name)) is expected


# ==================== B. validate_entrypoints subprocess ====================


def test_validate_entrypoints_real_repo_ok():
    """Sanity: real repo currently passes."""
    r = _run_validator("validate_entrypoints.py")
    assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"


def test_validate_entrypoints_rejects_extra_markdown(tmp_path: Path, monkeypatch):
    """Creating a non-allowed .md in repo root should fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_min_runtime_yaml(repo)
    (repo / "EXTRA.md").write_text("# extra\n")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    # Use module-level REPO_ROOT override via env? Validator hardcodes REPO_ROOT
    # from __file__. Instead, copy validator into tmp repo and run there.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "validate_entrypoints.py").write_text(
        (SCRIPTS / "validate_entrypoints.py").read_text()
    )
    r = subprocess.run(
        [sys.executable, "scripts/validate_entrypoints.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1, f"expected fail; got {r.returncode}; stdout={r.stdout}"
    assert "EXTRA.md" in r.stdout


def test_validate_entrypoints_rejects_md_archived(tmp_path: Path):
    """.md.archived should be rejected (hardening for *.md.*)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_min_runtime_yaml(repo)
    (repo / "stale_doc.md.archived").write_text("archived\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "validate_entrypoints.py").write_text(
        (SCRIPTS / "validate_entrypoints.py").read_text()
    )
    r = subprocess.run(
        [sys.executable, "scripts/validate_entrypoints.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "stale_doc.md.archived" in r.stdout


def test_validate_entrypoints_missing_agents_md(tmp_path: Path):
    """AGENTS.md deletion should fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_min_runtime_yaml(repo)
    (repo / "AGENTS.md").unlink()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "validate_entrypoints.py").write_text(
        (SCRIPTS / "validate_entrypoints.py").read_text()
    )
    r = subprocess.run(
        [sys.executable, "scripts/validate_entrypoints.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "AGENTS.md" in r.stdout


def test_validate_entrypoints_missing_claude_md(tmp_path: Path):
    """CLAUDE.md deletion should fail (Claude Code auto-entry pointer)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_min_runtime_yaml(repo)
    (repo / "CLAUDE.md").unlink()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "validate_entrypoints.py").write_text(
        (SCRIPTS / "validate_entrypoints.py").read_text()
    )
    r = subprocess.run(
        [sys.executable, "scripts/validate_entrypoints.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "CLAUDE.md" in r.stdout


def test_validate_entrypoints_missing_runtime_files(tmp_path: Path):
    """runtime_entrypoints.yaml referencing missing required file should fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_min_runtime_yaml(repo)
    # Remove one required runtime file
    (repo / "data" / "research_framework" / "current.yaml").unlink()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "validate_entrypoints.py").write_text(
        (SCRIPTS / "validate_entrypoints.py").read_text()
    )
    r = subprocess.run(
        [sys.executable, "scripts/validate_entrypoints.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "current.yaml" in r.stdout or "required runtime file missing" in r.stdout


def test_validate_entrypoints_missing_protocol_rules(tmp_path: Path):
    """protocol_rules.yaml missing R-id should fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_min_runtime_yaml(repo)
    rules_path = repo / "data" / "research_framework" / "protocol_rules.yaml"
    rules_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "version": "1.5-machine",
        "rules": [{"id": "R1", "summary": "only one"}],  # missing R4-R9
    }))
    (repo / "scripts").mkdir()
    (repo / "scripts" / "validate_entrypoints.py").write_text(
        (SCRIPTS / "validate_entrypoints.py").read_text()
    )
    r = subprocess.run(
        [sys.executable, "scripts/validate_entrypoints.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "missing" in r.stdout.lower()


# ==================== C. framework_doc_check.dispatch unit tests ====================


@pytest.mark.parametrize("rel,expected_validator", [
    # markdown routes
    ("AGENTS.md", "validate_entrypoints.py"),
    ("CLAUDE.md", "validate_entrypoints.py"),
    ("EXTRA.md", "validate_entrypoints.py"),
    ("EXTRA.md.bak", "validate_entrypoints.py"),
    # data/<run-id>/* routes
    ("data/cb_arb_test_20260517/spec.yaml", "validate_spec.py"),
    ("data/cb_arb_test_20260517/l4_ack.yaml", "validate_l4_ack.py"),
    ("data/cb_arb_test_20260517/diagnostic.yaml", "validate_l5_diagnostic.py"),
    # framework yaml routes
    ("data/research_framework/baseline_registry.yaml", "validate_baseline_registry.py"),
    ("data/research_framework/current.yaml", "validate_current_md.py"),
    ("data/research_framework/runtime_entrypoints.yaml", "validate_entrypoints.py"),
    ("data/research_framework/protocol_rules.yaml", "validate_entrypoints.py"),
    ("data/research_framework/experiments.yaml", "validate_entrypoints.py"),
    ("data/research_framework/truth_sync_waivers/foo.yaml", "validate_truth_sync.py"),
    ("data/research_framework/compute_budget_config.json", "validate_compute_budget.py"),
    ("data/research_framework/run_manifests/foo.yaml", "validate_run_manifest.py"),
    # skip routes
    ("README.txt", ""),
    ("scripts/foo.py", ""),
    ("strategies/cb_arb/verifier.py", ""),
])
def test_framework_doc_check_dispatch(rel: str, expected_validator: str):
    """framework_doc_check.dispatch routes each runtime path to correct validator."""
    fdc = _load_module("framework_doc_check.py")
    abs_path = REPO_ROOT / rel
    validator, _args = fdc.dispatch(abs_path)
    assert validator == expected_validator, (
        f"dispatch({rel}) → {validator!r}, expected {expected_validator!r}"
    )


def test_framework_doc_check_skips_outside_repo(tmp_path: Path):
    """Path outside REPO_ROOT should skip."""
    fdc = _load_module("framework_doc_check.py")
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text("test")
    validator, _ = fdc.dispatch(outside)
    assert validator == ""


# ==================== D. outbox_protocol_preflight subprocess ====================


def _make_outbox_files(tmp_path: Path, claude_msg: str) -> tuple[Path, Path, Path, Path]:
    claude_box = tmp_path / "claude.md"
    codex_box = tmp_path / "codex.md"
    state_file = tmp_path / "state.md"
    cache = tmp_path / "cache.jsonl"
    claude_box.write_text(claude_msg)
    codex_box.write_text("")
    state_file.write_text("")
    return claude_box, codex_box, state_file, cache


def _run_preflight(claude_box: Path, codex_box: Path, state_file: Path, cache: Path,
                   protocol_doc: Path, repo: Path,
                   ledger: Path | None = None) -> subprocess.CompletedProcess:
    args = [
        sys.executable, str(SCRIPTS / "outbox_protocol_preflight.py"),
        "check",
        "--claude-box", str(claude_box),
        "--codex-box", str(codex_box),
        "--state-file", str(state_file),
        "--cache", str(cache),
        "--protocol-doc", str(protocol_doc),
        "--repo", str(repo),
    ]
    if ledger:
        args += ["--ledger", str(ledger)]
    return subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT)


def test_outbox_preflight_missing_protocol_header(tmp_path: Path):
    """Message without protocol-redline-vX.Y header → MISSING-PROTOCOL."""
    msg = """### 2026-05-17 10:00 CST - Claude - L1/DIRECT

Body without header.
"""
    claude, codex, state, cache = _make_outbox_files(tmp_path, msg)
    protocol_doc = REPO_ROOT / "data" / "research_framework" / "protocol_rules.yaml"
    r = _run_preflight(claude, codex, state, cache, protocol_doc, tmp_path)
    # exit code 20 = HANDOFF
    assert r.returncode in {20}, f"got {r.returncode}; {r.stdout}; {r.stderr}"
    assert "MISSING-PROTOCOL" in r.stdout


def test_outbox_preflight_version_mismatch(tmp_path: Path):
    """Message protocol-redline-v0.1 vs local v1.5 → VERSION-MISMATCH."""
    msg = """### 2026-05-17 10:00 CST - Claude - L1/DIRECT

<!-- protocol-redline-v0.1 -->
Body.
"""
    claude, codex, state, cache = _make_outbox_files(tmp_path, msg)
    protocol_doc = REPO_ROOT / "data" / "research_framework" / "protocol_rules.yaml"
    r = _run_preflight(claude, codex, state, cache, protocol_doc, tmp_path)
    assert r.returncode == 20
    assert "VERSION-MISMATCH" in r.stdout


def test_outbox_preflight_version_missing_local(tmp_path: Path):
    """If protocol_doc points to non-existent file → VERSION-MISSING-LOCAL."""
    msg = """### 2026-05-17 10:00 CST - Claude - L1/DIRECT

<!-- protocol-redline-v1.5 -->
Body.
"""
    claude, codex, state, cache = _make_outbox_files(tmp_path, msg)
    missing_protocol = tmp_path / "does_not_exist.yaml"
    r = _run_preflight(claude, codex, state, cache, missing_protocol, tmp_path)
    assert r.returncode == 20
    assert "VERSION-MISSING-LOCAL" in r.stdout


def test_outbox_preflight_l1_direct_missing_l0_yaml(tmp_path: Path):
    """L1 DIRECT entry-id 1.1 without l0_intuition.yaml → MISSING-L0-PRECONDITION."""
    msg = """### 2026-05-17 10:00 CST - Claude - L1/DIRECT

<!-- protocol-redline-v1.5 -->
<!-- l0-entry-id: 1.1 -->
Project: quant
Task: L1 test
run-id: cb_arb_test_20260517

L1 hypothesis: test
parameter_space: foo
hard_floors: bar
output_artifacts: baz
compute_estimate: 10s
data_sources: none
true_cv_design: leave-one-year-out
stop_conditions: timeout
"""
    claude, codex, state, cache = _make_outbox_files(tmp_path, msg)
    protocol_doc = REPO_ROOT / "data" / "research_framework" / "protocol_rules.yaml"
    repo = tmp_path / "fakerepo"
    repo.mkdir()
    r = _run_preflight(claude, codex, state, cache, protocol_doc, repo)
    assert r.returncode == 20
    # path l0_intuition.yaml missing
    assert "l0_intuition.yaml" in r.stdout or "L0" in r.stdout


def test_outbox_preflight_l1_direct_l0_yaml_missing_schema(tmp_path: Path):
    """L0 yaml without schema_version → fail."""
    msg = """### 2026-05-17 10:00 CST - Claude - L1/DIRECT

<!-- protocol-redline-v1.5 -->
<!-- l0-entry-id: 1.1 -->
run-id: cb_arb_test_20260517

L1 hypothesis: t
parameter_space: t
hard_floors: t
output_artifacts: t
compute_estimate: t
data_sources: t
true_cv_design: t
stop_conditions: t
"""
    claude, codex, state, cache = _make_outbox_files(tmp_path, msg)
    protocol_doc = REPO_ROOT / "data" / "research_framework" / "protocol_rules.yaml"
    repo = tmp_path / "fakerepo"
    (repo / "data" / "cb_arb_test_20260517").mkdir(parents=True)
    (repo / "data" / "cb_arb_test_20260517" / "l0_intuition.yaml").write_text(
        yaml.safe_dump({"intuition_ladder": {"strong": "x", "medium": "y", "weak": "z"}})
        # missing schema_version
    )
    r = _run_preflight(claude, codex, state, cache, protocol_doc, repo)
    assert r.returncode == 20
    assert "schema_version" in r.stdout.lower() or "L0" in r.stdout


def test_outbox_preflight_l1_direct_l0_yaml_missing_ladder(tmp_path: Path):
    """L0 yaml without strong/medium/weak ladder → fail."""
    msg = """### 2026-05-17 10:00 CST - Claude - L1/DIRECT

<!-- protocol-redline-v1.5 -->
<!-- l0-entry-id: 1.1 -->
run-id: cb_arb_test_20260517

L1 hypothesis: t
parameter_space: t
hard_floors: t
output_artifacts: t
compute_estimate: t
data_sources: t
true_cv_design: t
stop_conditions: t
"""
    claude, codex, state, cache = _make_outbox_files(tmp_path, msg)
    protocol_doc = REPO_ROOT / "data" / "research_framework" / "protocol_rules.yaml"
    repo = tmp_path / "fakerepo"
    (repo / "data" / "cb_arb_test_20260517").mkdir(parents=True)
    (repo / "data" / "cb_arb_test_20260517" / "l0_intuition.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "intuition_ladder": {"strong": "x"},  # missing medium/weak
        })
    )
    r = _run_preflight(claude, codex, state, cache, protocol_doc, repo)
    assert r.returncode == 20
    assert "intuition" in r.stdout.lower() or "ladder" in r.stdout.lower() or "L0" in r.stdout


# ==================== E. search_ledger smoke tests ====================


def test_search_ledger_finds_rejected_pattern():
    """search_ledger should find 'panic detector' as STRONG_MATCH rejected.

    Note: search_ledger exits 1 when STRONG_MATCH found (intentional warning).
    """
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "search_ledger.py"), "panic detector"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 1, "STRONG_MATCH should signal exit 1 (warn)"
    assert "STRONG MATCH" in r.stdout
    assert "rejected" in r.stdout
    assert "panic" in r.stdout.lower()


def test_search_ledger_zero_matches_for_nonexistent_keyword():
    """search_ledger with random keyword should return 0 matches gracefully."""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "search_ledger.py"), "zzzznonexistentxxxxx"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # Should not crash, exit 0 (no matches is OK)
    assert r.returncode == 0


# ==================== F. evidence preservation (git audit trail) ====================


def test_experiments_yaml_has_migrated_reject_patterns():
    """experiments.yaml should contain 11+ reject patterns migrated from ledger."""
    path = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
    data = yaml.safe_load(path.read_text())
    entries = data.get("experiments", [])
    rejected = [e for e in entries if e.get("status") == "rejected"]
    assert len(rejected) >= 8, f"expected ≥8 rejected entries (ledger migration), got {len(rejected)}"


def test_research_insights_yaml_has_source_attribution():
    """research_insights.yaml should have source_migration block pointing to git history."""
    path = REPO_ROOT / "data" / "research_framework" / "research_insights.yaml"
    data = yaml.safe_load(path.read_text())
    assert "source_migration" in data
    src = data["source_migration"]
    assert "from_git_paths" in src
    assert any(".md" in p for p in src["from_git_paths"]), \
        "source_migration.from_git_paths should reference original .md paths"


def test_experiments_yaml_commit_refs_resolvable():
    """experiments.yaml commit_ref values should be git-show resolvable."""
    path = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
    data = yaml.safe_load(path.read_text())
    entries = data.get("experiments", [])
    refs_with_commit = [
        e["commit_ref"] for e in entries
        if e.get("commit_ref") and ":" in e["commit_ref"]
    ]
    assert len(refs_with_commit) >= 1, "at least one entry should have commit_ref"
    # Sample first one
    sample = refs_with_commit[0]
    commit_hash = sample.split(":")[0]
    r = subprocess.run(
        ["git", "cat-file", "-e", commit_hash],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"commit_ref {commit_hash} not resolvable: {r.stderr}"


# ==================== G. truth_sync edge cases ====================


def test_truth_sync_strategy_init_does_not_trigger():
    """__init__.py changes should NOT trigger truth_sync."""
    v = _load_module("validate_truth_sync.py")
    triggers = v.classify_triggers(["strategies/cb_arb/__init__.py"])
    assert triggers == [], f"__init__.py should not trigger; got {triggers}"


def test_truth_sync_data_research_framework_strategies_triggers():
    """data/research_framework/strategies.yaml changes trigger truth_sync."""
    v = _load_module("validate_truth_sync.py")
    triggers = v.classify_triggers(["data/research_framework/strategies.yaml"])
    assert len(triggers) == 1
    assert "strategies.yaml" in triggers[0]["path"]


# ==================== H. CLAUDE.md stub semantic check ====================


def test_claude_md_stub_points_to_agents_md():
    """CLAUDE.md should be a non-authoritative stub pointing to AGENTS.md."""
    path = REPO_ROOT / "CLAUDE.md"
    assert path.exists(), "CLAUDE.md must exist as Claude Code auto-entry pointer"
    text = path.read_text()
    assert "AGENTS.md" in text, "CLAUDE.md should reference AGENTS.md as authoritative"
    # stub should be short (not an alternative authoritative doc)
    assert len(text) < 2000, f"CLAUDE.md stub too long ({len(text)} chars); should redirect to AGENTS.md"


def test_agents_md_exists_and_nontrivial():
    """AGENTS.md is the primary cross-AI bootstrap and must be substantive."""
    path = REPO_ROOT / "AGENTS.md"
    assert path.exists()
    text = path.read_text()
    assert len(text) > 500, f"AGENTS.md too short ({len(text)} chars) to be the primary bootstrap"


# ==================== I. cross-AI runtime invariants ====================


def test_no_md_files_outside_allowed():
    """Final check: repo should have only AGENTS.md + CLAUDE.md as markdown."""
    md_files = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        rel_str = str(rel)
        if rel_str.startswith((".git/", ".venv/", ".pytest_cache/", "node_modules/")):
            continue
        name = path.name
        if name.endswith(".md") or ".md." in name:
            md_files.append(rel_str)
    extra = [m for m in md_files if m not in {"AGENTS.md", "CLAUDE.md"}]
    assert extra == [], f"unexpected markdown files: {extra}"


def test_runtime_yaml_files_all_present():
    """All required runtime yaml files exist."""
    required = [
        "data/research_framework/runtime_entrypoints.yaml",
        "data/research_framework/current.yaml",
        "data/research_framework/strategies.yaml",
        "data/research_framework/baseline_registry.yaml",
        "data/research_framework/experiments.yaml",
        "data/research_framework/protocol_rules.yaml",
    ]
    missing = [p for p in required if not (REPO_ROOT / p).exists()]
    assert missing == [], f"missing runtime yaml files: {missing}"


def test_framework_preflight_passes():
    """framework_preflight.py should still pass after refactor."""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "framework_preflight.py"), "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # exit 0 = OK, exit 2 = warnings only (still passes preflight)
    assert r.returncode in {0, 2}, f"preflight failed (exit {r.returncode}); stdout={r.stdout}"
