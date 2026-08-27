"""Append-only facts and read-only projections for research questions.

This Phase-0 module deliberately has no dependency on pending delivery, queue,
or pipeline writers.  Those systems retain their own execution authority.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from framework.autonomous.jsonl_ledger import append_jsonl, ledger_lock


EVENT_TYPES = {"admission_attempt", "external_review_attempt", "question_admission", "proposal_revision", "evidence_run", "holdout_reservation", "holdout_exposure", "holdout_final_run", "closure"}
CLOSURES = {"evidence_backed", "policy_budget", "external_block_exhausted"}


@dataclass(frozen=True)
class QuestionPolicy:
    policy_version: str
    revision_limit: int
    reframe_limit: int
    holdout_required: bool

    @classmethod
    def load(cls, path: Path | str) -> "QuestionPolicy":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping): raise ValueError("policy must be a mapping")
        revision, reframe, holdout = raw.get("revision_budget") or {}, raw.get("reframe_budget") or {}, raw.get("holdout") or {}
        value = cls(str(raw.get("policy_version") or ""), int(revision.get("max_per_lineage") or 0), int(reframe.get("max_per_lineage") or 0), bool(holdout.get("required_for_validated_label")))
        if not value.policy_version or value.revision_limit < 1 or value.reframe_limit < 0: raise ValueError("invalid research question policy")
        return value


@dataclass(frozen=True)
class QuestionState:
    value: str
    reason: str
    policy_version: str
    fact_count: int


def _key(event: Mapping[str, Any]) -> str:
    identity = dict(event); identity.pop("at", None); identity.pop("event_key", None)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_event(event: Mapping[str, Any]) -> list[str]:
    kind = str(event.get("event_type") or ""); errors = []
    if kind not in EVENT_TYPES: errors.append("unknown event_type")
    if kind != "admission_attempt" and (not event.get("question_id") or not event.get("lineage_id")): errors.append("question_id and lineage_id required")
    if kind == "question_admission" and not event.get("policy_version"): errors.append("admission policy_version required")
    if kind == "proposal_revision" and not event.get("revision_id"): errors.append("revision_id required")
    if kind == "holdout_reservation" and not event.get("reservation_operation_id"): errors.append("reservation_operation_id required")
    if kind == "holdout_final_run" and not event.get("holdout_final_run_id"): errors.append("holdout_final_run_id required")
    if kind == "closure":
        if event.get("closure_reason_type") not in CLOSURES: errors.append("invalid closure reason")
        if event.get("closure_claim") not in {"validated", "not_validated"}: errors.append("invalid closure claim")
        if event.get("closure_claim") == "validated" and not event.get("holdout_final_run_id"): errors.append("validated closure requires holdout_final_run_id")
    return errors


def derive_state(question: list[Mapping[str, Any]], lineage: list[Mapping[str, Any]], policy: QuestionPolicy) -> QuestionState:
    # JSONL append order is the authoritative happens-before order.  Seconds
    # are only observability metadata and cannot order two same-second writes.
    ordered = list(question)
    closures = [e for e in ordered if e.get("event_type") == "closure"]
    if closures: return QuestionState("CLOSED_" + str(closures[0]["closure_reason_type"]).upper(), "terminal closure", policy.policy_version, len(ordered))
    finals = [e for e in ordered if e.get("event_type") == "holdout_final_run"]
    if finals: return QuestionState("HOLDOUT_RESULT_UNCLOSED", "final holdout needs closure", policy.policy_version, len(ordered))
    exposed = {str(e.get("reservation_id")) for e in ordered if e.get("event_type") == "holdout_exposure"}
    reservations = [e for e in ordered if e.get("event_type") == "holdout_reservation" and str(e.get("reservation_id")) not in exposed]
    if reservations: return QuestionState("AWAITING_HOLDOUT_EXECUTION", "active holdout reservation", policy.policy_version, len(ordered))
    if exposed: return QuestionState("REQUIRES_NEW_HOLDOUT", "holdout exposed", policy.policy_version, len(ordered))
    revisions = [e for e in lineage if e.get("event_type") == "proposal_revision"]
    revision_keys = {str(e.get("revision_key") or e.get("revision_id")) for e in revisions}
    reframes = [e for e in lineage if e.get("event_type") == "question_admission" and e.get("parent_question_id")]
    if len(revision_keys) >= policy.revision_limit:
        if len(reframes) < policy.reframe_limit:
            return QuestionState("REFRAME_REQUIRED", "revision budget exhausted", policy.policy_version, len(ordered))
        return QuestionState("POLICY_BUDGET_CLOSURE_REQUIRED", "revision and reframe budgets exhausted", policy.policy_version, len(ordered))
    local_revisions = [e for e in ordered if e.get("event_type") == "proposal_revision"]
    if local_revisions:
        last = local_revisions[-1]
        if last.get("validity_outcome") != "valid" or last.get("compile_outcome") != "READY": return QuestionState("IN_PROPOSAL_REVISION", "latest revision is not executable", policy.policy_version, len(ordered))
        if not any(e.get("event_type") == "evidence_run" and e.get("revision_id") == last.get("revision_id") for e in ordered): return QuestionState("AWAITING_EXPERIMENT_RUN", "missing evidence", policy.policy_version, len(ordered))
        return QuestionState("EVALUATED", "evidence recorded", policy.policy_version, len(ordered))
    return QuestionState("ADMITTED_AWAITING_PROPOSAL" if any(e.get("event_type") == "question_admission" for e in ordered) else "NEW", "facts projected", policy.policy_version, len(ordered))


class ResearchQuestionLedger:
    def __init__(self, events_path: Path | str, policy_path: Path | str): self.events_path, self.policy_path = Path(events_path), Path(policy_path)
    @contextmanager
    def _lock(self) -> Iterator[None]:
        # Same "<events file>.lock" file this class locked inline before, now
        # owned by the shared writer so old and new holders still exclude each
        # other.  The whole dedupe/closure check plus the append must stay in
        # one critical section, so append() passes lock_held=True; flock is per
        # open file description, so re-locking here would self-deadlock.
        with ledger_lock(self.events_path):
            yield
    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists(): return []
        out=[]
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try: row=json.loads(line)
            except json.JSONDecodeError: continue
            if isinstance(row, dict): out.append(row)
        return out
    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        row=dict(event); row.setdefault("at", int(time.time())); row.setdefault("schema_version", 1); row["event_key"]=_key(row)
        errors=validate_event(row)
        if errors: raise ValueError("; ".join(errors))
        with self._lock():
            existing = self.events()
            if any(e.get("event_key")==row["event_key"] for e in existing): return row
            question_id = row.get("question_id")
            terminal = [e for e in existing if e.get("question_id") == question_id and e.get("event_type") == "closure"]
            if terminal:
                if row.get("event_type") == "closure":
                    return terminal[0]
                raise ValueError("cannot append facts after terminal closure")
            if row.get("event_type") == "closure" and row.get("closure_claim") == "validated":
                target = row.get("holdout_final_run_id")
                if not any(e.get("question_id") == question_id and e.get("event_type") == "holdout_final_run" and e.get("holdout_final_run_id") == target for e in existing):
                    raise ValueError("validated closure references missing holdout_final_run")
            append_jsonl(self.events_path, row, lock_held=True)
        return row
    def state(self, question_id: str) -> QuestionState:
        all_events=self.events(); question=[e for e in all_events if e.get("question_id")==question_id]
        lineage_id=str(question[0].get("lineage_id")) if question else ""
        return derive_state(question, [e for e in all_events if e.get("lineage_id")==lineage_id], QuestionPolicy.load(self.policy_path))
