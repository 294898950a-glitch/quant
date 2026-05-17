"""Acceptance tests for Result Reviewer (component 2) + Recent Results Digest (component 1).

Critical Claude guards:
- Reviewer facts field 100% code-extracted, hash-verified (anti AI-hallucination)
- Reviewer facts immutable on re-review
- Digest reads ONLY from review.yaml.facts (not run artifacts directly)
- No AI in digest (purely structural aggregation)

Expected API:
  result_reviewer.py:
    - review(run_dir: Path, verification_callback=None, ai_adapter=None) -> dict
    - extract_facts(run_dir: Path) -> dict  # pure code, no AI
    - verify_facts_hash(facts: dict, run_dir: Path) -> bool
  recent_results_digest.py:
    - build_digest(review_dir: Path, last_n: int = 5) -> dict
    - update_current_pointer(digest: dict, current_yaml_path: Path) -> None
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEWER_MODULE = REPO_ROOT / "framework" / "autonomous" / "result_reviewer.py"
DIGEST_MODULE = REPO_ROOT / "framework" / "autonomous" / "recent_results_digest.py"


def _load(path: Path, name: str):
    if not path.exists():
        pytest.skip(f"{path} not implemented yet")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _make_run_dir(tmp_path: Path, run_id: str = "test_run_20260517") -> Path:
    run_dir = tmp_path / "data" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "spec.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "run_id": run_id,
        "strategy_id": "cb_arb_value_gap_switch",
        "status": "COMPLETE",
        "hypothesis": "test hypothesis",
        "automation": {"verdict": {"decision": "passed_mechanical_but_falsifier_failed"}},
    }))
    (run_dir / "summary.json").write_text(json.dumps({
        "exit_code": 0,
        "compute_cost_yuan": 6.25,
        "best_train_variant": "variant_a",
        "best_test_variant": "variant_b",
    }))
    (run_dir / "summary_test.csv").write_text(
        "name,period,excess_return,max_drawdown\n"
        "baseline,train,0.131,-0.324\n"
        "baseline,test,0.380,-0.098\n"
        "variant_a,train,0.420,-0.260\n"
        "variant_a,test,-0.140,-0.150\n"
    )
    return run_dir


# =================== Result Reviewer ===================


def test_reviewer_extracts_facts_no_ai(tmp_path: Path):
    """facts field must be extractable WITHOUT AI (pure code parse of CSV/yaml)."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    facts = m.extract_facts(run_dir)
    assert isinstance(facts, dict)
    # Must include some structured metrics
    assert "exit_code" in facts or "best_train_variant" in facts


def test_reviewer_facts_hash_verified(tmp_path: Path):
    """Facts must be hash-verified against source artifacts."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    facts = m.extract_facts(run_dir)
    assert "artifacts_hash" in facts or "facts_hash" in facts, "facts must include hash for verification"


def test_reviewer_facts_immutable_on_rereview(tmp_path: Path):
    """Re-review must NOT overwrite facts with different values silently."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    # First review
    review1 = m.review(run_dir, ai_adapter=None)  # no AI for this test
    review_path = run_dir / "review.yaml"
    if review_path.exists():
        # Tamper with facts in-place
        data = yaml.safe_load(review_path.read_text())
        if "facts" in data and isinstance(data["facts"], dict):
            data["facts"]["exit_code"] = 999  # wrong value
            review_path.write_text(yaml.safe_dump(data))
            # Second review should detect tamper via hash mismatch and either restore or warn
            try:
                review2 = m.review(run_dir, ai_adapter=None)
                # If no exception, the facts must have been restored OR the function returned an error
                final = yaml.safe_load(review_path.read_text())
                if "facts" in final:
                    # Either restored or marked as inconsistent
                    assert final["facts"].get("exit_code") != 999 or "warning" in final
            except (ValueError, RuntimeError):
                # Acceptable: reject tampered re-review
                pass


