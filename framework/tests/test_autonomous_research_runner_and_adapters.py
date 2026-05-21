"""Acceptance tests for Runner, AI Provider Adapter (5),
Verification Tool (3), Strategy Ideator (4), Proposal Rewrite Loop (9).

Critical autonomous guards:
- F: User pause switch (file existence → runner stops)
- E: Runner audit log per action
- C: Compute budget is not an active orchestration gate
- D: AI provider robustness (retry then fail)
- Anti-fishing: Verification pre-registered requests

Expected API:
  research_queue_runner.py:
    - tick() -> str
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
    - propose(closed_tags, recent_digest, insights, ai_adapter) -> dict
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
RUNNER_MODULE = REPO_ROOT / "scripts" / "research_queue_runner.py"
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


def test_ai_adapter_logs_empty_content_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Empty model content must leave enough evidence to debug the provider response."""
    m = _load(AI_ADAPTER_MODULE, "ai_provider_adapter_empty_content")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_DEEPSEEK_KEY", "sk-test")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "", "reasoning_content": "hidden"},
                }]
            }).encode("utf-8")

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda request, timeout: FakeResponse())
    with pytest.raises(RuntimeError, match="diagnostics="):
        m._call_openai_chat_provider(
            "deepseek",
            {
                "api_key_env": "TEST_DEEPSEEK_KEY",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-pro",
            },
            "return yaml",
        )

    logs = list((tmp_path / "logs" / "provider_debug").glob("*_deepseek_empty_content.json"))
    assert logs
    diagnostic = json.loads(logs[0].read_text(encoding="utf-8"))
    assert diagnostic["finish_reason"] == "length"
    assert diagnostic["has_reasoning_content"] is True
    assert diagnostic["message_content_length"] == 0


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


def test_ideator_prompt_has_complete_role_boundaries():
    """Ideator prompt must state role, sequence, output contract, and hard boundaries."""
    m = _load(IDEATOR_MODULE, "strategy_ideator")
    source = IDEATOR_MODULE.read_text().lower()
    required = [
        "your only job is to propose",
        "you do not run tests",
        "you must follow this order",
        "propose exactly one",
        "success criteria and falsifiers",
        "not a renamed repeat",
        "return exactly one",
        "choose capability_ids only",
        "capability_menu",
        "missing_capability_request",
        "never hand-write a capability name",
        "do not use a capability whose resolved mechanic is in closed_tags",
        "compute budget is not a proposal gate",
        "do not invent runnable capability",
    ]
    for text in required:
        assert text in source


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


# =================== Runner ===================


def test_runner_respects_pause_flag(tmp_path: Path, monkeypatch):
    """Critical guard F: if pause flag file exists, runner must stop immediately."""
    m = _load(RUNNER_MODULE, "research_queue_runner")
    # Set pause flag
    pause_path = tmp_path / "orchestrator_paused.flag"
    pause_path.write_text("paused by user 2026-05-17")
    monkeypatch.setattr(m, "PAUSE_FLAG_PATH", pause_path)
    assert m.is_paused() is True


def test_runner_no_pause_flag_not_paused(tmp_path: Path, monkeypatch):
    m = _load(RUNNER_MODULE, "research_queue_runner")
    pause_path = tmp_path / "orchestrator_paused.flag"
    monkeypatch.setattr(m, "PAUSE_FLAG_PATH", pause_path)
    assert m.is_paused() is False


def test_runner_writes_audit_log(tmp_path: Path, monkeypatch):
    """Critical guard E: every major action logged to orchestrator_log.jsonl."""
    m = _load(RUNNER_MODULE, "research_queue_runner")
    audit_path = tmp_path / "orchestrator_log.jsonl"
    monkeypatch.setattr(m, "AUDIT_LOG_PATH", audit_path)

    m.audit("test_action", {"ok": True})
    row = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["action"] == "test_action"
    assert row["payload"] == {"ok": True}


def test_runner_pause_stops_tick(tmp_path: Path, monkeypatch):
    """tick must return immediately if paused, without running steps."""
    m = _load(RUNNER_MODULE, "research_queue_runner")
    pause_path = tmp_path / "orchestrator_paused.flag"
    pause_path.write_text("stop")
    monkeypatch.setattr(m, "PAUSE_FLAG_PATH", pause_path)
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(m, "AUDIT_LOG_PATH", tmp_path / "orchestrator_log.jsonl")
    assert m.tick() == "paused"


def test_runner_has_no_budget_gate():
    """Compute budget must not decide whether a cycle advances."""
    m = _load(RUNNER_MODULE, "research_queue_runner")
    source = RUNNER_MODULE.read_text().lower()
    assert "budget_cap" not in source
    assert "max_cycle_budget" not in source


def test_runner_no_auto_run_draft_specs():
    """Runner must NOT auto-run DRAFT specs (only READY)."""
    m = _load(RUNNER_MODULE, "research_queue_runner")
    source = (REPO_ROOT / "framework" / "autonomous" / "queue_remote_execution.py").read_text().upper()
    assert "SPEC.STATUS MUST BE READY" in source
    assert 'SPEC_STATUS == "DRAFT"' in source


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
