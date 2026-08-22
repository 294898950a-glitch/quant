"""Deprecated compatibility protocol for the pre-v2 autonomous path.

DEPRECATED (2026-07-12): new strategy evaluation must use
``framework.evaluation.contract`` and ``framework.evaluation.validation``.
Keep this module only until the legacy adapter migration is complete; do not
add new verdict rules here.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_SCHEMA_VERSION = 1

# Explicit compatibility adapter: new strategy families do not inherit these
# historical assumptions silently.
CB_ARB_COMPAT_PROTOCOL: dict[str, Any] = {
    "schema_version": PROTOCOL_SCHEMA_VERSION,
    "mode": "legacy_cb_arb_compat",
    "primary_metric": {"path": "adoption_pass", "direction": "maximize"},
    "splits": {"holdout_periods": [2025, 2026]},
    "cost_model": {"required": True},
    "fail_closed": True,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_protocol(protocol: Any) -> list[str]:
    if not isinstance(protocol, dict):
        return ["evaluation_protocol must be a mapping"]
    errors: list[str] = []
    if protocol.get("schema_version") != PROTOCOL_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PROTOCOL_SCHEMA_VERSION}")
    primary = protocol.get("primary_metric")
    if not isinstance(primary, dict) or not isinstance(primary.get("path"), str) or not primary.get("path"):
        errors.append("primary_metric.path is required")
    elif primary.get("direction") not in {"maximize", "minimize"}:
        errors.append("primary_metric.direction must be maximize or minimize")
    splits = protocol.get("splits")
    if not isinstance(splits, dict):
        errors.append("splits must be a mapping")
    elif not isinstance(splits.get("holdout_periods", []), list):
        errors.append("splits.holdout_periods must be a list")
    cost = protocol.get("cost_model")
    if not isinstance(cost, dict) or not isinstance(cost.get("required"), bool):
        errors.append("cost_model.required must be boolean")
    elif cost.get("required") and (not isinstance(cost.get("metric_path"), str) or not cost.get("metric_path")):
        errors.append("cost_model.metric_path is required when cost_model.required is true")
    constraints = protocol.get("constraints", [])
    if not isinstance(constraints, list):
        errors.append("constraints must be a list")
    else:
        for index, rule in enumerate(constraints):
            if not isinstance(rule, dict):
                errors.append(f"constraints[{index}] must be a mapping")
                continue
            if not isinstance(rule.get("metric"), str) or not rule.get("metric"):
                errors.append(f"constraints[{index}].metric is required")
            if rule.get("op") not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                errors.append(f"constraints[{index}].op is invalid")
            if not _is_number(rule.get("value")):
                errors.append(f"constraints[{index}].value must be numeric")
    robustness = protocol.get("robustness_checks", [])
    if not isinstance(robustness, list):
        errors.append("robustness_checks must be a list")
    else:
        for index, rule in enumerate(robustness):
            if not isinstance(rule, dict) or not isinstance(rule.get("metric"), str) or not rule.get("metric"):
                errors.append(f"robustness_checks[{index}].metric is required")
            elif rule.get("op") not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                errors.append(f"robustness_checks[{index}].op is invalid")
            if isinstance(rule, dict) and not _is_number(rule.get("value")):
                errors.append(f"robustness_checks[{index}].value must be numeric")
    comparator = protocol.get("comparator")
    if comparator is not None:
        if not isinstance(comparator, dict):
            errors.append("comparator must be a mapping")
        else:
            for key in ("metric_path", "baseline_metric_path"):
                if not isinstance(comparator.get(key), str) or not comparator.get(key):
                    errors.append(f"comparator.{key} is required")
            if comparator.get("op") not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                errors.append("comparator.op is invalid")
    if not isinstance(protocol.get("fail_closed", True), bool):
        errors.append("fail_closed must be boolean")
    return errors


def protocol_for_proposal(proposal: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    explicit = proposal.get("evaluation_protocol")
    if explicit is not None:
        # Compatibility specs deliberately retain their historical boolean verdict
        # semantics and are handled by the legacy pipeline branch.
        if (
            isinstance(explicit, dict)
            and explicit.get("mode") == "legacy_cb_arb_compat"
            and str(proposal.get("strategy_id") or "").startswith("cb_arb")
        ):
            return dict(explicit), []
        errors = validate_protocol(explicit)
        return (dict(explicit), errors) if not errors else (None, errors)
    strategy_id = str(proposal.get("strategy_id") or "")
    if strategy_id.startswith("cb_arb"):
        return dict(CB_ARB_COMPAT_PROTOCOL), []
    return None, ["evaluation_protocol is required for non-cb_arb strategies"]


def metric_value(metrics: dict[str, Any], path: str) -> Any:
    value: Any = metrics
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def evaluate_constraints(protocol: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    for index, rule in enumerate(protocol.get("constraints", []) or []):
        path = str(rule["metric"])
        actual = metric_value(metrics, path)
        expected = rule["value"]
        op = rule["op"]
        passed = False
        if _is_number(actual):
            passed = {"eq": actual == expected, "ne": actual != expected, "gt": actual > expected,
                      "gte": actual >= expected, "lt": actual < expected, "lte": actual <= expected}[op]
        checks[f"constraint_{index}"] = {
            "metric": path, "actual": actual, "op": op, "expected": expected,
            "status": "passed" if passed else "failed",
            "reason": None if _is_number(actual) else ("missing" if actual is None else "non_numeric"),
        }
    for index, rule in enumerate(protocol.get("robustness_checks", []) or []):
        path = str(rule["metric"])
        actual = metric_value(metrics, path)
        expected = rule["value"]
        op = rule["op"]
        passed = _is_number(actual) and {"eq": actual == expected, "ne": actual != expected, "gt": actual > expected,
                                          "gte": actual >= expected, "lt": actual < expected, "lte": actual <= expected}[op]
        checks[f"robustness_{index}"] = {
            "metric": path, "actual": actual, "op": op, "expected": expected,
            "status": "passed" if passed else "failed",
            "reason": None if _is_number(actual) else ("missing" if actual is None else "non_numeric"),
        }
    primary_path = str(protocol["primary_metric"]["path"])
    primary = metric_value(metrics, primary_path)
    cost = protocol.get("cost_model") or {}
    if cost.get("required"):
        cost_path = str(cost.get("metric_path") or "")
        cost_value = metric_value(metrics, cost_path)
        checks["cost_model"] = {
            "metric": cost_path,
            "status": "passed" if _is_number(cost_value) else "failed",
            "reason": None if _is_number(cost_value) else ("missing" if cost_value is None else "non_numeric"),
        }
    comparator = protocol.get("comparator")
    if isinstance(comparator, dict):
        candidate = metric_value(metrics, str(comparator["metric_path"]))
        baseline = metric_value(metrics, str(comparator["baseline_metric_path"]))
        op = comparator["op"]
        passed = _is_number(candidate) and _is_number(baseline) and {
            "eq": candidate == baseline, "ne": candidate != baseline, "gt": candidate > baseline,
            "gte": candidate >= baseline, "lt": candidate < baseline, "lte": candidate <= baseline,
        }[op]
        checks["comparator"] = {
            "metric": comparator["metric_path"], "baseline_metric": comparator["baseline_metric_path"],
            "actual": candidate, "baseline": baseline, "op": op,
            "status": "passed" if passed else "failed",
            "reason": None if _is_number(candidate) and _is_number(baseline) else "missing_or_non_numeric",
        }
    if (primary is None or not _is_number(primary)) and protocol.get("fail_closed", True):
        checks["primary_metric"] = {
            "metric": primary_path,
            "status": "failed",
            "reason": "missing" if primary is None else "non_numeric",
        }
    passed = all(item.get("status") == "passed" for item in checks.values()) if checks else primary is not None
    return {"protocol_pass": passed, "protocol_checks": checks, "primary_metric": primary_path}
