"""Bounded proposal rewrite loop."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import yaml


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:yaml|yml|json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else stripped


def _parse_mapping_response(content: str) -> dict[str, Any]:
    cleaned = _strip_markdown_fence(content)
    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            loaded = yaml.safe_load(cleaned)
        except yaml.YAMLError:
            loaded = None
    return loaded if isinstance(loaded, dict) else {"proposal_id": "invalid_response"}


class RewriteResult:
    def __init__(
        self,
        final_proposal: dict[str, Any],
        status: str,
        rounds_used: int,
        last_errors: list[str],
        provenance: list[dict[str, Any]] | None = None,
    ):
        self.final_proposal = final_proposal
        self.status = status
        self.rounds_used = rounds_used
        self.last_errors = last_errors
        self.provenance = provenance or []


def rewrite_until_valid(
    initial_proposal: dict[str, Any],
    validator: Callable[[dict[str, Any]], list[str]],
    ai_adapter,
    max_rounds: int = 3,
    context: dict[str, Any] | None = None,
) -> RewriteResult:
    proposal = dict(initial_proposal)
    errors = validator(proposal)
    if not errors:
        return RewriteResult(proposal, "valid", 0, [], [])

    provenance: list[dict[str, Any]] = []
    for round_num in range(1, max_rounds + 1):
        prompt = yaml.safe_dump(
            {
                "task": "Rewrite the strategy proposal to fix the validation errors.",
                "rules": [
                    "Return only one YAML or JSON object.",
                    "Do not include markdown fences or explanation.",
                    "Preserve the strategy intent when possible.",
                    "Use only allowed capability_ids if provided. Do not hand-write capability names.",
                    "Do not use closed tags if provided.",
                ],
                "validation_errors": errors,
                "current_proposal": proposal,
                "context": context or {},
            },
            allow_unicode=True,
            sort_keys=False,
        )
        response = ai_adapter.call_active_provider(prompt, schema={"type": "strategy_proposal"})
        provenance.append({
            "round": round_num,
            "provider_id": getattr(response, "provider_id", None),
            "response_hash": getattr(response, "response_hash", None),
        })
        proposal = _parse_mapping_response(response.content)
        errors = validator(proposal)
        if not errors:
            return RewriteResult(proposal, "valid", round_num, [], provenance)
    return RewriteResult(proposal, "exhausted", max_rounds, errors, provenance)
