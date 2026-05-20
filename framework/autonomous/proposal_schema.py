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


def validate_proposal(
    proposal: dict[str, Any],
    mechanics_vocab: set[str],
    capability_vocab: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    missing = sorted(PROPOSAL_REQUIRED_FIELDS - set(proposal))
    for field in missing:
        errors.append(f"missing required field {field}")
    if missing:
        return errors

    capability_ids = proposal.get("capability_ids")
    missing_capability_request = proposal.get("missing_capability_request")
    if capability_vocab is not None:
        has_ids = isinstance(capability_ids, list) and bool(capability_ids)
        has_missing_request = isinstance(missing_capability_request, (str, dict)) and bool(missing_capability_request)
        if not has_ids and not has_missing_request:
            errors.append("capability_ids must be a non-empty list unless missing_capability_request is provided")
        else:
            for cid in capability_ids or []:
                if not isinstance(cid, str):
                    errors.append("capability_ids entries must be strings")
                elif cid not in capability_vocab:
                    errors.append(f"unknown capability id {cid}")
    else:
        mechanics = proposal.get("mechanics")
        if not isinstance(mechanics, list) or not mechanics:
            errors.append("mechanics must be a non-empty list")
        else:
            for mechanic in mechanics:
                if not isinstance(mechanic, str):
                    errors.append("mechanics entries must be strings")
                elif mechanic not in mechanics_vocab:
                    errors.append(f"unknown mechanics tag {mechanic}")

    mechanics = proposal.get("mechanics")
    if mechanics is not None and (not isinstance(mechanics, list) or not mechanics):
        errors.append("mechanics must be a non-empty list when provided")
    else:
        for mechanic in mechanics or []:
            if not isinstance(mechanic, str):
                errors.append("mechanics entries must be strings")

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
