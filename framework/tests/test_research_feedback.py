from __future__ import annotations

import unittest

from framework.evaluation.research_feedback import (
    ClaudeReview,
    ExplorationPacket,
    ResearchQuestion,
    ReviewVerdict,
    ScoreVector,
    accept_claude_review,
    validate_exploration,
)


def packet() -> ExplorationPacket:
    question = ResearchQuestion(
        question_id="rq_1",
        question="Can the edge survive conservative costs?",
        source_dimension="realizability",
        premise=("gross edge exists", "cost-on is negative"),
        must_change=("execution_mechanism",),
        forbidden=("parameter_only_tuning",),
        required_evidence=("cost_on", "holdout", "rolling"),
        falsifier=("cost_on_return <= 0",),
        evidence_refs=("holdout_1", "cost_1"),
    )
    return ExplorationPacket(
        schema_version=1, run_id="run_1", plan_version="p1",
        candidates=(question,), evidence_refs=("holdout_1", "cost_1"),
        score_vectors={"rq_1": ScoreVector(3, 2, 1, 1, 3, 4)},
    )


class TestResearchFeedback(unittest.TestCase):
    def test_exploration_validates_typed_scores_and_refs(self) -> None:
        self.assertEqual(validate_exploration(packet()), [])

    def test_only_pass_admits_next_research_input(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, "rq_1", (), ("cost_1",), (), ({"type": "required_test", "value": "high_slippage"},), ("high_slippage",))
        research_input, errors = accept_claude_review(packet(), review)
        self.assertEqual(errors, [])
        assert research_input is not None
        self.assertEqual(research_input.question.question_id, "rq_1")
        self.assertIn("execution_mechanism", [item["value"] for item in research_input.constraints])
        self.assertIn("high_slippage", research_input.required_tests)

    def test_revise_or_block_never_admits_input(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.REVISE, "rq_1", ("weak premise",), (), (), (), ())
        research_input, errors = accept_claude_review(packet(), review)
        self.assertIsNone(research_input)
        self.assertTrue(errors)

    def test_block_never_admits_input(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.BLOCK, None, ("closed direction",), (), (), (), ())
        research_input, errors = accept_claude_review(packet(), review)
        self.assertIsNone(research_input)
        self.assertTrue(errors)

    def test_pass_with_blockers_is_rejected(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, "rq_1", ("unresolved",), ("cost_1",), (), (), ())
        research_input, errors = accept_claude_review(packet(), review)
        self.assertIsNone(research_input)
        self.assertTrue(errors)

    def test_pass_without_accepted_question_is_rejected(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, None, (), ("cost_1",), (), (), ())
        research_input, errors = accept_claude_review(packet(), review)
        self.assertIsNone(research_input)
        self.assertTrue(errors)

    def test_pass_evidence_in_packet_but_not_question_is_rejected(self) -> None:
        raw = packet()
        raw = ExplorationPacket(raw.schema_version, raw.run_id, raw.plan_version, raw.candidates, raw.evidence_refs + ("other",), raw.score_vectors)
        review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, "rq_1", (), ("other",), (), (), ())
        research_input, errors = accept_claude_review(raw, review)
        self.assertIsNone(research_input)
        self.assertTrue(errors)

    def test_research_input_carries_score_vector_and_separates_evidence(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, "rq_1", (), ("cost_1",), (), ("high_slippage",), ("high_slippage",), ScoreVector(2, 2, 1, 1, 2, 4))
        research_input, errors = accept_claude_review(packet(), review)
        self.assertEqual(errors, [])
        assert research_input is not None
        self.assertEqual(research_input.required_evidence, ("cost_on", "holdout", "rolling"))
        self.assertEqual(research_input.required_tests, ("high_slippage",))
        self.assertEqual(research_input.score_vectors["rq_1"].information_gain, 4)
        self.assertEqual(research_input.review_score_vector.realizability, 1)

    def test_pass_with_unknown_evidence_is_rejected(self) -> None:
        review = ClaudeReview(1, "review_1", ReviewVerdict.PASS, "rq_1", (), ("unknown",), (), (), ())
        research_input, errors = accept_claude_review(packet(), review)
        self.assertIsNone(research_input)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
