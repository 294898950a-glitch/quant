"""Acceptance tests for Proposal Schema (component 7) + Spec Compiler (component 8).

Critical: Spec Compiler enforces closed_directions_tags (Claude guard A, the most important
anti-loop guard). It prevents AI proposing already-rejected research directions.

Expected API:
  proposal_schema.py:
    - validate_proposal(proposal: dict, mechanics_vocab: set) -> list[str]
    - PROPOSAL_REQUIRED_FIELDS = {...}
  spec_compiler.py:
    - compile(proposal: dict, registry: dict, closed_tags: dict, budget_cap: float,
              recent_proposals: list[dict]) -> CompileResult
    - CompileResult attributes: status (READY/DRAFT/REJECT), spec_path, implementation_plan_path,
      reason, errors
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MODULE = REPO_ROOT / "framework" / "autonomous" / "proposal_schema.py"
COMPILER_MODULE = REPO_ROOT / "framework" / "autonomous" / "spec_compiler.py"


def _load(path: Path, name: str):
    if not path.exists():
        pytest.skip(f"{path} not implemented yet")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _valid_proposal() -> dict:
    return {
        "proposal_id": "valid_prop_20260517",
        "strategy_id": "cb_arb_value_gap_switch",
        "family": "valuation_modification",
        "hypothesis": "Test hypothesis about valuation",
        "source_insight": "research_insight_id_here",
        "expected_improvement": "train +0.5pp, validate +0pp, 2020 +5pp",
        "mechanics": ["valuation_adjustment"],  # in vocab, not closed
        "required_executor": "valuation_adjuster",
        "required_data": ["data/cb_warehouse/cb_daily.parquet"],
        "test_design": {
            "train_period": "2019-2022",
            "validate_period": "2023-2024",
            "test_period": "2025-2026",
        },
        "success_criteria": {
            "train_excess_min": 0.005,
            "validate_excess_min": 0.0,
            "test_dd_max_abs": 0.15,
        },
        "falsifiers": {
            "train_excess_lt_baseline": True,
            "validate_excess_lt_zero": True,
            "any_year_dd_gt_15pct": True,
        },
        "risk": "May overfit train",
        "why_not_repeated_failure": "Not in closed_directions; mechanics distinct from prior 7 rejects",
        "related_prior_runs": ["cb_arb_value_gap_rerun_8_stop_revaluation_2026-05-17"],
        "implementation_assumption": "Executor available",
    }


def _valid_mechanics_vocab() -> set[str]:
    return {
        "entry_filter", "valuation_adjustment", "exit_rule",
        "universe_filter", "position_sizing", "regime_conditioning",
        "rolling_pnl_feedback",  # listed but reject family
    }


# =================== Proposal Schema ===================


def test_proposal_schema_valid_passes():
    m = _load(SCHEMA_MODULE, "proposal_schema")
    errors = m.validate_proposal(_valid_proposal(), _valid_mechanics_vocab())
    assert errors == [], f"unexpected errors: {errors}"


@pytest.mark.parametrize("missing_field", [
    "proposal_id", "strategy_id", "family", "hypothesis", "source_insight",
    "expected_improvement", "mechanics", "required_executor", "required_data",
    "test_design", "success_criteria", "falsifiers", "risk",
    "why_not_repeated_failure", "related_prior_runs", "implementation_assumption",
])
def test_proposal_schema_missing_required_field(missing_field: str):
    m = _load(SCHEMA_MODULE, "proposal_schema")
    p = _valid_proposal()
    del p[missing_field]
    errors = m.validate_proposal(p, _valid_mechanics_vocab())
    assert any(missing_field in e for e in errors), \
        f"expected error mentioning {missing_field}; got: {errors}"


def test_proposal_schema_mechanics_must_be_in_vocab():
    """mechanics tags must come from controlled vocabulary (no free-form)."""
    m = _load(SCHEMA_MODULE, "proposal_schema")
    p = _valid_proposal()
    p["mechanics"] = ["totally_invented_mechanic_xyz"]
    errors = m.validate_proposal(p, _valid_mechanics_vocab())
    assert errors, "expected error for unknown mechanic tag"


def test_proposal_schema_falsifiers_required_keys():
    """Falsifiers must include train + validate + test dimensions per HDRF."""
    m = _load(SCHEMA_MODULE, "proposal_schema")
    p = _valid_proposal()
    p["falsifiers"] = {"train_only": True}  # missing validate/test
    errors = m.validate_proposal(p, _valid_mechanics_vocab())
    assert errors, "expected error for incomplete falsifiers"


# =================== Spec Compiler ===================


@pytest.fixture
def matching_registry() -> dict:
    return {
        "schema_version": 1,
        "executors": [{
            "id": "valuation_adjuster",
            "version": 1,
            "script_path": "scripts/evaluate_valuation.py",
            "can_test": ["valuation_adjustment"],
            "cannot_test": ["rolling_pnl_feedback"],
            "required_data": [
                {"path": "data/cb_warehouse/cb_daily.parquet", "schema_hash": "abc123"}
            ],
            "required_config_fields": ["haircut_pct"],
            "artifacts_produced": ["summary.csv"],
            "command_template": ["scripts/evaluate_valuation.py", "--output-dir", "{output_dir}"],
            "budget_estimate": {"sig_minutes": 0, "spot_minutes": 60, "local_minutes": 0},
            "vm_local_limits": {"vm_required": True, "local_allowed": False},
            "obsolescence_date": None,
        }],
    }


@pytest.fixture
def closed_tags_with_some() -> dict:
    return {
        "panic_filter": {"insight_id": "panic_detector_dead_end", "reject_count": 4},
        "universe_subset_filter": {"insight_id": "universe_filter_retest_reject_2026_05_17", "reject_count": 1},
    }


def test_compiler_valid_proposal_produces_ready(matching_registry, closed_tags_with_some):
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()  # mechanics: valuation_adjustment, not in closed
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=[],
    )
    assert result.status == "READY", f"expected READY; got {result.status}, reason: {result.reason}"


def test_compiler_closed_family_intersection_rejects(matching_registry, closed_tags_with_some):
    """Critical guard A: reject proposal whose mechanics intersect closed_tags."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()
    proposal["mechanics"] = ["panic_filter"]  # in closed_tags
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=[],
    )
    assert result.status == "REJECT"
    assert "closed" in result.reason.lower() or "panic_filter" in result.reason.lower()


