from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "validate_truth_sync.py"
    spec = importlib.util.spec_from_file_location("validate_truth_sync", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strategy_core_change_triggers_truth_sync():
    v = _load_module()
    triggers = v.classify_triggers(["strategies/cb_arb/verifier.py"])
    assert triggers == [
        {"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}
    ]


def test_strategy_test_change_does_not_trigger_truth_sync():
    v = _load_module()
    triggers = v.classify_triggers(["strategies/cb_arb/tests/test_verifier.py"])
    assert triggers == []


def test_experiment_manifest_does_not_trigger_truth_sync():
    v = _load_module()

    def loader(path: str):
        return {"promotion_status": "experiment"}

    triggers = v.classify_triggers(
        ["data/research_framework/run_manifests/example.yaml"],
        manifest_loader=loader,
    )
    assert triggers == []


def test_rejected_manifest_triggers_truth_sync():
    v = _load_module()

    def loader(path: str):
        return {"promotion_status": "rejected"}

    triggers = v.classify_triggers(
        ["data/research_framework/run_manifests/example.yaml"],
        manifest_loader=loader,
    )
    assert triggers == [
        {
            "path": "data/research_framework/run_manifests/example.yaml",
            "reason": "run_manifest_promotion_status=rejected",
        }
    ]


def test_missing_manifest_triggers_truth_sync():
    v = _load_module()

    def loader(path: str):
        return None

    triggers = v.classify_triggers(
        ["data/research_framework/run_manifests/example.yaml"],
        manifest_loader=loader,
    )
    assert triggers == [
        {
            "path": "data/research_framework/run_manifests/example.yaml",
            "reason": "run_manifest_deleted_or_unreadable",
        }
    ]


def test_truth_doc_satisfies_sync():
    v = _load_module()
    assert v.has_truth_sync(["data/research_framework/current.yaml"])
    assert v.has_truth_sync(["data/research_framework/baseline_registry.yaml"])


def test_valid_waiver_covers_trigger_path():
    v = _load_module()
    waiver = {
        "schema_version": 1,
        "date": "2026-05-17",
        "decision": "no_truth_change",
        "reason": "Only refactors import paths; strategy truth is unchanged.",
        "changed_paths": ["strategies/cb_arb/verifier.py"],
        "reviewer": "codex",
    }
    assert v.validate_waiver_data("waiver.yaml", waiver) == []
    triggers = [{"path": "strategies/cb_arb/verifier.py", "reason": "strategy_core_changed"}]
    assert v.has_waiver_for_triggers(triggers, waiver["changed_paths"])


def test_waiver_rejects_placeholder_reason():
    v = _load_module()
    waiver = {
        "schema_version": 1,
        "date": "2026-05-17",
        "decision": "no_truth_change",
        "reason": "<TODO>",
        "changed_paths": ["strategies/cb_arb/verifier.py"],
        "reviewer": "codex",
    }
    errors = v.validate_waiver_data("waiver.yaml", waiver)
    assert any("reason" in e for e in errors)
    assert any("placeholder" in e for e in errors)