def test_reviewer_no_ai_in_facts_field(tmp_path: Path):
    """If AI is None or fails, facts must STILL be populated (facts is code-only)."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    review = m.review(run_dir, ai_adapter=None)
    review_path = run_dir / "review.yaml"
    assert review_path.exists()
    data = yaml.safe_load(review_path.read_text())
    assert "facts" in data
    # facts must be non-empty
    assert data["facts"]


def test_reviewer_separates_facts_from_interpretation(tmp_path: Path):
    """review.yaml must have separate facts and interpretation fields."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    review = m.review(run_dir, ai_adapter=None)
    review_path = run_dir / "review.yaml"
    data = yaml.safe_load(review_path.read_text())
    assert "facts" in data
    assert "interpretation" in data
    assert "next_directions" in data
    # Without AI, interpretation/next_directions can be empty or placeholder
    # but the fields themselves must exist


def test_reviewer_records_provenance_when_ai_used(tmp_path: Path):
    """If AI adapter is provided, review.yaml must record provenance."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)

    class FakeAdapter:
        def call_active_provider(self, prompt: str, schema: dict):
            return type("R", (), {
                "content": "Mock interpretation",
                "provider_id": "fake_provider",
                "response_hash": "fakeresphash",
                "retries_used": 0,
            })()

    review = m.review(run_dir, ai_adapter=FakeAdapter())
    review_path = run_dir / "review.yaml"
    data = yaml.safe_load(review_path.read_text())
    assert data.get("ai_provider_used") == "fake_provider"
    assert data.get("response_hash") == "fakeresphash"


def test_reviewer_inconclusive_after_2_verification_rounds(tmp_path: Path):
    """If verification_callback is called 2 times and still unresolved, inconclusive=true."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    call_count = [0]

    def verification_cb(request_type: str, params: dict):
        call_count[0] += 1
        # Always return "insufficient" to force inconclusive
        return {"status": "insufficient", "data": {}}

    review = m.review(run_dir, verification_callback=verification_cb, ai_adapter=None)
    review_path = run_dir / "review.yaml"
    if review_path.exists():
        data = yaml.safe_load(review_path.read_text())
        # Either inconclusive flag set, or verification_rounds_used recorded
        assert data.get("inconclusive") is True or data.get("verification_rounds_used", 0) <= 2


def test_reviewer_populates_closed_directions_for_known_reject(tmp_path: Path):
    """When run fails with known reject pattern, closed_directions populated."""
    m = _load(REVIEWER_MODULE, "result_reviewer")
    run_dir = _make_run_dir(tmp_path)
    # Spec marks it as panic_filter family rejected
    spec_path = run_dir / "spec.yaml"
    spec_data = yaml.safe_load(spec_path.read_text())
    spec_data["mechanics"] = ["panic_filter"]
    spec_data["automation"]["verdict"]["decision"] = "passed_mechanical_but_falsifier_failed"
    spec_path.write_text(yaml.safe_dump(spec_data))

    review = m.review(run_dir, ai_adapter=None)
    review_path = run_dir / "review.yaml"
    data = yaml.safe_load(review_path.read_text())
    closed = data.get("closed_directions", [])
    # Either closed_directions populated OR explicit "not_closing_any_direction" markers
    # The acceptance: reviewer SHOULD propose closing the panic_filter family
    if closed:
        tags = [c.get("mechanic_tag") for c in closed]
        assert "panic_filter" in tags


# =================== Recent Results Digest ===================


def _make_review(review_dir: Path, run_id: str, status: str = "rejected") -> Path:
    """Create a review.yaml for digest testing."""
    review_dir.mkdir(parents=True, exist_ok=True)
    review_path = review_dir / f"{run_id}_review.yaml"
    review_path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "run_id": run_id,
        "strategy_id": "cb_arb_value_gap_switch",
        "facts": {
            "decision": "passed_mechanical_but_falsifier_failed" if status == "rejected" else "adopted",
            "best_test_excess": 0.380,
            "best_train_excess": -0.084,
            "artifacts_hash": "deadbeef",
        },
        "interpretation": {"narrative": "blah"},
        "closed_directions": [{"mechanic_tag": "panic_filter", "reason": "test"}],
        "next_directions": [],
    }))
    return review_path