def test_compiler_no_executor_match_produces_draft(closed_tags_with_some):
    """If no executor matches, produce DRAFT + implementation_plan (NOT silent fallback)."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    # Registry has executor for entry_filter, proposal needs valuation_adjustment
    registry = {
        "schema_version": 1,
        "executors": [{
            "id": "entry_only",
            "version": 1,
            "script_path": "scripts/evaluate_entry.py",
            "can_test": ["entry_filter"],
            "cannot_test": [],
            "required_data": [{"path": "data/cb_warehouse/cb_daily.parquet", "schema_hash": "abc"}],
            "required_config_fields": ["cost_model_enabled"],
            "artifacts_produced": ["summary.csv"],
            "command_template": ["scripts/evaluate_entry.py"],
            "budget_estimate": {"sig_minutes": 0, "spot_minutes": 60, "local_minutes": 0},
            "vm_local_limits": {"vm_required": True, "local_allowed": False},
            "obsolescence_date": None,
        }],
    }
    result = m.compile(
        proposal=_valid_proposal(),  # mechanics: valuation_adjustment
        registry=registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=[],
    )
    assert result.status == "DRAFT"
    assert result.implementation_plan_path is not None


def test_compiler_budget_over_cap_produces_draft(matching_registry, closed_tags_with_some):
    """If budget exceeds cap, produce DRAFT (don't auto-run expensive jobs)."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    result = m.compile(
        proposal=_valid_proposal(),
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=0.01,  # very low cap
        recent_proposals=[],
    )
    assert result.status == "DRAFT"
    assert "budget" in result.reason.lower()


def _proposal_hash(p: dict) -> str:
    payload = f"{p['hypothesis']}|{sorted(p['mechanics'])}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def test_compiler_cycle_detection_blocks_repeat(matching_registry, closed_tags_with_some):
    """Critical guard B: same (hypothesis + mechanics) repeated within last 5 → reject."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    same_proposal = _valid_proposal()
    recent = [{"hypothesis": same_proposal["hypothesis"],
               "mechanics": same_proposal["mechanics"]} for _ in range(3)]
    result = m.compile(
        proposal=same_proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=recent,
    )
    assert result.status == "REJECT"
    assert "cycle" in result.reason.lower() or "repeat" in result.reason.lower()


def test_compiler_distinct_proposal_passes_cycle(matching_registry, closed_tags_with_some):
    """A distinct proposal vs recent ones must NOT be cycle-rejected."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    new_proposal = _valid_proposal()
    recent = [{"hypothesis": "different hypothesis", "mechanics": ["entry_filter"]} for _ in range(5)]
    result = m.compile(
        proposal=new_proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=recent,
    )
    assert result.status == "READY"


def test_compiler_schema_invalid_proposal_rejected(matching_registry, closed_tags_with_some):
    """Schema-invalid proposal → REJECT (with errors), not silent compile."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    bad = _valid_proposal()
    del bad["mechanics"]
    result = m.compile(
        proposal=bad,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=[],
    )
    assert result.status == "REJECT"
    assert result.errors


def test_compiler_ready_spec_has_provenance(matching_registry, closed_tags_with_some, tmp_path):
    """READY spec yaml must include ideation_provenance field."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()
    proposal["ai_provider"] = "claude"
    proposal["prompt_path"] = "data/research_framework/prompts/test.txt"
    proposal["response_hash"] = "deadbeef"
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        budget_cap=100.0,
        recent_proposals=[],
        output_dir=tmp_path,  # optional kwarg if compiler writes spec
    )
    assert result.status == "READY"
    if result.spec_path:
        import yaml
        spec_data = yaml.safe_load(Path(result.spec_path).read_text())
        assert "ideation_provenance" in spec_data, "READY spec must record ideation provenance"
