"""Typed evidence handoff between measurement and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    key: str
    value: Any
    source: str
    plan_version: str
    scenario: str | None = None
    method: str | None = None
    applied_method: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""
    evidence_ref: str | None = None


def measurement_evidence(
    *, id: str, key: str, value: Any, source: str, plan_version: str,
    scenario: str | None = None, method: str | None = None,
    applied_method: str | None = None, parameters: dict[str, Any] | None = None,
    evidence_ref: str | None = None,
) -> EvidenceItem:
    """Wrap a measured value; the caller owns the source label."""
    if not id or not key or not source or not plan_version:
        raise ValueError("evidence id, key, source, and plan_version are required")
    return EvidenceItem(
        id=id, key=key, value=value, source=source, plan_version=plan_version,
        scenario=scenario, method=method, applied_method=applied_method,
        parameters=dict(parameters or {}),
        computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        evidence_ref=evidence_ref,
    )
