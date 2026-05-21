#!/usr/bin/env python3
"""Validate that quant automation uses only the registered AI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_STATE = REPO_ROOT / "data" / "research_framework" / "research_queue.yaml"
AI_PROVIDERS = REPO_ROOT / "data" / "research_framework" / "ai_providers.yaml"
STRATEGY_IDEATOR = REPO_ROOT / "data" / "research_framework" / "strategy_ideator.yaml"
COMMON_IDEATION_ENTRYPOINT = "scripts/run_strategy_ideation_once.py"
REVIEW_RESULT_ENTRYPOINT = "scripts/review_result.py"
DATA_QUALITY_ENTRYPOINT = "scripts/validate_data_quality.py"
DATA_REPAIR_ENTRYPOINT = "scripts/repair_data_quality.py"
ALLOWED_PROVIDER_ENTRYPOINTS = {
    COMMON_IDEATION_ENTRYPOINT,
    REVIEW_RESULT_ENTRYPOINT,
    DATA_QUALITY_ENTRYPOINT,
    DATA_REPAIR_ENTRYPOINT,
}
MECHANICAL_SCRIPTS = [
    REPO_ROOT / "scripts" / "research_queue_runner.py",
    REPO_ROOT / "scripts" / "quant_internal_tick.py",
    REPO_ROOT / "scripts" / "hermes_executor_handoff_tick.py",
    REPO_ROOT / "scripts" / "auto_research_pipeline.py",
]
DECOMMISSIONED_GENERATOR = REPO_ROOT / "scripts" / "generate_option_value_next_spec.py"
FORBIDDEN_TOKENS = [
    "claude --print",
    "--print",
    "chat.completions",
    "async_call_llm",
    "_call_claude",
]


def main() -> int:
    errors: list[str] = []
    state = yaml.safe_load(QUEUE_STATE.read_text(encoding="utf-8"))
    ideation = state.get("ideation") if isinstance(state, dict) else {}
    if not isinstance(ideation, dict) or ideation.get("enabled") is not False:
        errors.append("research_queue.yaml ideation.enabled must be false")
    if isinstance(ideation, dict) and str(ideation.get("command") or "") not in {"", "none"}:
        errors.append("research_queue.yaml ideation.command must be none")

    provider_registry = yaml.safe_load(AI_PROVIDERS.read_text(encoding="utf-8"))
    if not isinstance(provider_registry, dict):
        errors.append("ai_providers.yaml must be a YAML object")
        provider_registry = {}
    policy = provider_registry.get("policy") if isinstance(provider_registry, dict) else {}
    if not isinstance(policy, dict):
        errors.append("ai_providers.yaml policy must be a YAML object")
        policy = {}
    policy_allowed = set(str(value) for value in policy.get("allowed_entrypoints") or [])
    if policy_allowed != ALLOWED_PROVIDER_ENTRYPOINTS:
        errors.append(
            "ai_providers.yaml policy.allowed_entrypoints must be "
            f"{sorted(ALLOWED_PROVIDER_ENTRYPOINTS)}"
        )
    if policy.get("allowed_entrypoint") != COMMON_IDEATION_ENTRYPOINT:
        errors.append(f"ai_providers.yaml policy.allowed_entrypoint must remain {COMMON_IDEATION_ENTRYPOINT}")
    active = str(provider_registry.get("active_provider") or "")
    providers = provider_registry.get("providers") or {}
    if not isinstance(providers, dict) or active not in providers:
        errors.append(f"ai_providers.yaml active_provider {active!r} must be registered")
    elif isinstance(providers.get(active), dict):
        active_cfg = providers[active]
        if active_cfg.get("enabled") is not True:
            errors.append(f"active provider {active!r} must be enabled")
        active_allowed = set(str(value) for value in active_cfg.get("allowed_entrypoints") or [])
        if active_allowed != ALLOWED_PROVIDER_ENTRYPOINTS:
            errors.append(
                f"active provider {active!r} allowed_entrypoints must be "
                f"{sorted(ALLOWED_PROVIDER_ENTRYPOINTS)}"
            )
        if active_cfg.get("allowed_entrypoint") != COMMON_IDEATION_ENTRYPOINT:
            errors.append(f"active provider {active!r} allowed_entrypoint must remain {COMMON_IDEATION_ENTRYPOINT}")

    ideator = yaml.safe_load(STRATEGY_IDEATOR.read_text(encoding="utf-8"))
    if not isinstance(ideator, dict):
        errors.append("strategy_ideator.yaml must be a YAML object")
        ideator = {}
    if ideator.get("provider_registry") != "data/research_framework/ai_providers.yaml":
        errors.append("strategy_ideator.yaml must use data/research_framework/ai_providers.yaml")
    if ideator.get("allowed_entrypoint") != COMMON_IDEATION_ENTRYPOINT:
        errors.append(f"strategy_ideator.yaml allowed_entrypoint must be {COMMON_IDEATION_ENTRYPOINT}")

    for path in MECHANICAL_SCRIPTS:
        text = path.read_text(encoding="utf-8")
        if "generate_option_value_next_spec.py" in text:
            errors.append(f"{path.relative_to(REPO_ROOT)} must not call the next-spec generator")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                errors.append(f"{path.relative_to(REPO_ROOT)} contains forbidden LLM token: {token}")
        if COMMON_IDEATION_ENTRYPOINT in text and "strategy_ideation_once" not in text:
            errors.append(
                f"{path.relative_to(REPO_ROOT)} may call {COMMON_IDEATION_ENTRYPOINT} only "
                "with a project ideation ticket"
            )

    if DECOMMISSIONED_GENERATOR.exists():
        errors.append(
            "scripts/generate_option_value_next_spec.py is decommissioned; "
            "new specs must go through scripts/run_strategy_ideation_once.py"
        )

    if errors:
        print(f"validate_no_background_llm.py: {len(errors)} failure(s)")
        for error in errors:
            print(f"  FAIL {error}")
        return 1
    print("validate_no_background_llm.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
