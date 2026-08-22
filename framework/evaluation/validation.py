"""Fail-closed evaluation gates; measurement modules never decide PASS."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any, Iterable

from .contract import EvaluationPlan, EvaluationReport, GateResult, GateSpec, GateStatus, plan_from_mapping, validate_plan_complete
from .evidence import EvidenceItem


def _compare(actual: Any, op: str | None, expected: float | None) -> bool:
    if not isinstance(actual, Number) or isinstance(actual, bool) or op is None or expected is None:
        return False
    return {"eq": actual == expected, "ne": actual != expected, "gt": actual > expected,
            "gte": actual >= expected, "lt": actual < expected, "lte": actual <= expected}.get(op, False)


def _evidence_map(evidence: Iterable[EvidenceItem | dict[str, Any]]) -> dict[str, list[EvidenceItem]]:
    result: dict[str, list[EvidenceItem]] = {}
    for item in evidence:
        if isinstance(item, dict):
            item = EvidenceItem(**item)
        result.setdefault(item.key, []).append(item)
    return result


def _artifact_ok(plan: EvaluationPlan) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for item in plan.artifact_requirements:
        path = item.get("path") if isinstance(item, dict) else None
        if path and not Path(str(path)).exists():
            missing.append(str(item.get("id", path)))
    return not missing, missing


def _gate_result(plan: EvaluationPlan, gate: GateSpec, items: dict[str, list[EvidenceItem]]) -> GateResult:
    refs: list[str] = []
    selected: list[EvidenceItem] = []
    for key in gate.required_evidence_keys:
        candidates = items.get(key, [])
        valid = [item for item in candidates if item.plan_version == plan.plan_version and item.source in set(gate.allowed_sources)]
        if gate.scenario is not None:
            valid = [item for item in valid if item.scenario == gate.scenario]
        if not valid:
            return GateResult(gate.name, GateStatus.FAILED if gate.required else GateStatus.FAILED,
                              f"missing or invalid evidence: {key}", tuple(refs), details={"missing_key": key})
        selected.extend(valid)
        refs.extend(item.evidence_ref or item.id for item in valid)
    if gate.metric:
        metric_items = items.get(gate.metric, selected)
        if not metric_items or not any(_compare(item.value, gate.op, gate.value) for item in metric_items):
            if not gate.required and selected:
                return GateResult(gate.name, GateStatus.WAIVED, "optional threshold not met", tuple(refs))
            return GateResult(gate.name, GateStatus.FAILED, "declared threshold not met", tuple(refs))
    return GateResult(gate.name, GateStatus.PASSED, "all declared evidence checks passed", tuple(refs), True)


def evaluate_plan(plan: EvaluationPlan | dict[str, Any], evidence: Iterable[EvidenceItem | dict[str, Any]]) -> EvaluationReport:
    if isinstance(plan, dict):
        plan = plan_from_mapping(plan)
    errors = validate_plan_complete(plan)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if errors:
        gates = {name: GateResult(name, GateStatus.FAILED, "invalid evaluation plan", details={"errors": errors}) for name in plan.gates}
        return EvaluationReport(plan.plan_version, now, GateStatus.FAILED, "evaluation_protocol_invalid", gates)
    items = _evidence_map(evidence)
    artifact_ok, missing_artifacts = _artifact_ok(plan)
    integrity = _gate_result(plan, plan.gates["integrity"], items)
    if not artifact_ok:
        integrity = GateResult("integrity", GateStatus.FAILED, "required artifact missing", details={"missing": missing_artifacts})
    gates: dict[str, GateResult] = {"integrity": integrity}
    if integrity.status != GateStatus.PASSED:
        for name in plan.required_gates:
            if name != "integrity":
                gates[name] = GateResult(name, GateStatus.BLOCKED, "integrity prerequisite failed", blocked_by=("integrity",))
    else:
        for name, gate in plan.gates.items():
            if name != "integrity":
                gates[name] = _gate_result(plan, gate, items)
    required_failed = any(gates[name].status != GateStatus.PASSED for name in plan.required_gates)
    overall = GateStatus.FAILED if required_failed else GateStatus.PASSED
    decision = "evaluation_protocol_invalid" if integrity.status != GateStatus.PASSED else ("failed_declared_protocol" if required_failed else "passed_declared_protocol")
    refs = tuple(ref for result in gates.values() for ref in result.evidence_refs)
    return EvaluationReport(plan.plan_version, now, overall, decision, gates, refs)
