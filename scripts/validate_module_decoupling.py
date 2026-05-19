#!/usr/bin/env python3
"""Validate role boundaries between Hermes, scheduler, ideator, and runner."""

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

    hermes = read("scripts/hermes_quant_tick.py")
    scheduler = read("scripts/option_value_loop_daemon.py")
    ideator_entry = read("scripts/run_strategy_ideation_once.py")

    hermes_forbidden = [
        "run_strategy_ideation_once.py",
        "generate_option_value_next_spec.py",
        "IdeationCycle",
        "auto_research_pipeline.py",
        "rsync",
        "ssh ",
    ]
    for token in contains_any(hermes, hermes_forbidden):
        errors.append(f"scripts/hermes_quant_tick.py crosses role boundary via {token!r}")
    if "option_value_loop_daemon.py" not in hermes:
        errors.append("scripts/hermes_quant_tick.py must delegate mechanical progress to option_value_loop_daemon.py")

    scheduler_forbidden = [
        "run_strategy_ideation_once.py",
        "generate_option_value_next_spec.py",
        "IdeationCycle",
        "RegisteredProviderAdapter",
        "call_active_provider",
    ]
    for token in contains_any(scheduler, scheduler_forbidden):
        errors.append(f"scripts/option_value_loop_daemon.py crosses role boundary via {token!r}")
    if "decide_scheduler_action" not in scheduler:
        errors.append("scripts/option_value_loop_daemon.py must use shared workflow-state decision helper")

    ideator_forbidden = [
        "option_value_loop_daemon.py",
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