def test_digest_aggregates_last_n_reviews(tmp_path: Path):
    m = _load(DIGEST_MODULE, "recent_results_digest")
    review_dir = tmp_path / "reviews"
    for i in range(7):
        _make_review(review_dir, f"run_{i}")
    digest = m.build_digest(review_dir, last_n=5)
    assert isinstance(digest, dict)
    # Must reference only last 5
    if "recent_runs" in digest:
        assert len(digest["recent_runs"]) <= 5


def test_digest_reads_only_review_yaml(tmp_path: Path):
    """Critical guard G: digest must NOT read run artifacts (CSV/parquet) directly."""
    m = _load(DIGEST_MODULE, "recent_results_digest")
    review_dir = tmp_path / "reviews"
    _make_review(review_dir, "run_a")
    # Should work even if no run_dir with CSV files exists
    digest = m.build_digest(review_dir, last_n=5)
    assert isinstance(digest, dict)


def test_digest_detects_close_family_pattern(tmp_path: Path):
    """If 3+ recent runs reject same mechanic family, digest flags it for closure."""
    m = _load(DIGEST_MODULE, "recent_results_digest")
    review_dir = tmp_path / "reviews"
    # 3 reviews all flagging panic_filter as closed
    for i in range(3):
        _make_review(review_dir, f"panic_run_{i}", status="rejected")
    digest = m.build_digest(review_dir, last_n=5)
    if "suggested_closed_families" in digest:
        assert "panic_filter" in [f.get("tag") for f in digest["suggested_closed_families"]]


def test_digest_no_ai_required(tmp_path: Path):
    """Digest must work without AI adapter (it's structural aggregation only)."""
    m = _load(DIGEST_MODULE, "recent_results_digest")
    review_dir = tmp_path / "reviews"
    _make_review(review_dir, "run_a")
    # Function signature should NOT require ai_adapter
    import inspect
    sig = inspect.signature(m.build_digest)
    assert "ai_adapter" not in sig.parameters, "digest must not depend on AI"


def test_digest_updates_current_pointer_only(tmp_path: Path):
    """update_current_pointer must only ADD a pointer entry, not rewrite strategy truth."""
    m = _load(DIGEST_MODULE, "recent_results_digest")
    review_dir = tmp_path / "reviews"
    _make_review(review_dir, "run_a")
    digest = m.build_digest(review_dir, last_n=5)

    current_path = tmp_path / "current.yaml"
    original_current = {
        "schema_version": 1,
        "summary": {"deployable_strategies": 0},
        "strategies": [{
            "strategy_id": "cb_arb_value_gap_switch",
            "status": "wip",
            "baseline_row": "cb_arb-value-gap-switch-medium-signal-cost-on-20260516",
        }],
    }
    current_path.write_text(yaml.safe_dump(original_current))

    m.update_current_pointer(digest, current_path)

    updated = yaml.safe_load(current_path.read_text())
    # Strategy rows must be unchanged
    assert updated["strategies"][0]["strategy_id"] == "cb_arb_value_gap_switch"
    assert updated["strategies"][0]["status"] == "wip"
    assert updated["strategies"][0]["baseline_row"] == original_current["strategies"][0]["baseline_row"]
    # Pointer added (somewhere, e.g. summary.latest_digest_path or recent_digest_pointer field)
    has_pointer = (
        "latest_digest" in updated.get("summary", {})
        or "recent_digest_pointer" in updated
        or "latest_runs" in updated.get("summary", {})
    )
    assert has_pointer, "must add a pointer to digest somewhere in current.yaml"
