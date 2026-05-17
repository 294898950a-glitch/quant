"""Validation for AI-produced strategy proposals."""

from __future__ import annotations

from typing import Any


PROPOSAL_REQUIRED_FIELDS = {
    "proposal_id",
    "strategy_id",
    "family",
    "hypothesis",
    "source_insight",
    "expected_improvement",
    "mechanics",
    "required_executor",
    "required_data",
    "test_design",
    "success_criteria",
    "falsifiers",
    "risk",
    "why_not_repeated_failure",
    "related_prior_runs",
    "implementation_assumption",
}


def validate_proposal(proposal: dict[str, Any], mechanics_vocab: set[str]) -> list[str]:
    errors: list[str] = []
    missing = sorted(PROPOSAL_REQUIRED_FIELDS - set(proposal))
    for field in missing:
        errors.append(f"missing required field {field}")
    if missing:
        return errors

    mechanics = proposal.get("mechanics")
    if not isinstance(mechanics, list) or not mechanics:
        errors.append("mechanics must be a non-empty list")
    else:
        for mechanic in mechanics:
            if not isinstance(mechanic, str):
                errors.append("mechanics entries must be strings")
            elif mechanic not in mechanics_vocab:
                errors.append(f"unknown mechanics tag {mechanic}")

    required_data = proposal.get("required_data")
    if not isinstance(required_data, list):
        errors.append("required_data must be a list")

    for field in ("test_design", "success_criteria", "falsifiers"):
        if not isinstance(proposal.get(field), dict):
            errors.append(f"{field} must be a mapping")

    falsifiers = proposal.get("falsifiers", {})
    if isinstance(falsifiers, dict):
        keys = " ".join(str(k).lower() for k in falsifiers)
        if "train" not in keys:
            errors.append("falsifiers must include train dimension")
        if "validate" not in keys and "validation" not in keys:
            errors.append("falsifiers must include validate dimension")
        if "test" not in keys and "year" not in keys and "dd" not in keys:
            errors.append("falsifiers must include test dimension")
    return errors
