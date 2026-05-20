"""AI-backed strategy proposal producer.

The ideator writes proposals only. It never writes runnable specs and never
changes current strategy or baseline truth.
"""

from __future__ import annotations

import json
import re
from typing import Any

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
    return loaded if isinstance(loaded, dict) else {"proposal_id": "unparseable", "capability_ids": []}


def propose(
    closed_tags: dict[str, Any],
    recent_digest: dict[str, Any],
    insights: dict[str, Any],
    ai_adapter,
) -> dict[str, Any]:
    prompt = (
        "You are the ideation node in a five-node quant research workflow.\n"
        "Your only job is to propose the next research question. You do not run tests, "
        "write files, start VMs, update current strategy truth, promote results, or review outcomes.\n\n"
        "You must follow this order:\n"
        "1. Read the current research context, recent result digest, closed directions, available evidence, "
        "and available executor/tool information.\n"
        "2. Identify one unresolved problem that is supported by the provided evidence.\n"
        "3. Propose exactly one testable strategy idea for that problem.\n"
        "4. Choose capability_ids only from insights.capability_menu. Never hand-write a capability name. "
        "Use the menu meaning, available executors, and required data to choose. "
        "If none can test it, still propose "
        "the idea with missing_capability_request so the compiler can mark it DRAFT.\n"
        "5. Define success criteria and falsifiers before any run happens.\n"
        "6. Explain why this is not a renamed repeat of a failed or closed direction.\n\n"
        "Hard boundaries:\n"
        "- Return exactly one strategy_proposal YAML or JSON object and nothing else.\n"
        "- Do not use a capability whose resolved mechanic is in closed_tags.\n"
        "- Do not use any mechanic or direction in closed_tags.\n"
        "- Do not ask for a budget decision; compute budget is not a proposal gate.\n"
        "- Do not invent runnable capability. A READY proposal must match existing capability_ids and an existing executor.\n"
        "- If you output an unknown or misspelled capability id, validation will fail and you must rewrite.\n"
        "- If no capability_menu entry fits, include missing_capability_request instead of inventing an id.\n"
        "- Do not change the current strategy, baseline, protocol, or live status.\n"
        "- Do not request local cb_arb backtests.\n\n"
        "Required output fields are listed in insights.required_fields. Fill every field concretely.\n"
        f"closed_tags: {sorted(closed_tags)}\n"
        f"recent_digest: {recent_digest}\n"
        f"insights: {insights}\n"
    )
    response = ai_adapter.call_active_provider(prompt, schema={"type": "strategy_proposal"})
    proposal = _parse_mapping_response(response.content)
    if isinstance(proposal.get("proposal"), dict):
        proposal = proposal["proposal"]
    proposal["capability_ids"] = _normalise_ids(proposal.get("capability_ids", []))
    if "mechanics" in proposal:
        proposal["mechanics"] = _normalise_ids(proposal.get("mechanics", []))
    proposal["ai_provider"] = getattr(response, "provider_id", None)
    proposal["prompt_path"] = "framework/autonomous/strategy_ideator.py"
    proposal["response_hash"] = getattr(response, "response_hash", None)
    proposal["provenance"] = {
        "ai_provider": proposal["ai_provider"],
        "prompt_path": proposal["prompt_path"],
        "response_hash": proposal["response_hash"],
    }
    return proposal


def _normalise_ids(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in items:
        if isinstance(item, str):
            output.append(item)
        elif isinstance(item, dict):
            for key in ("id", "capability_id", "tag", "name", "mechanic", "mechanics"):
                candidate = item.get(key)
                if isinstance(candidate, str):
                    output.append(candidate)
                    break
    return output
