"""Acceptance tests for Proposal Schema (component 7) + Spec Compiler (component 8).

Critical: Spec Compiler enforces closed_directions_tags (Claude guard A, the most important
anti-loop guard). It prevents AI proposing already-rejected research directions.

Expected API:
  proposal_schema.py:
    - validate_proposal(proposal: dict, mechanics_vocab: set, capability_vocab: set | None = None) -> list[str]
    - PROPOSAL_REQUIRED_FIELDS = {...}
  spec_compiler.py:
    - compile(proposal: dict, registry: dict, closed_tags: dict,
              recent_proposals: list[dict]) -> CompileResult
    - CompileResult attributes: status (READY/DRAFT/REJECT), spec_path, implementation_plan_path,
      reason, errors
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MODULE = REPO_ROOT / "framework" / "autonomous" / "proposal_schema.py"
COMPILER_MODULE = REPO_ROOT / "framework" / "autonomous" / "spec_compiler.py"
IDEATION_CYCLE_MODULE = REPO_ROOT / "framework" / "autonomous" / "ideation_cycle.py"
STRATEGY_IDEATOR_MODULE = REPO_ROOT / "framework" / "autonomous" / "strategy_ideator.py"


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
        "capability_ids": ["C101"],
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
    "expected_improvement", "required_executor", "required_data",
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


def test_proposal_schema_capability_ids_must_be_exact():
    """Capability ids must come from the registry; misspellings are rejected."""
    m = _load(SCHEMA_MODULE, "proposal_schema")
    p = _valid_proposal()
    p["capability_ids"] = ["C10"]
    errors = m.validate_proposal(p, _valid_mechanics_vocab(), capability_vocab={"C101"})
    assert any("unknown capability id" in error for error in errors)


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
        "capabilities": {
            "C101": {"mechanic": "valuation_adjustment", "label": "Valuation adjustment"},
            "C102": {"mechanic": "rolling_pnl_feedback", "label": "Rolling PnL feedback"},
            "C999": {"mechanic": "panic_filter", "label": "Closed panic filter"},
        },
        "executors": [{
            "id": "valuation_adjuster",
            "version": 1,
            "script_path": "scripts/evaluate_valuation.py",
            "can_test": ["valuation_adjustment"],
            "can_test_capability_ids": ["C101"],
            "cannot_test": ["rolling_pnl_feedback"],
            "cannot_test_capability_ids": ["C102"],
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
        recent_proposals=[],
    )
    assert result.status == "READY", f"expected READY; got {result.status}, reason: {result.reason}"


def test_compiler_closed_family_intersection_rejects(matching_registry, closed_tags_with_some):
    """Critical guard A: reject proposal whose mechanics intersect closed_tags."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()
    proposal["capability_ids"] = ["C999"]  # maps to panic_filter in closed_tags
    proposal["mechanics"] = ["panic_filter"]
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "REJECT"
    assert "closed" in result.reason.lower() or "panic_filter" in result.reason.lower()


