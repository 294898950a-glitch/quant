"""Prompt contracts injected into autonomous AI calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_REL_PATH = Path("data/research_framework/ai_prompt_contracts.yaml")

DEFAULT_CONTRACT: dict[str, Any] = {
    "schema_version": 1,
    "global_strong_prompt": [
        "Read every required_reading_cards entry before answering.",
        "Treat required_reading_cards as binding runtime facts, not optional context.",
        "Return only the requested machine-readable object.",
        "If a required condition cannot be satisfied, return the requested failure/draft object rather than pretending it is runnable.",
    ],
    "required_reading_cards": {
        "workflow_boundaries": {
            "purpose": "Keep every role inside its node boundary.",
            "facts": [
                "The active nodes are state_and_rules, ideation, proposal_gate, runner, and review_memory.",
                "Ideation proposes; proposal_gate validates and matches; runner executes; review_memory records and summarizes.",
                "AI must not start VMs, mark strategies live, change current truth, or bypass the runner.",
            ],
        },
        "capability_executor_policy": {
            "purpose": "Prevent ideas from being treated as runnable without a matching execution path.",
            "facts": [
                "A READY proposal needs registered capability_ids and a strict executor match.",
                "If no capability id fits, use missing_capability_request.",
                "If no executor fits, produce a DRAFT/tool request instead of claiming READY.",
                "Never invent or misspell capability_ids; use only the injected menu.",
            ],
        },
        "failure_loop_policy": {
            "purpose": "Bound retry loops and record why a direction failed.",
            "facts": [
                "A failed proposal or tool output must be repaired from the concrete validation errors.",
                "After repeated failure, abandon the direction instead of rephrasing it forever.",
                "Do not repeat closed, forbidden, or exhausted families.",
            ],
        },
        "artifact_contract": {
            "purpose": "Ensure generated executors can be reviewed.",
            "facts": [
                "Runnable executors must write summary.json, report.yaml, l4_ack.yaml, and diagnostic.yaml.",
                "summary.json must contain adoption_pass.",
                "Generated Python must define main() and declare_data_requirements(command, spec).",
                "Do not write placeholder, TODO, pseudocode, or demo-only implementations.",
            ],
        },
        "data_boundary": {
            "purpose": "Keep data checks and repairs separate from strategy claims.",
            "facts": [
                "Data quality must pass before execution.",
                "Generated tools must declare all required input files.",
                "Raw warehouse data must not be overwritten by generated tools.",
            ],
        },
    },
    "roles": {
        "strategy_ideation": {
            "strong_prompt": [
                "Propose exactly one research direction.",
                "Use capability_ids only when they are present in the injected capability_menu.",
                "If the idea needs a new executor, keep the proposal testable and let the compiler produce DRAFT.",
            ],
            "required_cards": [
                "workflow_boundaries",
                "capability_executor_policy",
                "failure_loop_policy",
            ],
        },
        "executor_tool_design": {
            "strong_prompt": [
                "Design only the missing executor metadata and implementation plan.",
                "Do not write code in the design step.",
                "Explain why existing executors are insufficient.",
            ],
            "required_cards": [
                "workflow_boundaries",
                "capability_executor_policy",
                "artifact_contract",
                "data_boundary",
            ],
        },
        "executor_tool_code": {
            "strong_prompt": [
                "Write complete executable Python only inside files[*].content.",
                "Fix previous validation errors directly.",
                "Do not return placeholder or demo-only code.",
            ],
            "required_cards": [
                "workflow_boundaries",
                "artifact_contract",
                "data_boundary",
                "failure_loop_policy",
            ],
        },
    },
}


def _merge_contract(loaded: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONTRACT)
    for key, value in loaded.items():
        if key in {"required_reading_cards", "roles"} and isinstance(value, dict):
            base = dict(merged.get(key) or {})
            base.update(value)
            merged[key] = base
        else:
            merged[key] = value
    return merged


def load_prompt_contracts(repo_root: Path | str = Path("."), path: Path | str | None = None) -> dict[str, Any]:
    root = Path(repo_root)
    contract_path = Path(path) if path is not None else root / DEFAULT_REL_PATH
    if not contract_path.exists():
        return dict(DEFAULT_CONTRACT)
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        return dict(DEFAULT_CONTRACT)
    return _merge_contract(loaded)


def prompt_contract_for_role(role: str, repo_root: Path | str = Path(".")) -> dict[str, Any]:
    contract = load_prompt_contracts(repo_root)
    roles = contract.get("roles") if isinstance(contract.get("roles"), dict) else {}
    role_cfg = roles.get(role) if isinstance(roles.get(role), dict) else {}
    cards = contract.get("required_reading_cards") if isinstance(contract.get("required_reading_cards"), dict) else {}
    card_ids = [str(item) for item in role_cfg.get("required_cards", []) or []]
    selected_cards = {card_id: cards[card_id] for card_id in card_ids if card_id in cards}
    return {
        "role": role,
        "strong_prompt": list(contract.get("global_strong_prompt") or []) + list(role_cfg.get("strong_prompt") or []),
        "required_card_ids": card_ids,
        "required_reading_cards": selected_cards,
        "contract_path": str(DEFAULT_REL_PATH),
    }
