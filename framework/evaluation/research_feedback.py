"""Deterministic contract between exploration/review and next-round ideation.

LLMs may create the packet and review records, but this module is the sole
place where a PASS becomes a constrained research input.  It intentionally
contains no provider calls or mutable project truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


SCORE_DIMENSIONS = (
    "observation_trust", "incremental_edge", "stability", "realizability",
    "falsifiable_mechanism", "information_gain",
)
RESEARCH_INPUT_SCHEMA_VERSION = 2
_CONSTRAINT_KEYS = ("must_change", "forbidden", "required_evidence", "review_constraints", "required_changes")


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    REVISE = "REVISE"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class ScoreVector:
    observation_trust: int
    incremental_edge: int
    stability: int
    realizability: int
    falsifiable_mechanism: int
    information_gain: int

    def validate(self) -> list[str]:
        return [f"{name} must be an integer in [0, 4]" for name in SCORE_DIMENSIONS
                if not isinstance(getattr(self, name), int) or isinstance(getattr(self, name), bool)
                or not 0 <= getattr(self, name) <= 4]

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in SCORE_DIMENSIONS}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScoreVector":
        return cls(**{name: raw.get(name) for name in SCORE_DIMENSIONS})


@dataclass(frozen=True)
class ResearchQuestion:
    question_id: str
    question: str
    source_dimension: str
    premise: tuple[str, ...]
    must_change: tuple[str, ...]
    forbidden: tuple[str, ...]
    required_evidence: tuple[str, ...]
    falsifier: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {name: list(getattr(self, name)) if isinstance(getattr(self, name), tuple) else getattr(self, name)
                for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchQuestion":
        sequence = ("premise", "must_change", "forbidden", "required_evidence", "falsifier", "evidence_refs")
        return cls(
            question_id=str(raw.get("question_id") or ""), question=str(raw.get("question") or ""),
            source_dimension=str(raw.get("source_dimension") or ""),
            **{name: tuple(str(x) for x in (raw.get(name) or [])) for name in sequence},
        )


@dataclass(frozen=True)
class ExplorationPacket:
    schema_version: int
    run_id: str
    plan_version: str
    candidates: tuple[ResearchQuestion, ...]
    evidence_refs: tuple[str, ...]
    score_vectors: dict[str, ScoreVector] = field(default_factory=dict)
    open_uncertainties: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "run_id": self.run_id, "plan_version": self.plan_version,
                "candidates": [item.to_dict() for item in self.candidates], "evidence_refs": list(self.evidence_refs),
                "score_vectors": {key: value.as_dict() for key, value in self.score_vectors.items()},
                "open_uncertainties": list(self.open_uncertainties)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExplorationPacket":
        return cls(int(raw.get("schema_version") or 0), str(raw.get("run_id") or ""), str(raw.get("plan_version") or ""),
                   tuple(ResearchQuestion.from_dict(x) for x in raw.get("candidates", []) if isinstance(x, Mapping)),
                   tuple(str(x) for x in raw.get("evidence_refs", [])),
                   {str(key): ScoreVector.from_dict(value) for key, value in (raw.get("score_vectors") or {}).items() if isinstance(value, Mapping)},
                   tuple(str(x) for x in raw.get("open_uncertainties", [])))


@dataclass(frozen=True)
class ClaudeReview:
    schema_version: int
    review_id: str
    verdict: ReviewVerdict
    accepted_question_id: str | None
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    required_changes: tuple[str, ...]
    constraints: tuple[dict[str, Any], ...]
    required_tests: tuple[str, ...]
    score_vector: ScoreVector | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "review_id": self.review_id, "verdict": self.verdict.value,
                "accepted_question_id": self.accepted_question_id, "blockers": list(self.blockers),
                "evidence_refs": list(self.evidence_refs), "required_changes": list(self.required_changes),
                "constraints": [dict(item) for item in self.constraints], "required_tests": list(self.required_tests),
                "score_vector": self.score_vector.as_dict() if self.score_vector else None}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ClaudeReview":
        verdict = raw.get("verdict")
        return cls(int(raw.get("schema_version") or 0), str(raw.get("review_id") or ""), ReviewVerdict(str(verdict)),
                   str(raw["accepted_question_id"]) if raw.get("accepted_question_id") is not None else None,
                   tuple(str(x) for x in raw.get("blockers", [])), tuple(str(x) for x in raw.get("evidence_refs", [])),
                   tuple(str(x) for x in raw.get("required_changes", [])),
                   tuple(dict(x) for x in raw.get("constraints", []) if isinstance(x, Mapping)),
                   tuple(str(x) for x in raw.get("required_tests", [])),
                   ScoreVector.from_dict(raw["score_vector"]) if isinstance(raw.get("score_vector"), Mapping) else None)


@dataclass(frozen=True)
class ResearchInput:
    schema_version: int
    source_review_id: str
    source_run_id: str
    plan_version: str
    question: ResearchQuestion
    applied_constraints: dict[str, Any]
    required_evidence: tuple[str, ...]
    required_tests: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    score_vectors: dict[str, ScoreVector] = field(default_factory=dict)
    review_score_vector: ScoreVector | None = None

    @property
    def constraints(self) -> tuple[dict[str, Any], ...]:
        """Compatibility view for v1 readers; new code uses applied_constraints."""
        result: list[dict[str, Any]] = []
        for key in ("must_change", "forbidden", "required_evidence"):
            result.extend({"type": key, "value": value} for value in self.applied_constraints.get(key, []))
        result.extend(dict(value) for value in self.applied_constraints.get("review_constraints", []))
        return tuple(result)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "source_review_id": self.source_review_id,
                "source_run_id": self.source_run_id, "plan_version": self.plan_version,
                "question": self.question.to_dict(), "applied_constraints": _copy_constraints(self.applied_constraints),
                "required_evidence": list(self.required_evidence), "required_tests": list(self.required_tests),
                "evidence_refs": list(self.evidence_refs),
                "score_vectors": {key: value.as_dict() for key, value in self.score_vectors.items()},
                "review_score_vector": self.review_score_vector.as_dict() if self.review_score_vector else None}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchInput":
        version = int(raw.get("schema_version") or 0)
        question_raw = raw.get("question") if isinstance(raw.get("question"), Mapping) else {}
        if version == 1:  # readable only; admission always emits v2.
            old = raw.get("constraints") or []
            applied = _classify_constraints(tuple(dict(x) for x in old if isinstance(x, Mapping)), ())
        else:
            applied = _copy_constraints(raw.get("applied_constraints") if isinstance(raw.get("applied_constraints"), Mapping) else {})
        return cls(version, str(raw.get("source_review_id") or ""), str(raw.get("source_run_id") or ""),
                   str(raw.get("plan_version") or ""), ResearchQuestion.from_dict(question_raw), applied,
                   tuple(str(x) for x in raw.get("required_evidence", [])), tuple(str(x) for x in raw.get("required_tests", [])),
                   tuple(str(x) for x in raw.get("evidence_refs", [])),
                   {str(key): ScoreVector.from_dict(value) for key, value in (raw.get("score_vectors") or {}).items() if isinstance(value, Mapping)},
                   ScoreVector.from_dict(raw["review_score_vector"]) if isinstance(raw.get("review_score_vector"), Mapping) else None)


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))


def _copy_constraints(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"must_change": list(raw.get("must_change") or []), "forbidden": list(raw.get("forbidden") or []),
            "required_evidence": list(raw.get("required_evidence") or []),
            "review_constraints": [dict(x) for x in raw.get("review_constraints", []) if isinstance(x, Mapping)],
            "required_changes": list(raw.get("required_changes") or [])}


def _classify_constraints(constraints: tuple[dict[str, Any], ...], required_changes: tuple[str, ...], question: ResearchQuestion | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"must_change": list(question.must_change if question else ()),
                              "forbidden": list(question.forbidden if question else ()),
                              "required_evidence": list(question.required_evidence if question else ()),
                              "review_constraints": [], "required_changes": list(required_changes)}
    for raw_item in constraints:
        # Tolerate historical tuple construction where a bare string occupied
        # the constraints position; preserve it as opaque rather than dropping it.
        item = raw_item if isinstance(raw_item, Mapping) else {"type": "legacy_constraint", "value": str(raw_item)}
        kind, value = item.get("type"), item.get("value")
        if kind in {"must_change", "forbidden", "required_evidence"} and isinstance(value, str) and value:
            result[kind].append(value)
        else:
            result["review_constraints"].append(dict(item))
    for key in ("must_change", "forbidden", "required_evidence", "required_changes"):
        result[key] = list(_unique(result[key]))
    return result


def _question_errors(question: ResearchQuestion) -> list[str]:
    errors: list[str] = []
    if not question.question_id or not question.question:
        errors.append("question_id and question are required")
    if question.source_dimension not in SCORE_DIMENSIONS:
        errors.append("source_dimension is not a score dimension")
    for name in ("premise", "must_change", "required_evidence", "falsifier"):
        if not getattr(question, name):
            errors.append(f"{name} must not be empty")
    if not question.evidence_refs:
        errors.append("question evidence_refs must not be empty")
    return errors


def validate_exploration(packet: ExplorationPacket) -> list[str]:
    errors: list[str] = []
    if packet.schema_version != 1:
        errors.append("exploration schema_version must be 1")
    if not packet.run_id or not packet.plan_version:
        errors.append("run_id and plan_version are required")
    if not packet.candidates:
        errors.append("at least one candidate research question is required")
    refs = set(packet.evidence_refs)
    for question in packet.candidates:
        errors.extend(f"{question.question_id}: {error}" for error in _question_errors(question))
        missing = set(question.evidence_refs) - refs
        if missing:
            errors.append(f"{question.question_id}: evidence refs not in packet: {sorted(missing)}")
    for key, vector in packet.score_vectors.items():
        if key not in {question.question_id for question in packet.candidates}:
            errors.append(f"score vector references unknown question: {key}")
        errors.extend(f"{key}: {error}" for error in vector.validate())
    return errors


def accept_claude_review(packet: ExplorationPacket, review: ClaudeReview) -> tuple[ResearchInput | None, list[str]]:
    """The only deterministic PASS gate; successful admission always yields v2."""
    errors = validate_exploration(packet)
    if review.schema_version != 1:
        errors.append("review schema_version must be 1")
    if review.verdict is not ReviewVerdict.PASS:
        return None, [f"review verdict is {review.verdict.value}; no research input admitted"]
    if not review.review_id:
        errors.append("PASS review_id is required")
    if review.blockers:
        errors.append("PASS review must have no blockers")
    question = {item.question_id: item for item in packet.candidates}.get(review.accepted_question_id or "")
    if question is None:
        errors.append("PASS review must accept a candidate question")
    review_refs = set(review.evidence_refs)
    if not review_refs.issubset(set(packet.evidence_refs)):
        errors.append("review evidence_refs must be present in exploration packet")
    if question is not None and not review_refs.issubset(set(question.evidence_refs)):
        errors.append("review evidence_refs must be present in accepted question")
    if errors:
        return None, errors
    assert question is not None
    applied = _classify_constraints(review.constraints, review.required_changes, question)
    return ResearchInput(RESEARCH_INPUT_SCHEMA_VERSION, review.review_id, packet.run_id, packet.plan_version, question,
                         applied, tuple(applied["required_evidence"]), _unique(review.required_tests),
                         tuple(sorted(review_refs)), dict(packet.score_vectors), review.score_vector), []


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalized_token(value: Any) -> str:
    return re.sub(r"[\s_-]+", "_", _normalized_text(value).casefold()).strip("_")


def _as_unique_tokens(value: Any, label: str, errors: list[str]) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label} must be a non-empty-string list")
        return set()
    tokens = [_normalized_token(item) for item in value]
    if len(tokens) != len(set(tokens)):
        errors.append(f"{label} contains duplicates")
    return set(tokens)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _path_value(proposal: Mapping[str, Any], path: str) -> Any:
    value: Any = proposal
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _nonempty(value: Any) -> bool:
    return bool(value) if not isinstance(value, str) else bool(value.strip())


def _hypothesis_tokens(value: Any) -> list[str]:
    return re.findall(r"[\w]+", _normalized_text(value).casefold().replace("-", " ").replace("_", " "))


def validate_research_input_proposal(
    proposal: Mapping[str, Any], research_input: ResearchInput, active_plan_version: str,
    constraint_bindings: Mapping[str, Any], *, expected_proposal_id: str | None = None,
) -> list[str]:
    """Fail-closed validator for the conditional pending-input proposal contract."""
    errors: list[str] = []
    if research_input.schema_version != RESEARCH_INPUT_SCHEMA_VERSION:
        errors.append("pending research input must use schema_version 2")
    if research_input.plan_version != str(active_plan_version):
        errors.append("pending input plan_version does not match active plan")
    feedback = proposal.get("research_feedback")
    test_design = proposal.get("test_design")
    if not isinstance(feedback, Mapping):
        return ["research_feedback must be a mapping"]
    if not isinstance(test_design, Mapping):
        return ["test_design must be a mapping"]
    proposal_id = str(proposal.get("proposal_id") or "")
    if expected_proposal_id and proposal_id != expected_proposal_id:
        errors.append("proposal_id does not match preallocated claim")
    expected_ids = {"source_review_id": research_input.source_review_id, "source_run_id": research_input.source_run_id,
                    "plan_version": research_input.plan_version, "proposal_id": proposal_id,
                    "accepted_question_id": research_input.question.question_id}
    for key, expected in expected_ids.items():
        if str(feedback.get(key) or "") != expected:
            errors.append(f"research_feedback.{key} mismatch")
    question_text = _normalized_text(research_input.question.question)
    if _normalized_text(feedback.get("accepted_question_text")) != question_text:
        errors.append("research_feedback.accepted_question_text mismatch")
    for key, expected in (("accepted_question_id", research_input.question.question_id), ("accepted_question_text", question_text)):
        actual = test_design.get(key)
        if (_normalized_text(actual) if key.endswith("text") else str(actual or "")) != expected:
            errors.append(f"test_design.{key} mismatch")
    expected_constraints = _copy_constraints(research_input.applied_constraints)
    supplied = feedback.get("applied_constraints")
    if not isinstance(supplied, Mapping):
        errors.append("research_feedback.applied_constraints must be a mapping")
        supplied = {}
    for key in _CONSTRAINT_KEYS:
        actual = supplied.get(key)
        expected = expected_constraints[key]
        if key == "review_constraints":
            if not isinstance(actual, list) or _canonical(actual) != _canonical(expected):
                errors.append("research_feedback.applied_constraints.review_constraints mismatch")
        else:
            got = _as_unique_tokens(actual, f"research_feedback.applied_constraints.{key}", errors)
            want = {_normalized_token(item) for item in expected}
            if got != want:
                errors.append(f"research_feedback.applied_constraints.{key} mismatch")
    must_change = {_normalized_token(item) for item in expected_constraints["must_change"]}
    required_evidence = {_normalized_token(item) for item in expected_constraints["required_evidence"]}
    required_tests = {_normalized_token(item) for item in research_input.required_tests}
    changed = _as_unique_tokens(test_design.get("changed_dimensions"), "test_design.changed_dimensions", errors)
    if changed != must_change:
        errors.append("test_design.changed_dimensions must exactly equal must_change")
    evidence = _as_unique_tokens(test_design.get("required_evidence"), "test_design.required_evidence", errors)
    if evidence != required_evidence:
        errors.append("test_design.required_evidence mismatch")
    tests = _as_unique_tokens(test_design.get("required_tests"), "test_design.required_tests", errors)
    if tests != required_tests:
        errors.append("test_design.required_tests mismatch")
    feedback_tests = _as_unique_tokens(feedback.get("required_tests"), "research_feedback.required_tests", errors)
    if feedback_tests != required_tests:
        errors.append("research_feedback.required_tests mismatch")
    proposal_changes = proposal.get("required_changes")
    expected_changes = {_normalized_token(item) for item in expected_constraints["required_changes"]}
    if _as_unique_tokens(proposal_changes, "proposal.required_changes", errors) != expected_changes:
        errors.append("proposal.required_changes mismatch")
    bindings = constraint_bindings.get("bindings") if isinstance(constraint_bindings.get("bindings"), Mapping) else constraint_bindings
    declared = test_design.get("constraint_bindings")
    if not isinstance(declared, Mapping):
        errors.append("test_design.constraint_bindings must be a mapping")
        declared = {}
    if {_normalized_token(key) for key in declared} != must_change:
        errors.append("test_design.constraint_bindings keys mismatch")
    for token in must_change:
        allowed = bindings.get(token) if isinstance(bindings, Mapping) else None
        if not isinstance(allowed, list) or not all(isinstance(path, str) for path in allowed):
            errors.append(f"unregistered must_change token: {token}")
            continue
        actual_paths = declared.get(token)
        if actual_paths != allowed:
            errors.append(f"constraint_bindings.{token} mismatch")
        for path in allowed:
            if not _nonempty(_path_value(proposal, path)):
                errors.append(f"must_change binding {token} has empty field {path}")
    forbidden = {_normalized_token(item) for item in expected_constraints["forbidden"]}
    direct_values = [proposal.get("family"), *(proposal.get("mechanics") if isinstance(proposal.get("mechanics"), list) else []), *(test_design.get("changed_dimensions") if isinstance(test_design.get("changed_dimensions"), list) else [])]
    direct_tokens = {_normalized_token(item) for item in direct_values}
    words = _hypothesis_tokens(proposal.get("hypothesis"))
    for item in forbidden:
        phrase = [part for part in item.split("_") if part]
        phrase_present = any(words[index:index + len(phrase)] == phrase for index in range(len(words) - len(phrase) + 1))
        if item in direct_tokens or phrase_present:
            errors.append(f"forbidden token present: {item}")
    return errors
