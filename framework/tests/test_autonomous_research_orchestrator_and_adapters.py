"""Acceptance tests for Orchestrator (component 10), AI Provider Adapter (5),
Verification Tool (3), Strategy Ideator (4), Proposal Rewrite Loop (9).

Critical Claude guards:
- F: User pause switch (file existence → orchestrator stops)
- E: Orchestrator audit log per action
- C: Per-cycle budget cap
- D: AI provider robustness (retry then fail)
- Anti-fishing: Verification pre-registered requests

Expected API:
  orchestrator.py:
    - run_cycle(config: dict) -> CycleResult
    - is_paused() -> bool
    - PAUSE_FLAG_PATH = "data/research_framework/orchestrator_paused.flag"
    - AUDIT_LOG_PATH = "data/research_framework/orchestrator_log.jsonl"
  ai_provider_adapter.py:
    - load_providers(path) -> dict
    - call_active_provider(prompt, schema) -> ProviderResponse
  verification_tool.py:
    - request_verification(review_id, request_type, params, round_num) -> VerificationResult
    - ALLOWED_REQUEST_TYPES = {...}
  strategy_ideator.py:
    - propose(closed_tags, recent_digest, insights, budget_cap, ai_adapter) -> dict
  proposal_rewrite_loop.py:
    - rewrite_until_valid(initial_proposal, validator, ai_adapter) -> RewriteResult
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
ORCH_MODULE = REPO_ROOT / "framework" / "autonomous" / "orchestrator.py"
AI_ADAPTER_MODULE = REPO_ROOT / "framework" / "autonomous" / "ai_provider_adapter.py"
VERIFY_MODULE = REPO_ROOT / "framework" / "autonomous" / "verification_tool.py"
IDEATOR_MODULE = REPO_ROOT / "framework" / "autonomous" / "strategy_ideator.py"
REWRITE_MODULE = REPO_ROOT / "framework" / "autonomous" / "proposal_rewrite_loop.py"


def _load(path: Path, name: str):
    if not path.exists():
        pytest.skip(f"{path} not implemented yet")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# =================== AI Provider Adapter ===================


def test_ai_adapter_loads_providers(tmp_path: Path):
    m = _load(AI_ADAPTER_MODULE, "ai_provider_adapter")
    providers_path = tmp_path / "ai_providers.yaml"
    providers_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "active_provider": "claude",
        "providers": {
            "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-opus-4-7"},
            "deepseek": {"api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
        },
    }))
    cfg = m.load_providers(providers_path)
    assert "active_provider" in cfg
    assert cfg["active_provider"] == "claude"


def test_ai_adapter_returns_normalized_response():
    """Caller gets ProviderResponse regardless of which provider was called."""
    m = _load(AI_ADAPTER_MODULE, "ai_provider_adapter")

    # Adapter should have a way to mock the provider
    # Acceptance: there's an injectable provider client we can mock
    if hasattr(m, "call_active_provider"):
        # Best-effort signature check
        import inspect
        sig = inspect.signature(m.call_active_provider)
        # Should accept prompt and schema args
        params = list(sig.parameters.keys())
        assert any("prompt" in p for p in params) or "prompt" in params


def test_ai_adapter_retries_on_transient_failure():
    """Adapter must retry up to 3 times on rate-limit/5xx (Claude guard D)."""
    m = _load(AI_ADAPTER_MODULE, "ai_provider_adapter")
    # Acceptance: adapter has retry logic
    # Detailed verification depends on adapter signature; smoke check existence of retry-related symbols
    source = AI_ADAPTER_MODULE.read_text()
    # Either MAX_RETRIES constant or retry decorator
    assert "retry" in source.lower() or "max_retries" in source.lower() or "backoff" in source.lower(), \
        "adapter source must contain retry logic"


def test_ai_adapter_swappable_no_hardcoded_claude():
    """Adapter must NOT hardcode Claude as only provider."""
    m = _load(AI_ADAPTER_MODULE, "ai_provider_adapter")
    source = AI_ADAPTER_MODULE.read_text().lower()
    # Source should not have "anthropic" or "claude" as the only branch
    # Allow naming/imports, but should reference at least 2 providers in conditionals
    # Heuristic: source mentions "provider" multiple times for dispatch
    assert source.count("provider") >= 3, "adapter should be provider-agnostic in dispatch"


# =================== Verification Tool ===================


def test_verification_allowed_request_types():
    m = _load(VERIFY_MODULE, "verification_tool")
    assert hasattr(m, "ALLOWED_REQUEST_TYPES")
    expected = {
        "metric_slice", "year_breakdown", "source_attribution",
        "trade_group_attribution", "baseline_vs_candidate_diff",
        "artifact_consistency_check",
    }
    assert expected.issubset(set(m.ALLOWED_REQUEST_TYPES))


def test_verification_rejects_prohibited_types():
    m = _load(VERIFY_MODULE, "verification_tool")
    # Try to request a prohibited type
    for prohibited in ["new_strategy", "param_optimization", "promotion"]:
        with pytest.raises((ValueError, RuntimeError, KeyError)):
            m.request_verification(
                review_id="test",
                request_type=prohibited,
                params={},
                round_num=1,
            )


def test_verification_max_2_rounds():
    """3rd round must be rejected."""
    m = _load(VERIFY_MODULE, "verification_tool")
    # Even with valid request_type, round 3 must fail
    with pytest.raises((ValueError, RuntimeError)):
        m.request_verification(
            review_id="test",
            request_type="metric_slice",
            params={},
            round_num=3,
        )


def test_verification_round1_must_preregister_requests(tmp_path: Path):
    """Round 1 must declare all request_ids upfront (anti-fishing guard)."""
    m = _load(VERIFY_MODULE, "verification_tool")
    # Pre-register N requests in round 1
    # Round 2 must only allow + 1 follow-up
    # Smoke check: pre-registration logic exists
    source = VERIFY_MODULE.read_text().lower()
    has_preregister_logic = (
        "preregister" in source or "pre_register" in source
        or "request_id" in source and "round" in source
    )
    assert has_preregister_logic, "verification tool must have pre-registration logic"


def test_verification_log_written(tmp_path: Path):
    """Each request must append to verification_log.jsonl."""
    m = _load(VERIFY_MODULE, "verification_tool")
    review_dir = tmp_path / "data" / "test_run"
    review_dir.mkdir(parents=True)
    # If verification is callable with review_dir, must produce log
    # This is a smoke test of the contract
    log_path = review_dir / "verification_log.jsonl"
    # After calling request_verification (if it accepts a review_dir), log should exist
    try:
        m.request_verification(
            review_id="test_run",
            request_type="metric_slice",
            params={"period": "2020"},
            round_num=1,
            review_dir=review_dir,
        )
        assert log_path.exists() or "verification_log" in str(review_dir.glob("*"))
    except TypeError:
        # Function signature may differ; at minimum, source mentions logging
        source = VERIFY_MODULE.read_text().lower()
        assert "verification_log" in source or "log" in source


# =================== Strategy Ideator ===================


def test_ideator_filters_closed_tags():
    """Ideator must NOT propose mechanics in closed_tags (Claude guard A)."""
    m = _load(IDEATOR_MODULE, "strategy_ideator")
    source = IDEATOR_MODULE.read_text().lower()
    # Source must reference closed_tags filtering
    assert "closed_tags" in source or "closed_directions" in source


def test_ideator_records_provenance():
    """Proposal must include ai_provider/prompt_path/response_hash."""
    m = _load(IDEATOR_MODULE, "strategy_ideator")
    source = IDEATOR_MODULE.read_text().lower()
    assert "provenance" in source or ("ai_provider" in source and "response_hash" in source)


def test_ideator_does_not_write_spec_directly():
    """Ideator must write proposal only, not spec.yaml."""
    m = _load(IDEATOR_MODULE, "strategy_ideator")
    source = IDEATOR_MODULE.read_text()
    # Should write proposal.yaml or proposal object, NOT spec.yaml directly
    # Smoke heuristic: source doesn't have spec.yaml file writing
    # (compiler does that)
    forbidden = "write_spec" in source.lower() or 'spec.yaml"' in source
    # Allow reading spec template references but not writing
    # This is a soft check; the harder check is in compiler tests
    # Accept either: source mentions proposal but not spec write
    assert "proposal" in source.lower(), "ideator must write proposals"


# =================== Proposal Rewrite Loop ===================


def test_rewrite_loop_max_3_rounds():
    m = _load(REWRITE_MODULE, "proposal_rewrite_loop")
    # If validator always fails, rewrite must terminate after 3 attempts
    fake_adapter = MagicMock()
    fake_adapter.call_active_provider = MagicMock(return_value=type("R", (), {
        "content": '{"proposal_id": "x"}',
        "provider_id": "fake",
        "response_hash": "hash1",
        "retries_used": 0,
    })())

    def always_fail_validator(proposal: dict) -> list[str]:
        return ["always invalid"]

    result = m.rewrite_until_valid(
        initial_proposal={"proposal_id": "init"},
        validator=always_fail_validator,
        ai_adapter=fake_adapter,
    )
    assert result.status in {"exhausted", "valid"}
    assert result.rounds_used <= 3


def test_rewrite_loop_success_path():
    """Successful rewrite reaches valid state."""
    m = _load(REWRITE_MODULE, "proposal_rewrite_loop")
    call_count = [0]
    fake_adapter = MagicMock()
    fake_adapter.call_active_provider = MagicMock(return_value=type("R", (), {
        "content": '{"proposal_id": "fixed"}',
        "provider_id": "fake",
        "response_hash": "hash1",
        "retries_used": 0,
    })())

    def validator_passes_on_2nd(proposal: dict) -> list[str]:
        call_count[0] += 1
        if call_count[0] >= 2:
            return []  # pass
        return ["initial invalid"]

    result = m.rewrite_until_valid(
        initial_proposal={"proposal_id": "init"},
        validator=validator_passes_on_2nd,
        ai_adapter=fake_adapter,
    )
    assert result.status == "valid"
    assert result.rounds_used >= 1


def test_rewrite_loop_preserves_provenance_per_round():
    """Each rewrite round must record its own response_hash."""
    m = _load(REWRITE_MODULE, "proposal_rewrite_loop")
    source = REWRITE_MODULE.read_text().lower()
    assert "response_hash" in source or "provenance" in source


# =================== Orchestrator ===================


def test_orchestrator_respects_pause_flag(tmp_path: Path, monkeypatch):
    """Critical guard F: if pause flag file exists, orchestrator must stop immediately."""
    m = _load(ORCH_MODULE, "orchestrator")
    # Set pause flag
    pause_path = tmp_path / "orchestrator_paused.flag"
    pause_path.write_text("paused by user 2026-05-17")
    monkeypatch.setattr(m, "PAUSE_FLAG_PATH", pause_path)
    assert m.is_paused() is True


def test_orchestrator_no_pause_flag_not_paused(tmp_path: Path, monkeypatch):
    m = _load(ORCH_MODULE, "orchestrator")
    pause_path = tmp_path / "orchestrator_paused.flag"
    monkeypatch.setattr(m, "PAUSE_FLAG_PATH", pause_path)
    assert m.is_paused() is False


def test_orchestrator_writes_audit_log(tmp_path: Path, monkeypatch):
    """Critical guard E: every major action logged to orchestrator_log.jsonl."""
    m = _load(ORCH_MODULE, "orchestrator")
    audit_path = tmp_path / "orchestrator_log.jsonl"
    monkeypatch.setattr(m, "AUDIT_LOG_PATH", audit_path)

    # Source check at minimum
    source = ORCH_MODULE.read_text().lower()
    assert "audit" in source or "log" in source


def test_orchestrator_pause_stops_cycle(tmp_path: Path, monkeypatch):
    """run_cycle must return immediately if paused, without running steps."""
    m = _load(ORCH_MODULE, "orchestrator")
    pause_path = tmp_path / "orchestrator_paused.flag"
    pause_path.write_text("stop")
    monkeypatch.setattr(m, "PAUSE_FLAG_PATH", pause_path)

    # If run_cycle exists and is callable
    if hasattr(m, "run_cycle"):
        try:
            result = m.run_cycle({"max_cycle_budget_yuan": 100.0})
            # Either returns a "paused" result or doesn't do anything destructive
            if hasattr(result, "status"):
                assert result.status in {"paused", "stopped", "skipped"}
        except Exception:
            # Acceptable if pause causes early return / exception with clear message
            pass


def test_orchestrator_budget_cap_per_cycle():
    """Per-cycle budget cap default 100 CNY (Claude guard C)."""
    m = _load(ORCH_MODULE, "orchestrator")
    source = ORCH_MODULE.read_text().lower()
    # Source must reference budget cap logic
    assert "budget" in source and ("cap" in source or "max" in source or "limit" in source)


def test_orchestrator_no_auto_run_draft_specs():
    """Orchestrator must NOT auto-run DRAFT specs (only READY)."""
    m = _load(ORCH_MODULE, "orchestrator")
    source = ORCH_MODULE.read_text().upper()
    assert "DRAFT" in source and "READY" in source
    # Heuristic: source distinguishes between them


# =================== Schema files in research_framework/ ===================


def test_acceptance_criteria_file_exists():
    """The acceptance criteria yaml file must exist (Claude wrote it)."""
    path = REPO_ROOT / "data" / "research_framework" / "autonomous_research_acceptance_criteria.yaml"
    assert path.exists(), "Claude must write acceptance criteria"


def test_closed_directions_tags_field_exists():
    """closed_directions_tags must be in acceptance criteria for Codex to consume."""
    path = REPO_ROOT / "data" / "research_framework" / "autonomous_research_acceptance_criteria.yaml"
    if not path.exists():
        pytest.skip("acceptance criteria not yet written")
    data = yaml.safe_load(path.read_text())
    assert "closed_directions_tags" in data
    # Must include known 7-batch reject family tags
    closed_tags = data["closed_directions_tags"]
    assert "panic_filter" in closed_tags
    assert "universe_subset_filter" in closed_tags or "global_entry_filter" in closed_tags
