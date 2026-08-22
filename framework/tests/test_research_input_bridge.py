from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import json
import yaml

from framework.evaluation.pending_research_input import PendingResearchInputStore
from framework.autonomous.ideation_cycle import IdeationCycle, normalize_proposal_shape
from framework.autonomous.paths import ResearchPaths
from framework.autonomous.queue_ideation import QueueIdeationService
from framework.evaluation.research_feedback import (
    ClaudeReview,
    ExplorationPacket,
    ResearchQuestion,
    ReviewVerdict,
    ScoreVector,
    accept_claude_review,
    validate_research_input_proposal,
)


def make_input():
    question = ResearchQuestion(
        "question_1", "Can execution costs preserve the observed edge?", "realizability",
        ("edge exists",), ("execution_mechanism",), ("parameter_only_tuning",),
        ("cost_on",), ("cost_on <= zero",), ("evidence_1",),
    )
    packet = ExplorationPacket(1, "run_1", "1", (question,), ("evidence_1",),
                               {"question_1": ScoreVector(3, 2, 2, 2, 3, 3)})
    review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, "question_1", (), ("evidence_1",),
                          ("include slippage",), ({"type": "required_test", "value": "tail_cost"},),
                          ("tail_cost",))
    value, errors = accept_claude_review(packet, review)
    assert not errors and value is not None
    return value


def valid_proposal(research_input, proposal_id: str):
    constraints = research_input.applied_constraints
    return {
        "proposal_id": proposal_id,
        "family": "cost_execution_probe",
        "mechanics": ["cost_adjusted_execution"],
        "hypothesis": "Execution mechanism can preserve the edge under conservative cost assumptions.",
        "required_changes": list(constraints["required_changes"]),
        "test_design": {
            "changed_dimensions": list(constraints["must_change"]),
            "required_evidence": list(constraints["required_evidence"]),
            "required_tests": list(research_input.required_tests),
            "accepted_question_id": research_input.question.question_id,
            "accepted_question_text": research_input.question.question,
            "constraint_bindings": {"execution_mechanism": ["mechanics", "test_design.changed_dimensions"]},
        },
        "research_feedback": {
            "source_review_id": research_input.source_review_id,
            "source_run_id": research_input.source_run_id,
            "plan_version": research_input.plan_version,
            "proposal_id": proposal_id,
            "accepted_question_id": research_input.question.question_id,
            "accepted_question_text": research_input.question.question,
            "applied_constraints": constraints,
            "required_tests": list(research_input.required_tests),
        },
    }


