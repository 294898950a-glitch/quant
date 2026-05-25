"""Research Delta Gate (2026-05-25, Phase 1 V1).

Judges whether a Hermes-generated strategy proposal carries enough research
delta over the history of past experiments and recorded insights to justify
spending compute on it.

V1 emits exactly three actions:

* ``advance``  — proposal looks novel enough to flow into compile + queue.
* ``skip``     — proposal duplicates a settled prior run (same family,
                 same capability_ids, no new critical-insight reference) OR
                 sits inside a family the digest already marks as closed.
* ``watch``    — proposal is interesting but the evidence is thin (e.g. it
                 matches a prior run but cites a new critical insight, or
                 the queue is already saturated). Kept as an artifact for
                 review; not enqueued.

Out of scope for V1 (filed for later phases):

* ``merge``  — combining the proposal into an existing experiment.
* ``pivot``  — rewriting a previously failed direction in light of new evidence.

This module owns no side effects. It reads four inputs and returns one
:class:`DeltaDecision`. Persistence (writing the decision back to the
proposal artifact, updating the ideation status, refusing to enqueue) is
the caller's responsibility — see `framework/autonomous/ideation_cycle.py`
for the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Recognised actions. Callers must treat any other value as an internal
# error rather than silently re-mapping.
ACTIONS: frozenset[str] = frozenset({"advance", "skip", "watch"})


@dataclass(frozen=True)
class DeltaDecision:
    """Outcome of a delta-gate evaluation.

    Attributes
    ----------
    action:
        One of ``ACTIONS``.
    reason:
        A single human-readable sentence explaining the decision. The
        sentence must point at a concrete piece of evidence (run_id,
        insight id, family tag, or queue depth) so that the next reader
        can reproduce the call without re-deriving it.
    evidence:
        Structured pointers (run_id, family, insight id, queue_depth,
        ...). This is the audit trail; the reason is the human summary.
    """

    action: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(
                f"DeltaDecision.action must be one of {sorted(ACTIONS)}, "
                f"got {self.action!r}"
            )


# Tuning knobs (kept as module-level constants so they are visible in tests
# and easy to override later via direct patching).

# A family is considered "closed" if recent_results_digest lists it in
# suggested_closed_families with at least this many rejects.
CLOSED_FAMILY_REJECT_THRESHOLD: int = 2

# When the live queue holds at least this many queued/running items, new
# advances are throttled to "watch" so the queue does not balloon.
QUEUE_SATURATION_DEPTH: int = 5


_TERMINAL_NON_ADOPT_VERDICTS = frozenset({
    "rejected",
    "failed_mechanical_thresholds",
    "no_adoption_decision_unusable",
    "archive-direction",
})

_ADOPTED_VERDICTS = frozenset({"adopt", "adopted"})


def _str(value: Any) -> str:
    return str(value or "").strip()


def _capability_set(proposal: dict[str, Any]) -> tuple[str, ...]:
    raw = proposal.get("capability_ids") or []
    if not isinstance(raw, (list, tuple, set)):
        return tuple()
    return tuple(sorted({str(c) for c in raw if c}))


def _critical_insight_ids(research_insights: dict[str, Any] | None) -> set[str]:
    """Return ids of currently-active critical insights.

    "Active" means the insight is tagged priority: critical AND has NOT
    been marked deprecated / weakened by follow-up review. The latter is
    surfaced via either a ``do_not_use_as_default_direction: true`` flag
    or a ``status`` field whose value starts with "deprecated" or
    "weakened" (e.g. "weakened_by_follow_up_rejects_2026_05_25").
    Insights demoted from critical to deprecated by post-review evidence
    no longer count as evidence that justifies re-opening a closed
    family (2026-05-26 mandate).
    """
    if not isinstance(research_insights, dict):
        return set()
    out: set[str] = set()
    for item in research_insights.get("key_insights") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("priority", "")).lower() != "critical":
            continue
        if item.get("do_not_use_as_default_direction"):
            continue
        status_val = str(item.get("status", "")).lower()
        if status_val.startswith("deprecated") or status_val.startswith("weakened"):
            continue
        rid = item.get("id")
        if isinstance(rid, str) and rid:
            out.add(rid)
    return out


def _insight_closed_families(research_insights: dict[str, Any] | None) -> dict[str, str]:
    """Collect family → insight_id mapping from every insight's
    ``follow_up_review_results.closed_families`` list.

    This is the reject-chain defence (2026-05-26 mandate). When an
    insight's follow-up reviews converge on "the family this insight
    was driving is not viable", the insight gets a ``closed_families``
    list naming the specific families (and adjacent families that rest
    on the same disproven premise). A proposal whose family appears in
    any of these lists is treated as already-closed unless the proposal
    cites a *different* still-active critical insight.

    Returns a dict mapping family_name to the insight id that closed
    it, so the skip reason can name the evidence.
    """
    out: dict[str, str] = {}
    if not isinstance(research_insights, dict):
        return out
    for item in research_insights.get("key_insights") or []:
        if not isinstance(item, dict):
            continue
        followup = item.get("follow_up_review_results")
        if not isinstance(followup, dict):
            continue
        closed = followup.get("closed_families")
        if not isinstance(closed, list):
            continue
        insight_id = str(item.get("id") or "")
        for family in closed:
            family_str = str(family or "").strip()
            if family_str and family_str not in out:
                out[family_str] = insight_id
    return out


def _closed_family_tags(recent_digest: dict[str, Any] | None) -> set[str]:
    if not isinstance(recent_digest, dict):
        return set()
    out: set[str] = set()
    for entry in recent_digest.get("suggested_closed_families") or []:
        if not isinstance(entry, dict):
            continue
        try:
            count = int(entry.get("reject_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count >= CLOSED_FAMILY_REJECT_THRESHOLD:
            tag = entry.get("tag")
            if isinstance(tag, str) and tag:
                out.add(tag)
    return out


def _queue_depth(queue_state: dict[str, Any] | None) -> int:
    if not isinstance(queue_state, dict):
        return 0
    items = queue_state.get("queue") or []
    if not isinstance(items, list):
        return 0
    return sum(
        1
        for item in items
        if isinstance(item, dict) and _str(item.get("status")) in {"queued", "running"}
    )


def _prior_runs_in_family(
    recent_digest: dict[str, Any] | None, family: str
) -> list[dict[str, Any]]:
    if not isinstance(recent_digest, dict) or not family:
        return []
    out: list[dict[str, Any]] = []
    for run in recent_digest.get("recent_runs") or []:
        if not isinstance(run, dict):
            continue
        if _str(run.get("family")) == family:
            out.append(run)
    return out


def _proposal_cites_critical(
    proposal: dict[str, Any], critical_ids: set[str]
) -> tuple[bool, str]:
    """Return (cites, which_id). A proposal "cites a critical insight" if
    its source_insight field contains the verbatim id string.

    We do not allow fuzzy matching here — the proposal author (Hermes or
    user) must be explicit so the audit trail names the specific evidence.
    """
    source = _str(proposal.get("source_insight"))
    if not source or not critical_ids:
        return False, ""
    for cid in critical_ids:
        if cid in source:
            return True, cid
    return False, ""


def evaluate(
    proposal: dict[str, Any],
    *,
    recent_digest: dict[str, Any] | None = None,
    research_insights: dict[str, Any] | None = None,
    queue_state: dict[str, Any] | None = None,
) -> DeltaDecision:
    """Decide whether ``proposal`` carries enough research delta.

    Inputs are the same artifacts the ideator already loads:
    ``recent_results_digest.yaml``, ``research_insights.yaml``, and
    ``research_queue.yaml``. The function is pure: it does not write
    anywhere, does not call AI providers, does not start jobs.
    """
    if not isinstance(proposal, dict):
        raise TypeError("proposal must be a dict")
    family = _str(proposal.get("family"))
    capabilities = _capability_set(proposal)
    critical_ids = _critical_insight_ids(research_insights)
    cites_critical, cited_id = _proposal_cites_critical(proposal, critical_ids)
    closed_families = _closed_family_tags(recent_digest)
    queue_depth = _queue_depth(queue_state)

    # Rule 1 — family already closed by the digest. Skip unless the
    # proposal explicitly references a current critical insight that
    # might legitimately re-open the direction.
    if family and family in closed_families and not cites_critical:
        return DeltaDecision(
            action="skip",
            reason=(
                f"family {family!r} is in suggested_closed_families "
                f"(reject_count >= {CLOSED_FAMILY_REJECT_THRESHOLD}) and the "
                "proposal does not cite a critical research insight that would "
                "justify re-opening it."
            ),
            evidence={
                "closed_family": family,
                "queue_depth": queue_depth,
            },
        )

    # Rule 1b (2026-05-26 mandate) — family is in an insight-recorded
    # reject chain. When an insight's follow_up_review_results lists
    # ``closed_families`` (the family that drove this insight has been
    # disproven by N+ post-review rejects on related variants), any
    # new proposal in that family must cite a DIFFERENT, still-active
    # critical insight to be allowed through. Citing the deprecated
    # insight itself does not count — ``_critical_insight_ids`` already
    # filters out deprecated/weakened insights, so ``cites_critical``
    # will be False when the proposal only references the closed-by-
    # follow-up insight.
    insight_closed = _insight_closed_families(research_insights)
    if family and family in insight_closed and not cites_critical:
        closing_insight = insight_closed[family]
        return DeltaDecision(
            action="skip",
            reason=(
                f"family {family!r} appears in the closed_families list of "
                f"insight {closing_insight!r} (its follow-up reviews "
                "rejected the direction); the proposal does not cite a "
                "different, still-active critical insight that would "
                "overturn the reject chain."
            ),
            evidence={
                "insight_closed_family": family,
                "closing_insight": closing_insight,
                "queue_depth": queue_depth,
            },
        )

    # Rule 2 — exact duplicate by family + capability_ids vs a settled,
    # not-adopted prior run.
    if family and capabilities:
        for run in _prior_runs_in_family(recent_digest, family):
            prior_caps = tuple(sorted({str(c) for c in (run.get("capability_ids") or []) if c}))
            if prior_caps != capabilities:
                continue
            verdict = _str(run.get("verdict"))
            if verdict in _ADOPTED_VERDICTS:
                continue
            run_id = _str(run.get("run_id")) or "(unknown_run)"
            if not cites_critical:
                return DeltaDecision(
                    action="skip",
                    reason=(
                        f"duplicate of {run_id}: same family {family!r}, same "
                        f"capability_ids {list(capabilities)}; prior verdict "
                        f"{verdict or 'unsettled'}; the proposal does not cite "
                        "a critical research insight that distinguishes it."
                    ),
                    evidence={
                        "duplicate_of": run_id,
                        "verdict": verdict,
                        "queue_depth": queue_depth,
                    },
                )
            # Same shape but cites a critical insight that may not have been
            # in scope last time — keep as artifact for review instead of
            # auto-running again.
            return DeltaDecision(
                action="watch",
                reason=(
                    f"shape matches prior {run_id} (verdict "
                    f"{verdict or 'unsettled'}) but proposal cites critical "
                    f"insight {cited_id!r}; hold as evidence-pending instead "
                    "of immediately re-running."
                ),
                evidence={
                    "duplicate_of": run_id,
                    "verdict": verdict,
                    "cites_critical": cited_id,
                    "queue_depth": queue_depth,
                },
            )

    # Rule 3 — queue saturation. Downgrade new work to "watch" so the
    # research queue does not balloon during periods of heavy ideation.
    if queue_depth >= QUEUE_SATURATION_DEPTH:
        return DeltaDecision(
            action="watch",
            reason=(
                f"queue holds {queue_depth} active items "
                f"(>= QUEUE_SATURATION_DEPTH={QUEUE_SATURATION_DEPTH}); "
                "throttle new advances to watch until the queue drains."
            ),
            evidence={
                "queue_depth": queue_depth,
                "family": family,
            },
        )

    # Default: advance.
    note = (
        f"cites critical insight {cited_id!r}"
        if cites_critical
        else "no critical-insight reference"
    )
    return DeltaDecision(
        action="advance",
        reason=(
            f"novel family/capabilities (family={family or '∅'}, "
            f"capability_ids={list(capabilities) or '∅'}); {note}; "
            f"queue depth={queue_depth}."
        ),
        evidence={
            "family": family,
            "capability_ids": list(capabilities),
            "cites_critical": cited_id if cites_critical else None,
            "queue_depth": queue_depth,
        },
    )
