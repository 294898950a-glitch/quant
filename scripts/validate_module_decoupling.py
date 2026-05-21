#!/usr/bin/env python3
"""Validate role boundaries between internal cron, scheduler, ideator, and runner."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def contains_any(text: str, tokens: list[str]) -> list[str]:
    return [token for token in tokens if token in text]


def main() -> int:
    errors: list[str] = []

    declared = yaml.safe_load(read("data/research_framework/autonomous_research_acceptance_criteria.yaml"))
    components = declared.get("active_components") if isinstance(declared, dict) else []
    component_files: dict[str, set[str]] = {}
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            component_id = str(component.get("id") or "")
            files = component.get("files") if isinstance(component.get("files"), list) else []
            component_files[component_id] = {str(path) for path in files}
    required_declarations = {
        "ideation": {"framework/autonomous/queue_ideation.py"},
        "runner": {"scripts/research_queue_runner.py", "framework/autonomous/queue_remote_execution.py"},
        "review_memory": {"framework/autonomous/queue_review_memory.py"},
    }
    for component_id, paths in required_declarations.items():
        missing = paths - component_files.get(component_id, set())
        for path in sorted(missing):
            errors.append(f"architecture declaration missing {path!r} under component {component_id!r}")

    internal_tick = read("scripts/quant_internal_tick.py")
    scheduler = read("scripts/research_queue_runner.py")
    ideator_entry = read("scripts/run_strategy_ideation_once.py")

    internal_tick_forbidden = [
        "run_strategy_ideation_once.py",
        "generate_option_value_next_spec.py",
        "IdeationCycle",
        "auto_research_pipeline.py",
        "rsync",
        "ssh ",
    ]
    for token in contains_any(internal_tick, internal_tick_forbidden):
        errors.append(f"scripts/quant_internal_tick.py crosses role boundary via {token!r}")
    if "research_queue_runner.py" not in internal_tick:
        errors.append("scripts/quant_internal_tick.py must delegate mechanical progress to research_queue_runner.py")

    scheduler_forbidden = [
        "run_strategy_ideation_once.py",
        "generate_option_value_next_spec.py",
        "IdeationCycle",
        "RegisteredProviderAdapter",
        "call_active_provider",
        "executor_tool_response_invalid",
        "proposal_status",
        "auto_research_pipeline.py",
        "validate_data_quality.py",
        "repair_data_quality.py",
        "rsync",
        "ssh ",
        "--no-ideation",
        "allow_ideation",
        "daemon_loop",
        "PID_PATH",
        "RESTART_LOG_PATH",
        "watch_quant_vm_task_completion.sh",
        "subprocess.Popen",
        "discover_ready_specs",
        "auto_discover_ready_specs",
        "spec_matches_discovery",
    ]
    for token in contains_any(scheduler, scheduler_forbidden):
        errors.append(f"scripts/research_queue_runner.py crosses role boundary via {token!r}")
    if "decide_scheduler_action" not in scheduler:
        errors.append("scripts/research_queue_runner.py must use shared workflow-state decision helper")
    if "QueueIdeationService" not in scheduler:
        errors.append("scripts/research_queue_runner.py must delegate ideation to QueueIdeationService")
    if "QueueRemoteExecutionService" not in scheduler:
        errors.append("scripts/research_queue_runner.py must delegate VM work to QueueRemoteExecutionService")
    if "QueueReviewMemoryService" not in scheduler:
        errors.append("scripts/research_queue_runner.py must delegate review work to QueueReviewMemoryService")

    ideator_forbidden = [
        "research_queue_runner.py",
        "auto_research_pipeline.py",
        "watch_quant_vm_task_completion.sh",
        "rsync",
        "ssh ",
        "subprocess.Popen",
    ]
    for token in contains_any(ideator_entry, ideator_forbidden):
        errors.append(f"scripts/run_strategy_ideation_once.py crosses role boundary via {token!r}")

    queue_ideation = read("framework/autonomous/queue_ideation.py")
    queue_ideation_forbidden = ["rsync", "ssh ", "auto_research_pipeline.py"]
    for token in contains_any(queue_ideation, queue_ideation_forbidden):
        errors.append(f"framework/autonomous/queue_ideation.py crosses role boundary via {token!r}")
    if "run_strategy_ideation_once.py" not in queue_ideation:
        errors.append("framework/autonomous/queue_ideation.py must own the strategy ideation subprocess boundary")

    queue_remote_execution = read("framework/autonomous/queue_remote_execution.py")
    queue_remote_forbidden = [
        "run_strategy_ideation_once.py",
        "generate_option_value_next_spec.py",
        "IdeationCycle",
        "RegisteredProviderAdapter",
        "call_active_provider",
        "executor_tool_response_invalid",
        "proposal_status",
        "review_result.py",
        "build_recent_results_digest.py",
    ]
    for token in contains_any(queue_remote_execution, queue_remote_forbidden):
        errors.append(f"framework/autonomous/queue_remote_execution.py crosses role boundary via {token!r}")
    for token in ("auto_research_pipeline.py", "validate_data_quality.py", "repair_data_quality.py", "rsync", "ssh "):
        if token not in queue_remote_execution:
            errors.append(f"framework/autonomous/queue_remote_execution.py must own remote execution token {token!r}")

    queue_review_memory = read("framework/autonomous/queue_review_memory.py")
    queue_review_forbidden = [
        "run_strategy_ideation_once.py",
        "auto_research_pipeline.py",
        "validate_data_quality.py",
        "repair_data_quality.py",
        "rsync",
        "ssh ",
        "call_active_provider",
    ]
    for token in contains_any(queue_review_memory, queue_review_forbidden):
        errors.append(f"framework/autonomous/queue_review_memory.py crosses role boundary via {token!r}")
    for token in ("review_result.py", "build_recent_results_digest.py"):
        if token not in queue_review_memory:
            errors.append(f"framework/autonomous/queue_review_memory.py must own review token {token!r}")

    workflow = read("framework/autonomous/workflow_state.py")
    workflow_forbidden = ["yaml", "subprocess", "rsync", "ssh", "claude", "openai", "deepseek"]
    for token in contains_any(workflow.lower(), workflow_forbidden):
        errors.append(f"framework/autonomous/workflow_state.py must stay pure state logic; found {token!r}")

    if errors:
        print(f"validate_module_decoupling.py: {len(errors)} failure(s)")
        for error in errors:
            print(f"  FAIL {error}")
        return 1
    print("validate_module_decoupling.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