class TestResearchInputBridge(unittest.TestCase):
    def test_pass_builds_v2_and_preserves_constraints(self):
        research_input = make_input()
        self.assertEqual(research_input.schema_version, 2)
        self.assertEqual(research_input.applied_constraints["must_change"], ["execution_mechanism"])
        self.assertEqual(research_input.applied_constraints["required_changes"], ["include slippage"])
        self.assertEqual(research_input.applied_constraints["review_constraints"], [{"type": "required_test", "value": "tail_cost"}])

    def test_validator_is_bidirectional_and_forbidden_safe(self):
        research_input = make_input()
        bindings = {"bindings": {"execution_mechanism": ["mechanics", "test_design.changed_dimensions"]}}
        proposal = valid_proposal(research_input, "reserved_1")
        self.assertEqual([], validate_research_input_proposal(proposal, research_input, "1", bindings, expected_proposal_id="reserved_1"))
        proposal["test_design"]["required_tests"].append("unreviewed")
        self.assertTrue(validate_research_input_proposal(proposal, research_input, "1", bindings, expected_proposal_id="reserved_1"))
        proposal = valid_proposal(research_input, "reserved_1")
        proposal["hypothesis"] = "This is parameter only tuning."
        self.assertTrue(validate_research_input_proposal(proposal, research_input, "1", bindings, expected_proposal_id="reserved_1"))

    def test_store_claim_nonce_and_replay_rules(self):
        research_input = make_input()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PendingResearchInputStore(root / "pending.yaml", root / "history.jsonl")
            self.assertEqual(store.admit(research_input, "1", root / "runs", 900), (True, "ADMITTED"))
            claim, status = store.claim("1", root / "runs", 900)
            self.assertEqual(status, "CLAIMED")
            assert claim is not None
            self.assertTrue((root / "runs" / ".research_input_reservations" / f"{claim['proposal_id']}.json").exists())
            self.assertEqual(store.claim("1", root / "runs", 900)[1], "RESEARCH_INPUT_CLAIMED")
            self.assertFalse(store.release_claim("wrong", root / "runs", "wrong nonce"))
            self.assertTrue(store.release_claim(claim["claim_nonce"], root / "runs", "invalid feedback"))
            self.assertFalse((root / "runs" / ".research_input_reservations" / f"{claim['proposal_id']}.json").exists())
            # A released input may be deliberately re-admitted; consumed never may.
            self.assertEqual(store.admit(research_input, "1", root / "runs", 900), (True, "ADMITTED"))

    def test_consumed_input_cannot_be_re_admitted(self):
        research_input = make_input()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PendingResearchInputStore(root / "pending.yaml", root / "history.jsonl")
            self.assertEqual(store.admit(research_input, "1", root / "runs", 900), (True, "ADMITTED"))
            claim, status = store.claim("1", root / "runs", 900)
            self.assertEqual(status, "CLAIMED")
            assert claim is not None
            self.assertTrue(store.consume(claim["claim_nonce"], root / "runs", root / "runs" / "proposal.yaml"))
            self.assertEqual(
                store.admit(research_input, "1", root / "runs", 900),
                (False, "RESEARCH_INPUT_REPLAY_REJECTED"),
            )

    def test_recover_never_treats_missing_proposal_path_as_current_directory(self):
        research_input = make_input()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PendingResearchInputStore(root / "pending.yaml", root / "history.jsonl")
            self.assertEqual(store.admit(research_input, "1", root / "runs", 900), (True, "ADMITTED"))
            claim, status = store.claim("1", root / "runs", 900)
            self.assertEqual(status, "CLAIMED")
            assert claim is not None
            slot = yaml.safe_load((root / "pending.yaml").read_text(encoding="utf-8"))
            slot.pop("proposal_path")
            (root / "pending.yaml").write_text(yaml.safe_dump(slot), encoding="utf-8")
            self.assertEqual(store.recover("1", root / "runs", 900, lambda _slot: True), "RESEARCH_INPUT_CLAIMED")
            self.assertTrue((root / "pending.yaml").exists())

    def test_ttl_recovery_releases_slot_but_keeps_tombstone_for_slow_worker(self):
        research_input = make_input()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PendingResearchInputStore(root / "pending.yaml", root / "history.jsonl")
            self.assertEqual(store.admit(research_input, "1", root / "runs", 1), (True, "ADMITTED"))
            claim, status = store.claim("1", root / "runs", 1)
            self.assertEqual(status, "CLAIMED")
            assert claim is not None
            slot = yaml.safe_load((root / "pending.yaml").read_text(encoding="utf-8"))
            slot["claimed_at"] = 0
            (root / "pending.yaml").write_text(yaml.safe_dump(slot), encoding="utf-8")
            self.assertEqual(store.recover("1", root / "runs", 1, lambda _slot: False), "RELEASED_EXPIRED_CLAIM")
            self.assertFalse((root / "pending.yaml").exists())
            self.assertTrue((root / "runs" / ".research_input_reservations" / f"{claim['proposal_id']}.json").exists())

    def test_claim_stale_plan_moves_input_to_auditable_terminal_state(self):
        research_input = make_input()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PendingResearchInputStore(root / "pending.yaml", root / "history.jsonl")
            self.assertEqual(store.admit(research_input, "1", root / "runs", 900), (True, "ADMITTED"))
            claim, status = store.claim("2", root / "runs", 900)
            self.assertIsNone(claim)
            self.assertEqual(status, "RESEARCH_INPUT_STALE_PLAN_VERSION")
            self.assertFalse((root / "pending.yaml").exists())
            self.assertIn('"event": "stale_plan_version"', (root / "history.jsonl").read_text(encoding="utf-8"))

    def test_shape_normalization_keeps_conditional_feedback_fields_as_lists(self):
        proposal = normalize_proposal_shape({
            "required_changes": "add slippage",
            "research_feedback": {"required_tests": "tail_cost"},
            "test_design": {
                "changed_dimensions": "execution_mechanism",
                "required_evidence": "cost_on",
                "required_tests": "tail_cost",
            },
        })
        self.assertEqual(proposal["required_changes"], ["add slippage"])
        self.assertEqual(proposal["research_feedback"]["required_tests"], ["tail_cost"])
        self.assertEqual(proposal["test_design"]["changed_dimensions"], ["execution_mechanism"])

    def test_pending_input_dry_run_is_non_mutating_and_never_calls_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ResearchPaths.from_repo_root(root)
            paths.research_framework_dir.mkdir(parents=True)
            paths.strategy_ideator_config.write_text("research_input_plan_version: '1'\n", encoding="utf-8")
            paths.pending_research_input.write_text("state: pending\n", encoding="utf-8")

            class NeverCall:
                def call_active_provider(self, *_args, **_kwargs):
                    raise AssertionError("dry-run must not call an AI provider")

            payload = IdeationCycle(paths=paths, ai_adapter=NeverCall()).run_once(
                output_root=root / "data", dry_run=True,
            )
            self.assertEqual(payload["status"], "RESEARCH_INPUT_DRY_RUN_UNSUPPORTED")
            self.assertEqual(paths.pending_research_input.read_text(encoding="utf-8"), "state: pending\n")

    def test_enabled_lifecycle_without_policy_fails_before_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ResearchPaths.from_repo_root(root)
            paths.research_framework_dir.mkdir(parents=True)
            paths.strategy_ideator_config.write_text("research_input_plan_version: '1'\nresearch_lifecycle: {mode: shadow}\n", encoding="utf-8")
            payload = IdeationCycle(paths=paths, ai_adapter=object()).run_once(output_root=root / "data")
            self.assertEqual("RESEARCH_LIFECYCLE_POLICY_MISSING", payload["status"])

    def test_queue_routes_research_input_failure_without_draft_suppression(self):
        events: list[tuple[str, dict | None]] = []
        service = QueueIdeationService(
            repo_root=Path("."), load_state=lambda: {"queue": []}, save_state=lambda _state: None,
            write_status=lambda name, payload: events.append((name, payload)),
            audit=lambda name, payload: events.append((name, payload)), log=lambda _msg: None,
            mark_history=lambda *_args: None, rel=lambda path: str(path), now_iso=lambda: "now",
            ideation_env=lambda: {},
        )
        result = service._handle_failed_ideation(43, '{"status":"RESEARCH_INPUT_CLAIMED","reason":"lease"}')
        self.assertEqual(result, "ideation_research_input_claimed")
        self.assertTrue(all(name == "ideation_research_input_claimed" for name, _ in events))

    def test_invalid_free_proposal_never_creates_a_proposal_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ResearchPaths.from_repo_root(root)
            paths.research_framework_dir.mkdir(parents=True)
            paths.strategy_ideator_config.write_text("research_input_plan_version: '1'\n", encoding="utf-8")

            class InvalidResponse:
                content = '{"proposal_id":"malformed"}'
                provider_id, response_hash, retries_used = "test", "hash", 0

            class InvalidAdapter:
                def call_active_provider(self, *_args, **_kwargs):
                    return InvalidResponse()

            payload = IdeationCycle(paths=paths, ai_adapter=InvalidAdapter()).run_once(output_root=root / "data")
            self.assertEqual(payload["status"], "PROPOSAL_INVALID_STRUCTURE")
            self.assertFalse((root / "data" / "malformed").exists())

    def test_pending_pass_bypasses_external_draft_wait_then_consumes_input(self):
        research_input = make_input()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ResearchPaths.from_repo_root(root)
            rf = paths.research_framework_dir
            rf.mkdir(parents=True)
            paths.strategy_ideator_config.write_text(yaml.safe_dump({
                "research_input_plan_version": "1", "research_input_claim_ttl_seconds": 900,
            }), encoding="utf-8")
            paths.research_input_constraint_bindings.write_text(yaml.safe_dump({
                "schema_version": 1, "research_input_plan_version": "1",
                "bindings": {"execution_mechanism": ["mechanics", "test_design.changed_dimensions"]},
            }), encoding="utf-8")
            paths.executor_registry.write_text(yaml.safe_dump({
                "schema_version": 1,
                "capabilities": {"C001": {"mechanic": "cost_adjusted_execution", "label": "cost test"}},
                "executors": [{
                    "id": "cost_executor", "version": 1, "script_path": "scripts/cost.py",
                    "can_test": ["cost_adjusted_execution"], "can_test_capability_ids": ["C001"],
                    "cannot_test": [], "cannot_test_capability_ids": [], "required_data": [],
                    "required_config_fields": [], "artifacts_produced": ["summary.json"],
                    "command_template": ["python3", "scripts/cost.py"],
                    "budget_estimate": {"sig_minutes": 0, "spot_minutes": 1, "local_minutes": 0},
                    "vm_local_limits": {"vm_required": True, "local_allowed": False}, "obsolescence_date": None,
                }],
            }), encoding="utf-8")
            paths.recent_results_digest.write_text("schema_version: 1\nruns: []\n", encoding="utf-8")
            paths.research_queue.write_text("enabled: true\nqueue: []\n", encoding="utf-8")
            paths.evidence_tool_registry.write_text("schema_version: 1\ntools: {}\n", encoding="utf-8")
            store = PendingResearchInputStore(paths.pending_research_input, paths.research_input_admission_history)
            self.assertEqual(store.admit(research_input, "1", root / "data", 900), (True, "ADMITTED"))

            class FakeResponse:
                provider_id, response_hash, retries_used = "test", "hash", 0

                def __init__(self, content):
                    self.content = content

            class FakeAdapter:
                def call_active_provider(self, prompt, schema):
                    self.prompt = prompt
                    proposal = valid_proposal(research_input, "research_input_question_1_run_1")
                    proposal.update({
                        "strategy_id": "strategy_1", "source_insight": "reviewed cost evidence",
                        "expected_improvement": "cost-on survives", "capability_ids": ["C001"],
                        "required_executor": "cost_executor", "required_data": [], "required_data_fields": {},
                        "success_criteria": {"train": True},
                        "falsifiers": {"train": True, "validate": True, "test": True},
                        "risk": "cost risk", "why_not_repeated_failure": "reviewed question",
                        "related_prior_runs": [], "implementation_assumption": "registered executor",
                    })
                    return FakeResponse(json.dumps(proposal))

            with (
                patch("framework.autonomous.ideation_cycle.record_framework_change", lambda **_kwargs: "event"),
                patch(
                    "framework.autonomous.ideation_cycle.find_pending_tool_draft",
                    return_value={"package_status": "draft_tool_code"},
                ),
            ):
                payload = IdeationCycle(paths=paths, ai_adapter=FakeAdapter()).run_once(output_root=root / "data")
            self.assertIn(payload["status"], {"READY", "DRAFT", "SKIPPED_BY_DELTA_GATE", "WATCHED_BY_DELTA_GATE"})
            self.assertTrue(Path(payload["proposal_path"]).exists())
            self.assertIsNone(store.active(root / "data", 900))


if __name__ == "__main__":
    unittest.main()