def test_compiler_closed_family_name_rejects_even_if_capability_is_open(matching_registry, closed_tags_with_some):
    """Closed direction checks must include proposal family, not only resolved mechanics."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()
    proposal["family"] = "universe_subset_filter"
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "REJECT"
    assert "universe_subset_filter" in result.reason


def test_compiler_no_executor_match_produces_draft(closed_tags_with_some):
    """If no executor matches, produce DRAFT + implementation_plan (NOT silent fallback)."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    # Registry has executor for entry_filter, proposal needs valuation_adjustment
    registry = {
        "schema_version": 1,
        "capabilities": {
            "C101": {"mechanic": "valuation_adjustment", "label": "Valuation adjustment"},
            "C103": {"mechanic": "entry_filter", "label": "Entry filter"},
        },
        "executors": [{
            "id": "entry_only",
            "version": 1,
            "script_path": "scripts/evaluate_entry.py",
            "can_test": ["entry_filter"],
            "can_test_capability_ids": ["C103"],
            "cannot_test": [],
            "cannot_test_capability_ids": [],
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
        recent_proposals=[],
    )
    assert result.status == "DRAFT"
    assert result.implementation_plan_path is not None


def test_compiler_missing_capability_request_produces_draft(matching_registry, closed_tags_with_some):
    """If no registered capability fits, keep the idea as DRAFT instead of REJECT/READY."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()
    del proposal["capability_ids"]
    proposal["missing_capability_request"] = {
        "name": "new_formula_capability",
        "reason": "No existing capability changes the valuation formula in the requested way.",
    }
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "DRAFT"
    assert "missing registered capability" in result.reason


def test_compiler_required_data_missing_produces_draft(matching_registry, closed_tags_with_some, tmp_path, monkeypatch):
    """A matched executor cannot become READY when its registered data is absent."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    registry = dict(matching_registry)
    registry["matching_rules"] = {"require_required_data_exists": True}
    result = m.compile(
        proposal=_valid_proposal(),
        registry=registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "DRAFT"
    assert "data missing" in result.reason


def test_compiler_has_no_budget_gate(matching_registry, closed_tags_with_some):
    """Executor match, not budget metadata, determines READY."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    result = m.compile(
        proposal=_valid_proposal(),
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "READY"
    assert "budget" not in result.reason.lower()


def _proposal_hash(p: dict) -> str:
    payload = f"{p['hypothesis']}|{sorted(p['capability_ids'])}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def test_compiler_cycle_detection_blocks_repeat(matching_registry, closed_tags_with_some):
    """Critical guard B: same (hypothesis + mechanics) repeated within last 5 → reject."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    same_proposal = _valid_proposal()
    recent = [{"hypothesis": same_proposal["hypothesis"],
               "capability_ids": same_proposal["capability_ids"]} for _ in range(3)]
    result = m.compile(
        proposal=same_proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=recent,
    )
    assert result.status == "REJECT"
    assert "cycle" in result.reason.lower() or "repeat" in result.reason.lower()


def test_compiler_distinct_proposal_passes_cycle(matching_registry, closed_tags_with_some):
    """A distinct proposal vs recent ones must NOT be cycle-rejected."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    new_proposal = _valid_proposal()
    recent = [{"hypothesis": "different hypothesis", "capability_ids": ["C103"]} for _ in range(5)]
    result = m.compile(
        proposal=new_proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=recent,
    )
    assert result.status == "READY"


def test_compiler_schema_invalid_proposal_rejected(matching_registry, closed_tags_with_some):
    """Schema-invalid proposal → REJECT (with errors), not silent compile."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    bad = _valid_proposal()
    del bad["capability_ids"]
    result = m.compile(
        proposal=bad,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "REJECT"
    assert result.errors


def test_compiler_invalid_registry_produces_draft(matching_registry, closed_tags_with_some):
    """Bad executor registry is a system backlog item, not a runnable READY spec."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    registry = dict(matching_registry)
    registry["executors"] = [dict(matching_registry["executors"][0])]
    del registry["executors"][0]["can_test_capability_ids"]
    result = m.compile(
        proposal=_valid_proposal(),
        registry=registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
    )
    assert result.status == "DRAFT"
    assert result.errors


def test_capability_menu_includes_executor_and_data(matching_registry):
    """Ideator context must include id meaning, matching executor, and required data."""
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    menu = m.capability_menu(matching_registry)
    assert menu["C101"]["mechanic"] == "valuation_adjustment"
    assert menu["C101"]["available_executors"][0]["id"] == "valuation_adjuster"
    assert "data/cb_warehouse/cb_daily.parquet" in menu["C101"]["required_data"]


def test_ideation_closed_tags_include_runtime_forbidden_families():
    """Forbidden queue/config families must be injected before proposal and compile."""
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    closed = m.closed_tags_from_runtime(
        digest={"runs": []},
        config={"forbidden_families": ["option_source_pnl_feedback"]},
        queue_state={"forbidden_families": ["regime_classifier_variant"]},
    )
    assert "option_source_pnl_feedback" in closed
    assert "regime_classifier_variant" in closed


def test_ideation_cycle_no_longer_rewrites_proposals():
    """First AI call proposes once; missing executor is handled by design then code calls."""
    source = IDEATION_CYCLE_MODULE.read_text()
    assert "rewrite_until_valid" not in source
    assert "request_executor_tool_code" in source
    assert "request_executor_tool_design" in source


def test_ideation_cycle_normalizes_common_proposal_shape_drift():
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    normalized = m.normalize_proposal_shape({
        "test_design": "run train and test",
        "success_criteria": ["beat baseline"],
        "falsifiers": ["train worse", "test worse"],
    })
    assert normalized["test_design"] == {"description": "run train and test"}
    assert normalized["success_criteria"] == {"criteria": ["beat baseline"]}
    assert set(normalized["falsifiers"]) == {"train", "validate", "test"}


def test_strategy_ideator_accepts_fenced_yaml_response():
    m = _load(STRATEGY_IDEATOR_MODULE, "strategy_ideator")
    parsed = m._parse_mapping_response("```yaml\nproposal_id: p1\ncapability_ids:\n  - C001\n```")
    assert parsed == {"proposal_id": "p1", "capability_ids": ["C001"]}


def test_strategy_ideator_bad_response_becomes_rewriteable_stub():
    m = _load(STRATEGY_IDEATOR_MODULE, "strategy_ideator")
    parsed = m._parse_mapping_response('proposal_id: "cut off')
    assert parsed["proposal_id"] == "unparseable"


def test_recent_proposals_from_digest_feeds_repeat_detection():
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    digest = {
        "runs": [
            {
                "proposal_id": "p1",
                "hypothesis": "same idea",
                "capability_ids": ["C101"],
            }
        ]
    }
    proposals = m.recent_proposals_from_digest(digest)
    assert proposals == [{"proposal_id": "p1", "hypothesis": "same idea", "capability_ids": ["C101"]}]


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
        recent_proposals=[],
        output_dir=tmp_path,  # optional kwarg if compiler writes spec
    )
    assert result.status == "READY"
    if result.spec_path:
        import yaml
        spec_data = yaml.safe_load(Path(result.spec_path).read_text())
        assert "ideation_provenance" in spec_data, "READY spec must record ideation provenance"


def test_compiler_ready_spec_overwrites_freeform_mechanics_with_capability_mapping(
    matching_registry,
    closed_tags_with_some,
    tmp_path,
):
    """Spec truth must come from capability_ids, not AI-written free-form mechanics."""
    m = _load(COMPILER_MODULE, "spec_compiler")
    proposal = _valid_proposal()
    proposal["mechanics"] = ["entry_filter"]
    result = m.compile(
        proposal=proposal,
        registry=matching_registry,
        closed_tags=closed_tags_with_some,
        recent_proposals=[],
        output_dir=tmp_path,
    )
    assert result.status == "READY"
    import yaml
    spec_data = yaml.safe_load(Path(result.spec_path).read_text())
    assert spec_data["mechanics"] == ["valuation_adjustment"]
    assert spec_data["proposal"]["mechanics"] == ["valuation_adjustment"]


def test_ideation_cycle_offline_end_to_end_generates_ready_spec(tmp_path, monkeypatch):
    """Offline full chain: fake AI proposal -> compiler -> READY spec."""
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    monkeypatch.setattr(m, "record_framework_change", lambda **_: "eventhash")

    rf = tmp_path / "data" / "research_framework"
    rf.mkdir(parents=True)
    (rf / "strategy_ideator.yaml").write_text(yaml.safe_dump({
        "provider_registry": "data/research_framework/ai_providers.yaml",
        "allowed_entrypoint": "scripts/run_strategy_ideation_once.py",
    }), encoding="utf-8")
    (rf / "recent_results_digest.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "runs": [],
    }), encoding="utf-8")
    (rf / "evidence_tool_registry.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "tools": {},
    }), encoding="utf-8")
    (rf / "mechanics_vocab.yaml").write_text(yaml.safe_dump({
        "mechanics": [],
    }), encoding="utf-8")
    (rf / "executor_registry.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "capabilities": {
            "C001": {
                "mechanic": "static_candidate_position_scale",
                "label": "Static candidate scaling",
            },
        },
        "executors": [{
            "id": "static_scaler",
            "version": 1,
            "script_path": "scripts/evaluate_static_scaler.py",
            "can_test": ["static_candidate_position_scale"],
            "can_test_capability_ids": ["C001"],
            "cannot_test": [],
            "cannot_test_capability_ids": [],
            "required_data": [{"path": "data/source.parquet"}],
            "required_config_fields": ["output_dir"],
            "artifacts_produced": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
            "command_template": ["python3", "scripts/evaluate_static_scaler.py", "--output-dir", "{output_dir}"],
            "budget_estimate": {"sig_minutes": 0, "spot_minutes": 60, "local_minutes": 0},
            "vm_local_limits": {"vm_required": True, "local_allowed": False},
            "obsolescence_date": None,
        }],
    }), encoding="utf-8")

    proposal = {
        "proposal_id": "offline_chain_ready",
        "strategy_id": "cb_arb_value_gap_switch",
        "family": "static_candidate_position_scale",
        "hypothesis": "Scale only obviously weak option-sourced candidates.",
        "source_insight": "recent digest shows option-sourced candidates need narrower exposure tests",
        "expected_improvement": "train non-worse, test drawdown non-worse",
        "capability_ids": ["C001"],
        "mechanics": ["static_candidate_position_scale"],
        "required_executor": "static_scaler",
        "required_data": ["data/source.parquet"],
        "test_design": {"train_period": "2019-2024", "validate_period": "2023-2024", "test_period": "2025-2026"},
        "success_criteria": {"train_excess_min": 0.0, "validate_excess_min": 0.0, "test_dd_max_abs": 0.15},
        "falsifiers": {"train_excess_lt_baseline": True, "validate_excess_lt_zero": True, "test_dd_gt_15pct": True},
        "risk": "May reduce upside too much.",
        "why_not_repeated_failure": "This uses the registered static scaling capability only.",
        "related_prior_runs": [],
        "implementation_assumption": "Existing executor can evaluate the grid.",
    }

    class FakeResponse:
        content = json.dumps(proposal)
        provider_id = "fake_ai"
        response_hash = "fakehash"
        retries_used = 0

    class FakeAdapter:
        def call_active_provider(self, prompt, schema):
            return FakeResponse()

    paths = m.ResearchPaths.from_repo_root(tmp_path)
    payload = m.IdeationCycle(paths=paths, ai_adapter=FakeAdapter()).run_once(
        output_root=tmp_path / "data",
    )

    assert payload["status"] == "READY"
    spec_path = Path(payload["spec_path"])
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert spec["status"] == "READY"
    assert spec["capability_ids"] == ["C001"]
    assert spec["mechanics"] == ["static_candidate_position_scale"]
    assert spec["ideation_provenance"]["ai_provider"] == "fake_ai"


def test_ideation_cycle_missing_executor_requests_tool_code(tmp_path, monkeypatch):
    """If no executor can run the idea, ask AI once more for draft tool code."""
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    monkeypatch.setattr(m, "record_framework_change", lambda **_: "eventhash")

    rf = tmp_path / "data" / "research_framework"
    rf.mkdir(parents=True)
    (rf / "strategy_ideator.yaml").write_text(yaml.safe_dump({
        "provider_registry": "data/research_framework/ai_providers.yaml",
        "allowed_entrypoint": "scripts/run_strategy_ideation_once.py",
    }), encoding="utf-8")
    (rf / "recent_results_digest.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "runs": [],
    }), encoding="utf-8")
    (rf / "research_queue.yaml").write_text(yaml.safe_dump({
        "enabled": True,
        "queue": [],
    }), encoding="utf-8")
    (rf / "evidence_tool_registry.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "tools": {},
    }), encoding="utf-8")
    (rf / "mechanics_vocab.yaml").write_text(yaml.safe_dump({
        "mechanics": [],
    }), encoding="utf-8")
    (rf / "executor_registry.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "capabilities": {
            "C007": {
                "mechanic": "new_exit_rule",
                "label": "Change exit logic",
            },
        },
        "executors": [],
    }), encoding="utf-8")

    proposal = {
        "proposal_id": "missing_executor_case",
        "strategy_id": "cb_arb_value_gap_switch",
        "family": "exit_rule_optimization",
        "hypothesis": "Change exit when value gap closes slowly.",
        "source_insight": "recent runs show exit logic is the unresolved issue",
        "expected_improvement": "lower 2020 drawdown without reducing test return",
        "capability_ids": ["C007"],
        "required_executor": "adaptive_exit_evaluator",
        "required_data": [],
        "test_design": {"train_period": "2019-2024", "validate_period": "2020", "test_period": "2025-2026"},
        "success_criteria": {"train_excess_min": 0.0, "validate_excess_min": 0.0, "test_dd_max_abs": 0.15},
        "falsifiers": {"train": True, "validate": True, "test": True},
        "risk": "May exit winners too early.",
        "why_not_repeated_failure": "Exit logic has not been tested by existing executors.",
        "related_prior_runs": [],
        "implementation_assumption": "A new evaluator can reuse existing value-gap ranks.",
    }
    tool_design = {
        "tool_request_id": "adaptive_exit_evaluator",
        "why_existing_tools_insufficient": "No registered executor can change exit logic.",
        "reviewed_existing_executor_ids": [],
        "registry_entry": {
            "id": "adaptive_exit_evaluator",
            "script_path": "scripts/evaluate_adaptive_exit.py",
            "strategy_id": "cb_arb_value_gap_switch",
            "family": "exit_rule_optimization",
            "can_test_capability_ids": ["C007"],
            "command_template": ["python3", "scripts/evaluate_adaptive_exit.py"],
            "artifacts_produced": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
        },
        "implementation_outline": {
            "script_path": "scripts/evaluate_adaptive_exit.py",
            "main_inputs": ["output_dir"],
            "core_steps": ["load data", "apply exit rule", "write artifacts"],
        },
        "validation_plan": ["python3 -m py_compile generated_executor/adaptive_exit_evaluator.py"],
    }
    tool_code = {
        "files": [
            {
                "path": "generated_executor/adaptive_exit_evaluator.py",
                "content": "print('draft evaluator')\n",
                "purpose": "draft evaluator",
            }
        ],
    }

    class FakeResponse:
        def __init__(self, payload, response_hash):
            self.content = json.dumps(payload)
            self.provider_id = "fake_ai"
            self.response_hash = response_hash
            self.retries_used = 0

    class FakeAdapter:
        def __init__(self):
            self.calls = 0

        def call_active_provider(self, prompt, schema):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(proposal, "proposalhash")
            if self.calls == 2:
                return FakeResponse(tool_design, "designhash")
            return FakeResponse(tool_code, "codehash")

    adapter = FakeAdapter()
    paths = m.ResearchPaths.from_repo_root(tmp_path)
    payload = m.IdeationCycle(paths=paths, ai_adapter=adapter).run_once(
        output_root=tmp_path / "data",
    )

    assert adapter.calls == 3
    assert payload["status"] == "DRAFT"
    package = payload["executor_tool_package"]
    assert package["tool_request_id"] == "adaptive_exit_evaluator"
    assert package["status"] == "draft_tool_code"
    assert package["design_response_hash"] == "designhash"
    assert package["code_response_hash"] == "codehash"
    assert Path(package["descriptor_path"]).exists()
    assert Path(package["written_files"][0]).exists()


def test_executor_tool_package_validation_blocks_bad_ai_tool_response(tmp_path, monkeypatch):
    """Second AI call is accepted only when hard-coded fields are present."""
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    monkeypatch.setattr(m, "record_framework_change", lambda **_: "eventhash")

    run_dir = tmp_path / "run"

    class FakeResponse:
        content = json.dumps({"tool_request_id": "bad_tool"})
        provider_id = "fake_ai"
        response_hash = "badtoolhash"
        retries_used = 0

    class FakeAdapter:
        def call_active_provider(self, prompt, schema):
            return FakeResponse()

    result = m.request_executor_tool_code(
        proposal={"proposal_id": "p1"},
        compile_reason="no strict executor match",
        compile_errors=[],
        registry={"executors": []},
        ai_adapter=FakeAdapter(),
        run_dir=run_dir,
        store=m.ArtifactStore(),
    )
    assert result["status"] == "invalid_tool_code_response"
    assert result["written_files"] == []
    assert result["validation_errors"]


def test_executor_tool_package_validation_blocks_placeholder_code():
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    errors = m.validate_executor_tool_package({
        "tool_request_id": "placeholder_tool",
        "why_existing_tools_insufficient": "No current executor changes exits.",
        "reviewed_existing_executor_ids": ["option_position_sizing"],
        "registry_entry": {"id": "placeholder_tool"},
        "files": [{
            "path": "generated_executor/placeholder_tool.py",
            "content": "trade_records = []\n# detailed simulation code goes here\n",
        }],
        "validation_plan": "syntax check",
    })
    assert any("placeholder" in error or "trade_records" in error for error in errors)


def test_executor_tool_package_validation_blocks_truncated_python():
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    errors = m.validate_executor_tool_package({
        "tool_request_id": "truncated_tool",
        "why_existing_tools_insufficient": "No current executor changes exits.",
        "reviewed_existing_executor_ids": ["option_position_sizing"],
        "registry_entry": {"id": "truncated_tool"},
        "files": [{
            "path": "generated_executor/truncated_tool.py",
            "content": "def run(:\n    return 1\n",
        }],
        "validation_plan": "syntax check",
    })
    assert any("not valid Python" in error for error in errors)


def test_executor_tool_request_template_requires_yaml_literal_code():
    m = _load(IDEATION_CYCLE_MODULE, "ideation_cycle")
    template = m.executor_tool_required_yaml_template()
    assert "tool_request_id:" in template
    assert "content: |" in template
    source = IDEATION_CYCLE_MODULE.read_text()
    assert "Return YAML only." in source
    assert "Do not return JSON." in source
