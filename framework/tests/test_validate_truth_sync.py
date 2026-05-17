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


def test_audit_relevant_paths_include_truth_and_trigger_paths():
    v = _load_module()
    paths = [
        "data/research_framework/current.yaml",
        "data/research_framework/strategies.yaml",
        "scripts/unrelated.py",
    ]
    triggers = [
        {
            "path": "data/research_framework/strategies.yaml",
            "reason": "strategy_registry_changed",
        }
    ]

    assert v.audit_relevant_paths(paths, triggers) == [
        "data/research_framework/current.yaml",
        "data/research_framework/strategies.yaml",
    ]


def test_append_protected_action_audit_is_idempotent(tmp_path, monkeypatch):
    v = _load_module()
    audit_path = tmp_path / "protected_action_audit.jsonl"
    paths = ["data/research_framework/current.yaml", "data/research_framework/strategies.yaml"]
    triggers = [{"path": "data/research_framework/strategies.yaml", "reason": "strategy_registry_changed"}]
    monkeypatch.setattr(v, "diff_for_paths", lambda relevant, source: "same diff")

    first = v.append_protected_action_audit(
        paths=paths,
        source="working-tree",
        triggers=triggers,
        errors=[],
        waiver_covered=[],
        audit_path=audit_path,
    )
    second = v.append_protected_action_audit(
        paths=paths,
        source="working-tree",
        triggers=triggers,
        errors=[],
        waiver_covered=[],
        audit_path=audit_path,
    )

    assert first == second
    assert len(audit_path.read_text(encoding="utf-8").splitlines()) == 1


def test_framework_doc_check_truth_sync_secondary_paths():
    path = Path(__file__).resolve().parents[2] / "scripts" / "framework_doc_check.py"
    spec = importlib.util.spec_from_file_location("framework_doc_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = Path(__file__).resolve().parents[2]
    assert module.needs_truth_sync_check(root / "data/research_framework/current.yaml")
    assert module.needs_truth_sync_check(root / "data/research_framework/strategies.yaml")
    assert module.needs_truth_sync_check(root / "data/research_framework/run_manifests/x.yaml")
    assert not module.needs_truth_sync_check(root / "data/research_framework/experiments.yaml")
