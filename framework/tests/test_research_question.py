from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from io import StringIO
from unittest.mock import patch

from framework.evaluation.research_question import QuestionPolicy, ResearchQuestionLedger, derive_state
from framework.evaluation.lifecycle_cutover import compare_window
from framework.evaluation.research_question_shadow import ShadowAdmissionService, admission_operation_id, question_identity
from framework.evaluation.pending_research_input import PendingResearchInputStore
from framework.tests.test_research_input_bridge import make_input


POLICY = '''policy_version: test-v1
revision_budget: {max_per_lineage: 2}
reframe_budget: {max_per_lineage: 2}
holdout: {required_for_validated_label: true, one_final_run_per_question: true}
'''


def event(kind: str, **extra):
    return {"event_type": kind, "question_id": "q1", "lineage_id": "l1", **extra}


class TestResearchQuestionLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.policy_path = root / "policy.yaml"
        self.policy_path.write_text(POLICY, encoding="utf-8")
        self.ledger = ResearchQuestionLedger(root / "events.jsonl", self.policy_path)
        self.policy = QuestionPolicy.load(self.policy_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_is_idempotent_without_wall_clock_identity(self):
        row = event("question_admission", policy_version="test-v1")
        self.ledger.append(row)
        self.ledger.append(row)
        self.assertEqual(1, len(self.ledger.events()))

    def test_budget_accumulates_across_reframed_questions(self):
        first = event("proposal_revision", revision_id="r1", validity_outcome="valid")
        second = {**event("proposal_revision", revision_id="r2", validity_outcome="valid"), "question_id": "q2"}
        reframe = {**event("question_admission", policy_version="test-v1"), "question_id": "q2", "parent_question_id": "q1"}
        self.assertEqual("REFRAME_REQUIRED", derive_state([first], [first, reframe, second], self.policy).value)

    def test_exposed_holdout_is_not_waiting(self):
        question = [event("holdout_reservation", reservation_id="h1", reservation_operation_id="op1"), event("holdout_exposure", reservation_id="h1", reason="leak")]
        self.assertEqual("REQUIRES_NEW_HOLDOUT", derive_state(question, question, self.policy).value)

    def test_validated_closure_requires_final_run_reference(self):
        with self.assertRaisesRegex(ValueError, "holdout_final_run_id"):
            self.ledger.append(event("closure", closure_reason_type="evidence_backed", closure_claim="validated"))

    def test_first_closure_has_terminal_projection_priority(self):
        question = [event("closure", closure_reason_type="policy_budget", closure_claim="not_validated")]
        self.assertEqual("CLOSED_POLICY_BUDGET", derive_state(question, question, self.policy).value)

    def test_both_lineage_budgets_exhausted_requires_closure(self):
        revisions = [event("proposal_revision", revision_id="r1", validity_outcome="valid"), {**event("proposal_revision", revision_id="r2", validity_outcome="valid"), "question_id": "q2"}]
        reframes = [{**event("question_admission", policy_version="test-v1"), "question_id": "q2", "parent_question_id": "q1"}, {**event("question_admission", policy_version="test-v1"), "question_id": "q3", "parent_question_id": "q2"}]
        self.assertEqual("POLICY_BUDGET_CLOSURE_REQUIRED", derive_state(revisions[:1], revisions + reframes, self.policy).value)

    def test_terminal_closure_rejects_later_facts_and_returns_first_closure(self):
        closure = self.ledger.append(event("closure", closure_reason_type="policy_budget", closure_claim="not_validated"))
        self.assertEqual(closure, self.ledger.append(event("closure", closure_reason_type="evidence_backed", closure_claim="not_validated")))
        with self.assertRaisesRegex(ValueError, "terminal closure"):
            self.ledger.append(event("proposal_revision", revision_id="late", validity_outcome="valid"))

    def test_validated_closure_must_reference_recorded_final_run(self):
        self.ledger.append(event("holdout_final_run", holdout_final_run_id="h1"))
        with self.assertRaisesRegex(ValueError, "missing holdout_final_run"):
            self.ledger.append(event("closure", closure_reason_type="evidence_backed", closure_claim="validated", holdout_final_run_id="other"))

    def test_zero_reframe_budget_goes_to_policy_closure_required(self):
        zero = QuestionPolicy("zero", 1, 0, True)
        revision = event("proposal_revision", revision_id="r1", validity_outcome="valid")
        self.assertEqual("POLICY_BUDGET_CLOSURE_REQUIRED", derive_state([revision], [revision], zero).value)

    def test_shadow_reconcile_admits_only_after_pending_success_and_is_idempotent(self):
        item = make_input()
        pending = PendingResearchInputStore(Path(self.tmp.name) / "pending.yaml", Path(self.tmp.name) / "history.jsonl")
        shadow = ShadowAdmissionService(self.ledger, pending, Path(self.tmp.name) / "runs", 900)
        operation = shadow.record_attempt(item)
        self.assertEqual(admission_operation_id(item), operation)
        self.assertEqual(0, shadow.reconcile())
        self.assertEqual((True, "ADMITTED"), pending.admit(item, "1", Path(self.tmp.name) / "runs", 900, operation))
        self.assertEqual(1, shadow.reconcile())
        self.assertEqual(1, shadow.reconcile())
        kinds = [row["event_type"] for row in self.ledger.events()]
        self.assertEqual(1, kinds.count("question_admission"))

    def test_shadow_attempt_for_occupied_slot_never_becomes_admission(self):
        item = make_input()
        blocked = replace(item, source_review_id="review_blocked")
        pending = PendingResearchInputStore(Path(self.tmp.name) / "pending.yaml", Path(self.tmp.name) / "history.jsonl")
        self.assertEqual((True, "ADMITTED"), pending.admit(item, "1", Path(self.tmp.name) / "runs", 900))
        shadow = ShadowAdmissionService(self.ledger, pending, Path(self.tmp.name) / "runs", 900)
        blocked_operation = shadow.record_attempt(blocked)
        self.assertEqual((False, "RESEARCH_INPUT_SLOT_OCCUPIED"), pending.admit(blocked, "1", Path(self.tmp.name) / "runs", 900, blocked_operation))
        self.assertEqual(1, shadow.reconcile())
        self.assertFalse(any(row.get("event_type") == "question_admission" and row.get("admission_operation_id") == blocked_operation for row in self.ledger.events()))

    def test_admission_cli_fails_closed_when_enabled_policy_is_missing(self):
        from scripts import admit_research_input as command
        root = Path(self.tmp.name)
        framework_dir = root / "data" / "research_framework"
        framework_dir.mkdir(parents=True)
        for mode in ("shadow", "compare", "authoritative"):
            (framework_dir / "strategy_ideator.yaml").write_text(f"research_lifecycle: {{mode: {mode}}}\n", encoding="utf-8")
            stream = StringIO()
            with patch.object(command, "REPO_ROOT", root), patch.object(command, "require_ticket"), \
                 patch("sys.argv", ["admit_research_input.py", "--packet", str(root / "packet.yaml"), "--review", str(root / "review.yaml")]), \
                 patch("sys.stdout", stream):
                self.assertEqual(1, command.main())
            self.assertIn("RESEARCH_LIFECYCLE_POLICY_MISSING", stream.getvalue())

    def test_revision_compile_outcome_controls_executability_projection(self):
        self.policy_path.write_text(POLICY.replace("max_per_lineage: 2", "max_per_lineage: 3", 1), encoding="utf-8")
        item = make_input()
        pending = PendingResearchInputStore(Path(self.tmp.name) / "pending.yaml", Path(self.tmp.name) / "history.jsonl")
        shadow = ShadowAdmissionService(self.ledger, pending, Path(self.tmp.name) / "runs", 900)
        proposal = {"proposal_id": "p1", "mechanics": ["execution"], "test_design": {"required_evidence": ["cost_on"]}, "falsifiers": {"test": "no edge"}}
        shadow.record_revision(item, proposal, compile_outcome="DRAFT")
        question_id = question_identity(item)[0]
        self.assertEqual("IN_PROPOSAL_REVISION", self.ledger.state(question_id).value)
        proposal["proposal_id"] = "p2"
        shadow.record_revision(item, proposal, compile_outcome="READY")
        self.assertEqual("AWAITING_EXPERIMENT_RUN", self.ledger.state(question_id).value)

    def test_run_evidence_requires_existing_admission_linkage(self):
        from framework.autonomous import run_recorder
        root = Path(self.tmp.name)
        rf = root / "data" / "research_framework"
        rf.mkdir(parents=True)
        (rf / "strategy_ideator.yaml").write_text("research_lifecycle: {mode: shadow}\n", encoding="utf-8")
        (rf / "research_question_policy.yaml").write_text(POLICY, encoding="utf-8")
        manifest, output = root / "manifest.yaml", root / "output"
        manifest.write_text("schema_version: 1\n", encoding="utf-8"); output.mkdir()
        spec = {"run_id": "run1", "research_lifecycle": {"question_id": "q1", "lineage_id": "l1", "revision_id": "p1"}}
        with patch.object(run_recorder, "REPO_ROOT", root):
            run_recorder.record_lifecycle_evidence(spec, manifest, output)
        ledger = ResearchQuestionLedger(rf / "research_question_events.jsonl", rf / "research_question_policy.yaml")
        self.assertEqual([], ledger.events())
        ledger.append(event("question_admission", policy_version="test-v1"))
        with patch.object(run_recorder, "REPO_ROOT", root):
            run_recorder.record_lifecycle_evidence(spec, manifest, output)
        self.assertEqual("evidence_run", ledger.events()[-1]["event_type"])

    def test_compare_diff_is_idempotent_and_cutover_needs_consecutive_zeroes(self):
        from framework.autonomous import run_recorder
        root = Path(self.tmp.name); rf = root / "data" / "research_framework"; rf.mkdir(parents=True)
        (rf / "strategy_ideator.yaml").write_text("research_lifecycle: {mode: compare}\n", encoding="utf-8")
        (rf / "research_question_policy.yaml").write_text(POLICY, encoding="utf-8")
        ledger = ResearchQuestionLedger(rf / "research_question_events.jsonl", rf / "research_question_policy.yaml")
        ledger.append(event("question_admission", policy_version="test-v1"))
        ledger.append(event("evidence_run", revision_id="run1", run_id="run1", evidence_refs=["m"], result_ref="m"))
        manifest, output = root / "manifest.yaml", root / "output"; manifest.write_text("schema_version: 1\n", encoding="utf-8"); output.mkdir()
        spec = {"run_id": "run1", "status": "COMPLETE", "research_lifecycle": {"question_id": "q1", "lineage_id": "l1", "revision_id": "run1"}}
        with patch.object(run_recorder, "REPO_ROOT", root):
            run_recorder.record_lifecycle_projection_diff(spec, manifest)
            run_recorder.record_lifecycle_projection_diff(spec, manifest)
        diffs = rf / "lifecycle_projection_diffs.jsonl"
        self.assertEqual(1, len(diffs.read_text(encoding="utf-8").splitlines()))
        self.assertEqual((False, "requires 2 consecutive zero-diff cycles; has 1", 1), compare_window(diffs, 2))
        with diffs.open("a", encoding="utf-8") as handle: handle.write('{"run_id":"run2","differences":[]}\n')
        self.assertEqual((True, "cutover window satisfied", 2), compare_window(diffs, 2))
        ledger.append(event("proposal_revision", revision_id="r2", revision_key="distinct", validity_outcome="valid", compile_outcome="READY"))
        with patch.object(run_recorder, "REPO_ROOT", root):
            run_recorder.record_lifecycle_projection_diff(spec, manifest)
        rows = [__import__("json").loads(line) for line in diffs.read_text(encoding="utf-8").splitlines()]
        self.assertIn("duplicate_run_id", rows[-1]["differences"])

    def test_cutover_gate_rejects_missing_real_compare_window(self):
        ok, reason, count = compare_window(Path(self.tmp.name) / "no_diffs.jsonl", 30)
        self.assertFalse(ok); self.assertIn("requires 30", reason); self.assertEqual(0, count)

    def test_cutover_gate_requires_exactly_thirty_unique_zero_diff_runs(self):
        path = Path(self.tmp.name) / "diffs.jsonl"
        path.write_text("".join(f'{{"run_id":"r{i}","differences":[]}}\n' for i in range(29)), encoding="utf-8")
        self.assertFalse(compare_window(path, 30)[0])
        with path.open("a", encoding="utf-8") as handle: handle.write('{"run_id":"r29","differences":[]}\n')
        self.assertTrue(compare_window(path, 30)[0])
        with path.open("a", encoding="utf-8") as handle: handle.write('{"run_id":"r29","differences":["duplicate_run_id"]}\n')
        self.assertFalse(compare_window(path, 30)[0])

    def test_momentum_train_selection_is_deterministic_without_test_metrics(self):
        from scripts.evaluate_cb_arb_valuation_formula_momentum import _select_train_winner
        rows = [
            {"config_id": "w020_lb060", "penalty_weight": 0.2, "lookback_days": 60, "train_sharpe": 0.12345671},
            {"config_id": "w010_lb040", "penalty_weight": 0.1, "lookback_days": 40, "train_sharpe": 0.12345679},
        ]
        winner = _select_train_winner(rows)
        self.assertEqual("w010_lb040", winner["config_id"])
        self.assertNotIn("test_sharpe", winner)

    def test_momentum_grid_has_fifteen_nonbaseline_train_combinations(self):
        from scripts.evaluate_cb_arb_valuation_formula_momentum import _DEFAULT_MOMENTUM_LOOKBACKS, _DEFAULT_MOMENTUM_WEIGHTS, _FIXED_BASELINE
        grid = {(weight, lookback) for weight in _DEFAULT_MOMENTUM_WEIGHTS for lookback in _DEFAULT_MOMENTUM_LOOKBACKS}
        self.assertEqual(15, len(grid))
        self.assertNotIn(_FIXED_BASELINE, grid)

    def test_momentum_protocol_freezes_winner_and_rejects_second_test_exposure(self):
        from scripts.evaluate_cb_arb_valuation_formula_momentum import _freeze_test_protocol
        output = Path(self.tmp.name) / "run"; output.mkdir()
        args = SimpleNamespace(train_start="20180101", train_end="20221231", test_start="20230101", test_end="20260508")
        winner = {"config_id": "w010_lb020", "penalty_weight": 0.1, "lookback_days": 20, "train_sharpe": 0.1, "train_score": 0.1}
        protocol = _freeze_test_protocol(output, args, [winner], winner)
        self.assertEqual("w010_lb020", protocol["train_selection"]["winner"])
        self.assertEqual(2, protocol["test_exposure_count"])
        with self.assertRaisesRegex(RuntimeError, "test exposure already exists"):
            _freeze_test_protocol(output, args, [winner], winner)

    def test_momentum_bottom_diagnostic_uses_entries_only_and_never_selects(self):
        from scripts.evaluate_cb_arb_valuation_formula_momentum import _bottom_momentum_trade_count
        import pandas as pd
        count = _bottom_momentum_trade_count(
            {"trades": [{"cb_code": "cb1", "entry_date": "20250102"}, {"cb_code": "cb2", "entry_date": "20250102"}]},
            pd.DataFrame([
                {"stk_code": "s1", "trade_date": "20250102", "momentum_zscore": -1.2},
                {"stk_code": "s2", "trade_date": "20250102", "momentum_zscore": -0.3},
            ]),
            {"cb1": "s1", "cb2": "s2"},
        )
        self.assertEqual(1, count)

    def test_invalid_event_cannot_smuggle_status(self):
        with self.assertRaisesRegex(ValueError, "unknown event_type"):
            self.ledger.append(event("CLOSED_EVIDENCE_BACKED"))
