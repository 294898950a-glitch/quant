"""Acceptance tests for Executor Registry (autonomous research framework component 6).

Codex must implement framework/autonomous/executor_registry.py to make these tests pass.

Expected public API:
  - load_registry(path: Path) -> dict
  - validate_registry_schema(registry: dict) -> list[str]  # errors
  - match_executor(proposal_mechanics: set, proposal_data: set, registry: dict) -> dict | None
  - ExecutorMatch dataclass with: executor_id, version, script_path, command_template, budget_estimate, required_data
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "framework" / "autonomous" / "executor_registry.py"


def _load_module():
    if not MODULE_PATH.exists():
        pytest.skip(f"{MODULE_PATH} not implemented yet (Codex acceptance pending)")
    spec = importlib.util.spec_from_file_location("executor_registry", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_executor(eid: str = "test_executor") -> dict:
    return {
        "id": eid,
        "version": 1,
        "script_path": "scripts/evaluate_test.py",
        "can_test": ["entry_filter"],
        "cannot_test": ["rolling_pnl_feedback"],
        "required_data": [
            {"path": "data/cb_warehouse/cb_daily.parquet", "schema_hash": "abc123"}
        ],
        "required_config_fields": ["cost_model_enabled"],
        "artifacts_produced": ["summary.csv"],
        "command_template": ["scripts/evaluate_test.py", "--output-dir", "{output_dir}"],
        "budget_estimate": {"sig_minutes": 0, "spot_minutes": 60, "local_minutes": 0},
        "vm_local_limits": {"vm_required": True, "local_allowed": False},
        "obsolescence_date": None,
    }


def _valid_registry(executors: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "executors": executors if executors is not None else [_minimal_executor()],
    }


# ========= load_registry =========


def test_load_registry_returns_dict(tmp_path: Path):
    m = _load_module()
    reg_path = tmp_path / "executor_registry.yaml"
    reg_path.write_text(yaml.safe_dump(_valid_registry()))
    result = m.load_registry(reg_path)
    assert isinstance(result, dict)
    assert "executors" in result


def test_load_registry_missing_file_raises(tmp_path: Path):
    m = _load_module()
    missing = tmp_path / "nope.yaml"
    with pytest.raises((FileNotFoundError, OSError)):
        m.load_registry(missing)


# ========= validate_registry_schema =========


def test_validate_schema_minimal_valid():
    m = _load_module()
    errors = m.validate_registry_schema(_valid_registry())
    assert errors == [], f"unexpected errors: {errors}"


@pytest.mark.parametrize("missing_field", [
    "id", "version", "script_path", "can_test", "cannot_test", "required_data",
    "required_config_fields", "artifacts_produced", "command_template",
    "budget_estimate", "vm_local_limits",
])
def test_validate_schema_missing_required_field(missing_field: str):
    m = _load_module()
    exec_data = _minimal_executor()
    del exec_data[missing_field]
    registry = _valid_registry([exec_data])
    errors = m.validate_registry_schema(registry)
    assert any(missing_field in e for e in errors), \
        f"expected error mentioning {missing_field}; got: {errors}"


def test_validate_schema_duplicate_executor_id():
    m = _load_module()
    e1 = _minimal_executor("dup")
    e2 = _minimal_executor("dup")
    registry = _valid_registry([e1, e2])
    errors = m.validate_registry_schema(registry)
    assert any("dup" in e.lower() or "duplicate" in e.lower() for e in errors)


def test_validate_schema_empty_executors_list():
    m = _load_module()
    errors = m.validate_registry_schema({"schema_version": 1, "executors": []})
    # empty is suspicious but not necessarily wrong; either reject or empty-ok
    # Acceptance: at least don't crash
    assert isinstance(errors, list)


# ========= match_executor =========


def test_match_executor_exact_match():
    m = _load_module()
    exec_data = _minimal_executor()
    registry = _valid_registry([exec_data])
    match = m.match_executor(
        proposal_mechanics={"entry_filter"},
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match is not None
    assert match["id"] == "test_executor" or getattr(match, "executor_id", None) == "test_executor"


def test_match_executor_no_match_returns_none():
    m = _load_module()
    exec_data = _minimal_executor()
    registry = _valid_registry([exec_data])
    match = m.match_executor(
        proposal_mechanics={"position_sizing"},  # not in can_test
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match is None


def test_match_executor_cannot_test_intersection_blocks():
    """Critical: prevent the rolling_pnl_feedback bug (Codex 22:02 known bug)."""
    m = _load_module()
    exec_data = _minimal_executor()
    exec_data["can_test"] = ["entry_filter"]
    exec_data["cannot_test"] = ["rolling_pnl_feedback"]
    registry = _valid_registry([exec_data])
    match = m.match_executor(
        proposal_mechanics={"entry_filter", "rolling_pnl_feedback"},  # cannot_test intersected
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match is None, "cannot_test intersection must block match"


def test_match_executor_missing_required_data_blocks():
    m = _load_module()
    exec_data = _minimal_executor()
    registry = _valid_registry([exec_data])
    match = m.match_executor(
        proposal_mechanics={"entry_filter"},
        proposal_data=set(),  # 提供数据不足
        registry=registry,
    )
    assert match is None


def test_match_executor_obsolete_excluded():
    m = _load_module()
    exec_data = _minimal_executor()
    exec_data["obsolescence_date"] = (date.today() - timedelta(days=1)).isoformat()
    registry = _valid_registry([exec_data])
    match = m.match_executor(
        proposal_mechanics={"entry_filter"},
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match is None, "obsolete executor must not match"


def test_match_executor_future_obsolescence_still_matches():
    m = _load_module()
    exec_data = _minimal_executor()
    exec_data["obsolescence_date"] = (date.today() + timedelta(days=30)).isoformat()
    registry = _valid_registry([exec_data])
    match = m.match_executor(
        proposal_mechanics={"entry_filter"},
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match is not None


def test_match_executor_no_closest_fallback():
    """If multiple partial-match candidates, must NOT silently fall back to closest.
    The temporary-loop bug was caused by exactly this kind of silent fallback."""
    m = _load_module()
    # 2 executors: one supports entry_filter only, another supports rolling_pnl
    e1 = _minimal_executor("entry_only")
    e1["can_test"] = ["entry_filter"]
    e2 = _minimal_executor("rolling_only")
    e2["can_test"] = ["rolling_pnl_feedback"]
    registry = _valid_registry([e1, e2])
    # Proposal needs BOTH mechanics; neither executor supports both
    match = m.match_executor(
        proposal_mechanics={"entry_filter", "rolling_pnl_feedback"},
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match is None, "Must NOT silently fall back to nearest executor (the bug case)"


def test_match_executor_multiple_valid_returns_one():
    """If 2 executors fully match, registry must return one consistently (deterministic)."""
    m = _load_module()
    e1 = _minimal_executor("first")
    e2 = _minimal_executor("second")
    registry = _valid_registry([e1, e2])
    match1 = m.match_executor(
        proposal_mechanics={"entry_filter"},
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    match2 = m.match_executor(
        proposal_mechanics={"entry_filter"},
        proposal_data={"data/cb_warehouse/cb_daily.parquet"},
        registry=registry,
    )
    assert match1 is not None
    assert match2 is not None
    # Deterministic: same input → same output (no random tie-breaking)
    id1 = match1["id"] if isinstance(match1, dict) else getattr(match1, "executor_id")
    id2 = match2["id"] if isinstance(match2, dict) else getattr(match2, "executor_id")
    assert id1 == id2
