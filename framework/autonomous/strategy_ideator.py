"""AI-backed strategy proposal producer.

The ideator writes proposals only. It never writes runnable specs and never
changes current strategy or baseline truth.
"""

from __future__ import annotations

import json
from typing import Any

import yaml


def propose(
    closed_tags: dict[str, Any],
    recent_digest: dict[str, Any],
    insights: dict[str, Any],
    budget_cap: float,
    ai_adapter,
) -> dict[str, Any]:
    prompt = (
        "Produce one strategy_proposal.yaml object. Avoid these closed_tags: "
        f"{sorted(closed_tags)}. Budget cap CNY: {budget_cap}. "
        f"Recent digest: {recent_digest}. Insights: {insights}."
    )
    response = ai_adapter.call_active_provider(prompt, schema={"type": "strategy_proposal"})
    try:
        proposal = json.loads(response.content)
    except json.JSONDecodeError:
        loaded = yaml.safe_load(response.content)
        proposal = loaded if isinstance(loaded, dict) else {"proposal_id": "unparseable", "mechanics": []}
    if isinstance(proposal.get("proposal"), dict):
        proposal = proposal["proposal"]
    closed = set(closed_tags)
    proposal["mechanics"] = [
        mechanic for mechanic in _normalise_mechanics(proposal.get("mechanics", [])) if mechanic not in closed
    ]
    proposal["ai_provider"] = getattr(response, "provider_id", None)
    proposal["prompt_path"] = "framework/autonomous/strategy_ideator.py"
    proposal["response_hash"] = getattr(response, "response_hash", None)
    proposal["provenance"] = {
        "ai_provider": proposal["ai_provider"],
        "prompt_path": proposal["prompt_path"],
        "response_hash": proposal["response_hash"],
    }
    return proposal


def _normalise_mechanics(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    mechanics: list[str] = []
    for item in items:
        if isinstance(item, str):
            mechanics.append(item)
        elif isinstance(item, dict):
            for key in ("tag", "name", "id", "mechanic", "mechanics"):
                candidate = item.get(key)
                if isinstance(candidate, str):
                    mechanics.append(candidate)
                    break
    return mechanics
