"""Auto-update behavior tests for YAML-only runtime.

Verifies which yaml runtime files can / cannot be machine-auto-updated:

- experiments.yaml: auto-updated by auto_research_pipeline.update_experiments
- run_manifests/*.yaml: auto-written by auto_research_pipeline
- l4_ack.yaml computed_data: auto-filled by auto_compute_l4_data.py
- current.yaml: NOT auto-updated (by design; AI/user editorial)
- baseline_registry.yaml: NOT auto-updated (by design; user/AI promotion)
- research_insights.yaml: NOT auto-updated (by design)

Tests both positive (auto-update works + idempotent) and negative
(not-auto-updated paths stay manual).

Run: .venv/bin/python -m pytest framework/tests/test_yaml_only_auto_update.py -v
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load_module(script_name: str):
    path = SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(
        script_name.replace(".py", "").replace("-", "_"), path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ==================== A. experiments.yaml auto-update ====================


def test_update_experiments_appends_new_entry(tmp_path: Path, monkeypatch):
    """Empty experiments.yaml → after update, new entry appended."""
    arp = _load_module("auto_research_pipeline.py")
    exp_path = tmp_path / "experiments.yaml"
    exp_path.write_text(yaml.safe_dump({"schema_version": 1, "experiments": []}))
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    spec = {"run_id": "cb_arb_test_20260517", "strategy_id": "cb_arb",
            "hypothesis_id": "hyp1", "hypothesis": "test hypothesis"}
    output_dir = tmp_path / "data" / "cb_arb_test_20260517"
    output_dir.mkdir(parents=True)
    manifest_path = tmp_path / "data" / "research_framework" / "run_manifests" / "test.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("placeholder")
    verdict = {"status": "wip", "decision": "incomplete", "summary": {}}
    compute = {"decision": "record-only", "estimated_compute_cost_yuan": 5.0}

    arp.update_experiments(spec, output_dir, manifest_path, verdict, compute, dry_run=False)

    data = yaml.safe_load(exp_path.read_text())
    assert len(data["experiments"]) == 1
    entry = data["experiments"][0]
    assert entry["id"] == "cb_arb_test_20260517"
    assert entry["strategy_id"] == "cb_arb"
    assert entry["status"] == "wip"
    assert "automation" in entry
    assert "updated_at" in data


def test_update_experiments_idempotent_on_same_run_id(tmp_path: Path, monkeypatch):
    """Calling update_experiments twice with same run_id → updates in-place, no duplicate."""
    arp = _load_module("auto_research_pipeline.py")
    exp_path = tmp_path / "experiments.yaml"
    exp_path.write_text(yaml.safe_dump({"schema_version": 1, "experiments": []}))
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    spec = {"run_id": "cb_arb_idem_20260517", "strategy_id": "cb_arb",
            "hypothesis_id": "hyp1", "hypothesis": "first call"}
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text("x")
    verdict = {"status": "wip", "decision": "incomplete", "summary": {}}
    compute = {"decision": "record-only"}

    arp.update_experiments(spec, output_dir, manifest_path, verdict, compute, dry_run=False)
    # Update with new verdict
    verdict2 = {"status": "rejected", "decision": "fail", "summary": {"task_count": 5}}
    spec2 = {**spec, "hypothesis": "second call"}
    arp.update_experiments(spec2, output_dir, manifest_path, verdict2, compute, dry_run=False)

    data = yaml.safe_load(exp_path.read_text())
    assert len(data["experiments"]) == 1, "should update in-place, not duplicate"
    assert data["experiments"][0]["status"] == "rejected"
    assert data["experiments"][0]["summary"].startswith("second call")


def test_update_experiments_multiple_run_ids_coexist(tmp_path: Path, monkeypatch):
    """Multiple distinct run_ids → all coexist."""
    arp = _load_module("auto_research_pipeline.py")
    exp_path = tmp_path / "experiments.yaml"
    exp_path.write_text(yaml.safe_dump({"schema_version": 1, "experiments": []}))
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    for i in range(3):
        spec = {"run_id": f"cb_arb_multi_{i}_20260517", "strategy_id": "cb_arb",
                "hypothesis_id": f"h{i}", "hypothesis": f"call {i}"}
        verdict = {"status": "wip", "decision": "ok", "summary": {}}
        compute = {"decision": "record-only"}
        out = tmp_path / f"out_{i}"
        out.mkdir()
        m = tmp_path / f"m_{i}.yaml"
        m.write_text("x")
        arp.update_experiments(spec, out, m, verdict, compute, dry_run=False)

    data = yaml.safe_load(exp_path.read_text())
    ids = [e["id"] for e in data["experiments"]]
    assert len(ids) == 3
    assert set(ids) == {f"cb_arb_multi_{i}_20260517" for i in range(3)}


def test_update_experiments_dry_run_does_not_write(tmp_path: Path, monkeypatch):
    """dry_run=True should not modify experiments.yaml."""
    arp = _load_module("auto_research_pipeline.py")
    exp_path = tmp_path / "experiments.yaml"
    initial = {"schema_version": 1, "experiments": []}
    exp_path.write_text(yaml.safe_dump(initial))
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    spec = {"run_id": "dry", "strategy_id": "cb_arb", "hypothesis_id": "h", "hypothesis": "x"}
    arp.update_experiments(spec, tmp_path, tmp_path / "m.yaml",
                           {"status": "wip", "decision": "ok", "summary": {}},
                           {"decision": "record-only"}, dry_run=True)

    data = yaml.safe_load(exp_path.read_text())
    assert data == initial


# ==================== B. l4_ack.yaml auto-fill ====================


def test_auto_compute_l4_fills_computed_data(tmp_path: Path, monkeypatch):
    """Mock a data/<run-id>/ with spec.yaml status=RUNNING + minimal csvs.
    auto_compute_l4_data.process_run should fill l4_ack.yaml computed_data."""
    acd = _load_module("auto_compute_l4_data.py")
    monkeypatch.setattr(acd, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(acd, "REPO_ROOT", tmp_path)

    run_dir = tmp_path / "data" / "cb_arb_l4test_20260517"
    run_dir.mkdir(parents=True)
    # minimal spec
    (run_dir / "spec.yaml").write_text(yaml.safe_dump({
        "run_id": "cb_arb_l4test_20260517",
        "status": "RUNNING",
        "hard_floors": {"cumulative_excess": 0.0},
    }))
    # minimal ranked.csv (1 candidate passing floor)
    (run_dir / "ranked.csv").write_text(
        "candidate,cumulative_excess\n"
        "test_candidate,0.5\n"
    )
    # minimal summary.csv (baseline + selected)
    (run_dir / "summary.csv").write_text(
        "candidate,cumulative_excess\n"
        "medium_baseline,0.2\n"
        "test_candidate,0.5\n"
    )
    # empty trades.csv
    (run_dir / "trades.csv").write_text("trade_id,candidate,exit_reason,pnl_amount\n")

    acd.process_run(run_dir, dry_run=False)
    ack = yaml.safe_load((run_dir / "l4_ack.yaml").read_text())
    assert "q1_floor_binding" in ack
    assert "computed_data" in ack["q1_floor_binding"]
    assert "auto_computed_at" in ack
    assert "q3_baseline_alignment" in ack


def test_auto_compute_l4_dry_run_skips_write(tmp_path: Path, monkeypatch):
    """dry_run=True should print but not write l4_ack.yaml."""
    acd = _load_module("auto_compute_l4_data.py")
    monkeypatch.setattr(acd, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(acd, "REPO_ROOT", tmp_path)

    run_dir = tmp_path / "data" / "cb_arb_dry_20260517"
    run_dir.mkdir(parents=True)
    (run_dir / "spec.yaml").write_text(yaml.safe_dump({
        "run_id": "cb_arb_dry_20260517", "status": "RUNNING", "hard_floors": {},
    }))
    (run_dir / "ranked.csv").write_text("candidate,cumulative_excess\nx,0.1\n")
    (run_dir / "summary.csv").write_text("candidate,cumulative_excess\nmedium_baseline,0.0\n")
    (run_dir / "trades.csv").write_text("trade_id,candidate,exit_reason,pnl_amount\n")

    acd.process_run(run_dir, dry_run=True)
    assert not (run_dir / "l4_ack.yaml").exists()


def test_auto_compute_l4_preserves_existing_answer(tmp_path: Path, monkeypatch):
    """Existing l4_ack.yaml answer/pass fields preserved; only computed_data updated."""
    acd = _load_module("auto_compute_l4_data.py")
    monkeypatch.setattr(acd, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(acd, "REPO_ROOT", tmp_path)

    run_dir = tmp_path / "data" / "cb_arb_preserve_20260517"
    run_dir.mkdir(parents=True)
    (run_dir / "spec.yaml").write_text(yaml.safe_dump({
        "run_id": "cb_arb_preserve_20260517", "status": "RUNNING", "hard_floors": {},
    }))
    (run_dir / "ranked.csv").write_text("candidate,cumulative_excess\nx,0.1\n")
    (run_dir / "summary.csv").write_text("candidate,cumulative_excess\nmedium_baseline,0.0\n")
    (run_dir / "trades.csv").write_text("trade_id,candidate,exit_reason,pnl_amount\n")
    # pre-existing l4_ack with answer
    (run_dir / "l4_ack.yaml").write_text(yaml.safe_dump({
        "q1_floor_binding": {"answer": "manual answer keep", "pass": True},
    }))

    acd.process_run(run_dir, dry_run=False)
    ack = yaml.safe_load((run_dir / "l4_ack.yaml").read_text())
    assert ack["q1_floor_binding"]["answer"] == "manual answer keep"
    assert ack["q1_floor_binding"]["pass"] is True
    assert "computed_data" in ack["q1_floor_binding"]


# ==================== D. NEGATIVE: not-auto-updated paths ====================


def test_current_yaml_not_auto_updated_by_pipeline(tmp_path: Path, monkeypatch):
    """auto_research_pipeline does NOT touch current.yaml.

    current.yaml represents adopted strategy truth and is editorial - AI/user
    decides when to flip baseline_row. This test enforces that contract.
    """
    arp = _load_module("auto_research_pipeline.py")
    # Verify auto_research_pipeline has no reference to writing current.yaml
    source = (SCRIPTS / "auto_research_pipeline.py").read_text()
    # writes to experiments.yaml are OK (positive case)
    # but writes to current.yaml should be absent
    assert "current.yaml" not in source or "write" not in source.split("current.yaml")[0].split("\n")[-1].lower(), \
        "auto_research_pipeline should not write current.yaml; current state is editorial"


def test_baseline_registry_not_auto_updated_by_pipeline():
    """auto_research_pipeline does NOT auto-promote baseline_registry.yaml.

    Baseline promotion requires explicit user/AI editorial (truth_sync waiver
    or baseline_registry edit). Pipeline only records experiment, not promotion.
    """
    source = (SCRIPTS / "auto_research_pipeline.py").read_text()
    # Ensure no write_yaml(BASELINE_REGISTRY, ...) or similar
    assert "BASELINE_REGISTRY" not in source or "write_yaml(BASELINE_REGISTRY" not in source
    # Ensure pipeline does not modify status=adopted
    assert "status: adopted" not in source.lower() or "promote" not in source.lower()


def test_research_insights_not_auto_updated_by_pipeline():
    """auto_research_pipeline does NOT write research_insights.yaml.

    Cross-batch insights are AI/user-distilled, not auto-derived from one run.
    """
    source = (SCRIPTS / "auto_research_pipeline.py").read_text()
    assert "research_insights.yaml" not in source


def test_truth_sync_flags_unmade_current_update():
    """If strategies/cb_arb/verifier.py changes but current.yaml not, truth_sync triggers."""
    v = _load_module("validate_truth_sync.py")
    triggers = v.classify_triggers(["strategies/cb_arb/verifier.py"])
    assert len(triggers) == 1
    assert triggers[0]["reason"] == "strategy_core_changed"


# ==================== E. Round-trip: auto-update + validator + search ====================


def test_auto_update_experiments_then_dispatch_route(tmp_path: Path, monkeypatch):
    """After update_experiments, framework_doc_check.dispatch routes correctly."""
    arp = _load_module("auto_research_pipeline.py")
    fdc = _load_module("framework_doc_check.py")

    exp_path = tmp_path / "experiments.yaml"
    exp_path.write_text(yaml.safe_dump({"schema_version": 1, "experiments": []}))
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    spec = {"run_id": "rt", "strategy_id": "cb_arb", "hypothesis_id": "h", "hypothesis": "rt"}
    arp.update_experiments(spec, tmp_path, tmp_path / "m.yaml",
                           {"status": "wip", "decision": "ok", "summary": {}},
                           {"decision": "record-only"}, dry_run=False)
    # Dispatch the real-repo experiments.yaml path (dispatch is path-based,
    # not content-based; this verifies routing rule still holds)
    target = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
    validator, _args = fdc.dispatch(target)
    assert validator == "validate_entrypoints.py"


def test_search_ledger_sees_real_repo_experiments():
    """search_ledger directly reads experiments.yaml on disk (no index)."""
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "search_ledger.py"), "value gap"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # exit 0 (no STRONG_MATCH) or 1 (STRONG_MATCH found); both OK
    assert r.returncode in {0, 1}
    # Must successfully read experiments.yaml (no error message)
    assert "Error" not in r.stdout and "Traceback" not in r.stdout
    assert "Error" not in r.stderr and "Traceback" not in r.stderr


# ==================== F. Manifest auto-write ====================


def test_run_manifest_path_derivation():
    """Verify pipeline derives run_manifest path under data/research_framework/run_manifests/."""
    # Read pipeline source for the path construction
    arp = _load_module("auto_research_pipeline.py")
    source = (SCRIPTS / "auto_research_pipeline.py").read_text()
    assert "run_manifests" in source
    # The pipeline should write under data/research_framework/run_manifests/
    assert "research_framework" in source


# ==================== G. Edge: malformed yaml ====================


def test_update_experiments_rejects_non_list(tmp_path: Path, monkeypatch):
    """If experiments.yaml has experiments: <not-list>, update should fail loud."""
    arp = _load_module("auto_research_pipeline.py")
    exp_path = tmp_path / "experiments.yaml"
    exp_path.write_text(yaml.safe_dump({"schema_version": 1, "experiments": "wrong_type"}))
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    with pytest.raises(arp.PipelineError, match="experiments.yaml experiments must be list"):
        arp.update_experiments(
            {"run_id": "x", "strategy_id": "s", "hypothesis_id": "h", "hypothesis": "y"},
            tmp_path, tmp_path / "m.yaml",
            {"status": "wip", "decision": "ok", "summary": {}},
            {"decision": "record-only"},
            dry_run=False,
        )


def test_update_experiments_creates_experiments_list_if_missing(tmp_path: Path, monkeypatch):
    """If experiments.yaml has no 'experiments' key, setdefault creates empty list."""
    arp = _load_module("auto_research_pipeline.py")
    exp_path = tmp_path / "experiments.yaml"
    exp_path.write_text(yaml.safe_dump({"schema_version": 1}))  # no experiments key
    monkeypatch.setattr(arp, "EXPERIMENTS", exp_path)

    arp.update_experiments(
        {"run_id": "create", "strategy_id": "s", "hypothesis_id": "h", "hypothesis": "y"},
        tmp_path, tmp_path / "m.yaml",
        {"status": "wip", "decision": "ok", "summary": {}},
        {"decision": "record-only"},
        dry_run=False,
    )
    data = yaml.safe_load(exp_path.read_text())
    assert "experiments" in data
    assert len(data["experiments"]) == 1


# ==================== H. Live repo sanity (read-only) ====================


def test_real_experiments_yaml_has_entries():
    """Real experiments.yaml on disk should have at least 4 entries (smoke)."""
    path = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
    data = yaml.safe_load(path.read_text())
    assert len(data.get("experiments", [])) >= 4


def test_real_research_insights_yaml_loadable():
    """research_insights.yaml should be valid yaml with expected structure."""
    path = REPO_ROOT / "data" / "research_framework" / "research_insights.yaml"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    assert "schema_version" in data
    assert "source_migration" in data
