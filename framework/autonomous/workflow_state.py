"""Shared workflow state helpers for autonomous research drivers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ACTIVE_STATUSES = {"queued", "running"}


@dataclass(frozen=True)
class WorkflowDecision:
    action: str
    reason: str
    counts: dict[str, int]


def queue_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("queue") if isinstance(state, dict) else []
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("workflow state queue must be a list")
    return [item for item in raw if isinstance(item, dict)]


def queue_counts(state: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in queue_items(state):
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def count_status(state: dict[str, Any], status: str) -> int:
    return queue_counts(state).get(status, 0)


def has_active_work(state: dict[str, Any]) -> bool:
    counts = queue_counts(state)
    return any(counts.get(status, 0) > 0 for status in ACTIVE_STATUSES)


def decide_scheduler_action(state: dict[str, Any]) -> WorkflowDecision:
    """Return the next mechanical action allowed by queue state.

    This helper deliberately does not inspect files, call an AI, or start work.
    It only answers which lane the scheduler is allowed to enter.
    """
    counts = queue_counts(state)
    if counts.get("queued", 0) > 0:
        return WorkflowDecision(
            action="start_or_continue_queued",
            reason="queued work exists",
            counts=counts,
        )
    if counts.get("running", 0) > 0:
        return WorkflowDecision(
            action="monitor_running",
            reason="remote work is still running",
            counts=counts,
        )
    return WorkflowDecision(
        action="discover_or_request_direction",
        reason="no queued or running work exists",
        counts=counts,
    )
