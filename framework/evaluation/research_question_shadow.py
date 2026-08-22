"""Phase-1 shadow writer.  It observes pending delivery; it never claims it."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from framework.evaluation.pending_research_input import PendingResearchInputStore
from framework.evaluation.research_feedback import ResearchInput
from framework.evaluation.research_question import ResearchQuestionLedger


def admission_operation_id(research_input: ResearchInput) -> str:
    raw = f"{research_input.source_review_id}|{research_input.source_run_id}|{research_input.question.question_id}"
    return "admit_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def question_identity(research_input: ResearchInput) -> tuple[str, str]:
    question = research_input.question
    question_id = "rq_" + hashlib.sha256(f"{research_input.source_review_id}|{research_input.source_run_id}|{question.question_id}".encode()).hexdigest()[:16]
    lineage_id = "lineage_" + hashlib.sha256(f"{question.source_dimension}|{' '.join(question.question.casefold().split())}".encode()).hexdigest()[:16]
    return question_id, lineage_id


class ShadowAdmissionService:
    def __init__(self, ledger: ResearchQuestionLedger, pending: PendingResearchInputStore, output_root: Path | str, ttl_seconds: int) -> None:
        self.ledger, self.pending, self.output_root, self.ttl_seconds = ledger, pending, Path(output_root), ttl_seconds

    def record_attempt(self, research_input: ResearchInput) -> str:
        operation = admission_operation_id(research_input)
        self.ledger.append({"event_type": "admission_attempt", "admission_operation_id": operation,
                            "research_input_ref": hashlib.sha256(json.dumps(research_input.to_dict(), sort_keys=True).encode()).hexdigest(), "outcome": "attempted"})
        return operation

    def reconcile(self, provider: str = "external", model: str = "unknown") -> int:
        """Append facts only for a pending-store state reconstructed under its lock."""
        slot = self.pending.active(self.output_root, self.ttl_seconds)
        if not isinstance(slot, Mapping) or str(slot.get("state")) not in {"pending", "claimed"}:
            return 0
        raw = slot.get("research_input")
        if not isinstance(raw, Mapping): return 0
        item = ResearchInput.from_dict(raw)
        operation = str(slot.get("admission_operation_id") or admission_operation_id(item))
        question_id, lineage_id = question_identity(item)
        question_hash = hashlib.sha256(" ".join(item.question.question.split()).encode()).hexdigest()
        self.ledger.append({"event_type": "external_review_attempt", "question_id": question_id, "lineage_id": lineage_id,
                            "admission_operation_id": operation, "candidate_hash": question_hash, "attempt_ordinal": 1,
                            "provider": provider, "model": model, "verdict": "PASS", "review_id": item.source_review_id})
        self.ledger.append({"event_type": "question_admission", "question_id": question_id, "lineage_id": lineage_id,
                            "admission_operation_id": operation, "research_input_ref": hashlib.sha256(json.dumps(item.to_dict(), sort_keys=True).encode()).hexdigest(),
                            "question_text_hash": question_hash, "policy_version": self.ledger.state(question_id).policy_version,
                            "source_review_id": item.source_review_id, "source_run_id": item.source_run_id})
        return 1

    def record_revision(self, research_input: ResearchInput, proposal: Mapping[str, Any], *, compile_outcome: str) -> None:
        question_id, lineage_id = question_identity(research_input)
        design = proposal.get("test_design") if isinstance(proposal.get("test_design"), Mapping) else {}
        substantive = {"mechanics": list(proposal.get("mechanics") or []), "evidence": list(design.get("required_evidence") or []), "falsifiers": proposal.get("falsifiers") or {}}
        revision_key = "rev_" + hashlib.sha256(json.dumps(substantive, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        self.ledger.append({
            "event_type": "proposal_revision", "question_id": question_id, "lineage_id": lineage_id,
            "revision_id": str(proposal.get("proposal_id") or ""),
            "revision_key": revision_key,
            "mechanism_delta": list(proposal.get("mechanics") or []),
            "evidence_delta": list(design.get("required_evidence") or []),
            "falsifier_delta": proposal.get("falsifiers") or {},
            "validity_outcome": "valid", "compile_outcome": compile_outcome,
        })
