#!/usr/bin/env python3
"""Validate role boundaries between internal cron, scheduler, ideator, and runner."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def contains_any(text: str, tokens: list[str]) -> list[str]:
    return [token for token in tokens if token in text]


def main() -> int:
    errors: list[str] = []

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
        "generate_option_value_next_spec.py",
        "IdeationCycle",
        "RegisteredProviderAdapter",
        "call_active_provider",
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
    if "run_strategy_ideation_once.py" in scheduler and "strategy_ideation_once" not in scheduler:
        errors.append(
            "scripts/research_queue_runner.py may call run_strategy_ideation_once.py only through "
            "the registered project ideation ticket"
        )

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
