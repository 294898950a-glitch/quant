"""Versioned contracts for first-principles strategy evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GateStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NOT_EVALUATED = "not_evaluated"
    WAIVED = "waived"


GATE_NAMES = ("integrity", "signal", "benchmark", "friction", "generalization", "constraints")
ALLOWED_SOURCES = {"train", "validation", "holdout", "rolling"}


@dataclass(frozen=True)
class GateSpec:
    name: str
    required: bool = True
    required_evidence_keys: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    required_sources: tuple[str, ...] = ()
    metric: str | None = None
    op: str | None = None
    value: float | None = None
    scenario: str | None = None


@dataclass(frozen=True)
class EvaluationPlan:
    schema_version: int
    plan_version: str
    mode: str
    required_gates: tuple[str, ...]
    gates: dict[str, GateSpec]
    artifact_requirements: tuple[dict[str, Any], ...] = ()
    multiple_testing: dict[str, Any] = field(default_factory=dict)
    robustness: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: GateStatus
    reason: str
    evidence_refs: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    required_sources_satisfied: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationReport:
    plan_version: str
    evaluated_at: str
    overall_status: GateStatus
    decision: str
    gates: dict[str, GateResult]
    evidence_refs: tuple[str, ...] = ()


def _gate_from_mapping(name: str, raw: Any) -> GateSpec:
    if not isinstance(raw, dict):
        return GateSpec(name=name)
    return GateSpec(
        name=name,
        required=bool(raw.get("required", True)),
        required_evidence_keys=tuple(str(x) for x in raw.get("required_evidence_keys", []) or []),
        allowed_sources=tuple(str(x) for x in raw.get("allowed_sources", []) or []),
        required_sources=tuple(str(x) for x in raw.get("required_sources", []) or []),
        metric=str(raw["metric"]) if raw.get("metric") else None,
        op=str(raw["op"]) if raw.get("op") else None,
        value=float(raw["value"]) if raw.get("value") is not None else None,
        scenario=str(raw["scenario"]) if raw.get("scenario") else None,
    )


def plan_from_mapping(raw: dict[str, Any]) -> EvaluationPlan:
    gates_raw = raw.get("gates") or {}
    gates = {name: _gate_from_mapping(name, gates_raw.get(name, {})) for name in GATE_NAMES}
    required = tuple(str(x) for x in raw.get("required_gates", []) or [])
    return EvaluationPlan(
        schema_version=int(raw.get("schema_version", 1)),
        plan_version=str(raw.get("plan_version", raw.get("schema_version", "1"))),
        mode=str(raw.get("mode", "declared")),
        required_gates=required,
        gates=gates,
        artifact_requirements=tuple(raw.get("artifact_requirements", []) or []),
        multiple_testing=dict(raw.get("multiple_testing") or {}),
        robustness=dict(raw.get("robustness") or {}),
    )


def validate_plan_complete(plan: EvaluationPlan | dict[str, Any]) -> list[str]:
    if isinstance(plan, dict):
        try:
            plan = plan_from_mapping(plan)
        except (TypeError, ValueError) as exc:
            return [f"invalid evaluation plan: {exc}"]
    errors: list[str] = []
    if plan.schema_version != 1:
        errors.append("schema_version must be 1")
    if not plan.plan_version:
        errors.append("plan_version is required")
    if plan.mode not in {"declared", "legacy_cb_arb_compat"}:
        errors.append("mode must be declared or legacy_cb_arb_compat")
    if "integrity" not in plan.required_gates:
        errors.append("integrity must be a required gate")
    unknown = set(plan.required_gates) - set(GATE_NAMES)
    if unknown:
        errors.append(f"unknown required gates: {sorted(unknown)}")
    for name, gate in plan.gates.items():
        if name not in GATE_NAMES:
            errors.append(f"unknown gate: {name}")
        if not gate.required_evidence_keys:
            errors.append(f"{name}.required_evidence_keys is required")
        if not set(gate.required_sources).issubset(set(gate.allowed_sources)):
            errors.append(f"{name}.required_sources must be a subset of allowed_sources")
        bad_sources = set(gate.allowed_sources) - ALLOWED_SOURCES
        if bad_sources:
            errors.append(f"{name}.allowed_sources has invalid values: {sorted(bad_sources)}")
    for item in plan.artifact_requirements:
        if not isinstance(item, dict) or not item.get("id") or not (item.get("path") or item.get("evidence_ref")):
            errors.append("artifact_requirements entries need id and path/evidence_ref")
    return errors
