#!/usr/bin/env python3
"""Validate the autonomous research architecture stays pruned to 5 nodes."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
ARCH_PATH = REPO_ROOT / "data" / "research_framework" / "autonomous_research_acceptance_criteria.yaml"
EXPECTED = {"state_and_rules", "ideation", "proposal_gate", "runner", "review_memory"}
FORBIDDEN_ACTIVE_BUDGET_TOKENS = {
    "budgets",
    "budget gate",
    "budget,",
    "validate_compute_budget",
}
DECOMMISSIONED_FILES = {
    "scripts/estimate_compute_budget.py": "standalone budget estimator is decommissioned; compute estimates are record-only metadata in specs",
    "data/research_framework/compute_budget_config.json": "standalone budget estimator config is decommissioned",
    "scripts/validate_compute_budget.py": "compute budget validation is decommissioned; budget is metadata only",
    "scripts/validate_current_md.py": "current state validator must use the YAML name",
    "scripts/snapshot_current_state.py": "markdown current-state snapshot is decommissioned",
    "scripts/generate_indexes.py": "markdown index generation is decommissioned",
    "scripts/evaluate_cb_arb_legacy.py": "legacy relative-rank evaluation entry is decommissioned",
    "scripts/process_quant_claude_outbox.py": "old direct Claude outbox processor is decommissioned; project-owned internal tick is the scheduled entry",
    "scripts/check_quant_outbox_misroute.py": "old outbox routing checker is decommissioned with direct outbox polling",
    "scripts/outbox_protocol_preflight.py": "old outbox protocol preflight is decommissioned; runtime rules are YAML-owned",
    "scripts/research_select_next.py": "old markdown-ledger selector is decommissioned; ideation reads YAML runtime state",
    "scripts/research_arxiv_first_run.py": "old markdown paper-candidate generator is decommissioned",
    "scripts/recover_cb_arb_reflog.py": "one-off recovery helper is decommissioned",
    "scripts/migrate_specs_to_yaml.py": "one-off markdown spec migration helper is decommissioned",
    "scripts/backfill_run_manifests.py": "one-off run manifest backfill helper is decommissioned",
    "scripts/research_memory.py": "old runs.jsonl helper is decommissioned; review memory is YAML/artifact based",
    "scripts/monitor_cb_arb_concurrent.py": "old batch monitor is decommissioned; runner settles remote results on tick",
    "scripts/monitor_cb_arb_holdout_progress.py": "old holdout monitor is decommissioned; runner settles remote results on tick",
    "scripts/watch_quant_vm_task_completion.sh": "local watcher is decommissioned; runner settles remote results on tick",
    "scripts/option_value_loop_daemon.py": "old option-value runner name is decommissioned; use scripts/research_queue_runner.py",
    "data/research_framework/option_value_loop.yaml": "old option-value queue state name is decommissioned; use data/research_framework/research_queue.yaml",
    "scripts/option_value_progress_reporter.py": "old progress heartbeat reporter is decommissioned; internal tick reports queue status",
    "scripts/option_value_loop_ctl.sh": "old runner control shell is decommissioned; internal tick issues tickets and calls the queue runner directly",
    "scripts/install_option_value_loop_autostart.sh": "old runner autostart installer is decommissioned; no direct daemon startup is allowed",
    "scripts/install_option_value_loop_cron.sh": "old runner cron installer is decommissioned; project-owned internal tick is the scheduled entry",
    "scripts/option_value_progress_loop_runner.sh": "separate progress loop is decommissioned; internal tick reports state",
    "scripts/option_value_progress_loop_ctl.sh": "separate progress control shell is decommissioned; internal tick reports state",
    "scripts/install_option_value_progress_cron.sh": "separate progress cron is decommissioned; internal tick reports state",
    "scripts/watch_quant_claude_processor.sh": "Claude outbox watcher is decommissioned; autonomous progress must enter through the project-owned internal tick",
    "scripts/framework_watch_daemon.py": "real-time framework watcher is decommissioned; validation runs through explicit checks and preflight",
    "scripts/framework_watch_ctl.sh": "real-time framework watcher control script is decommissioned",
    "scripts/install_framework_watch_autostart.sh": "real-time framework watcher autostart is decommissioned",
    "framework/result_reviewer.py": "duplicate top-level reviewer is decommissioned; review memory lives under framework/autonomous",
    "framework/digest.py": "duplicate top-level digest is decommissioned; review memory lives under framework/autonomous",
    "framework/autonomous/orchestrator.py": "legacy orchestrator shell is decommissioned; runner owns scheduling",
    "framework/_alias.py": "framework alias bridge is decommissioned",
    "framework/auditor.py": "framework alias shell is decommissioned",
    "framework/benchmarks.py": "framework alias shell is decommissioned",
    "framework/bootstrap.py": "framework alias shell is decommissioned",
    "framework/editor.py": "framework alias shell is decommissioned",
    "framework/evaluator.py": "framework alias shell is decommissioned",
    "framework/holdout.py": "framework alias shell is decommissioned",
    "framework/holdout_splitter.py": "framework alias shell is decommissioned",
    "framework/hypothesizer.py": "framework alias shell is decommissioned",
    "framework/judge.py": "framework alias shell is decommissioned",
    "framework/llm_queue.py": "framework alias shell is decommissioned",
    "framework/memory.py": "framework alias shell is decommissioned",
    "framework/orchestrator.py": "framework alias shell is decommissioned",
    "framework/pool_stats.py": "framework alias shell is decommissioned",
    "framework/result_types.py": "framework alias shell is decommissioned",
    "framework/sanity_checker.py": "framework alias shell is decommissioned",
}


def main() -> int:
    errors: list[str] = []
    data = yaml.safe_load(ARCH_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        errors.append("architecture file root must be a mapping")
        data = {}

    components = data.get("active_components")
    if not isinstance(components, list):
        errors.append("active_components must be a list")
        components = []
    ids = [str(item.get("id")) for item in components if isinstance(item, dict)]
    if len(ids) > 5:
        errors.append(f"active architecture has {len(ids)} nodes; max is 5")
    missing = sorted(EXPECTED - set(ids))
    extra = sorted(set(ids) - EXPECTED)
    if missing:
        errors.append(f"missing active architecture nodes: {missing}")
    if extra:
        errors.append(f"unexpected active architecture nodes: {extra}")
    if "components" in data:
        errors.append("legacy components list is not allowed; use active_components")

    for rel_path, reason in DECOMMISSIONED_FILES.items():
        if (REPO_ROOT / rel_path).exists():
            errors.append(f"{rel_path} must not exist: {reason}")

    acceptance = data.get("acceptance") if isinstance(data, dict) else {}
    if not isinstance(acceptance, dict):
        errors.append("acceptance must be a mapping")
    elif int(acceptance.get("max_active_components") or 0) != 5:
        errors.append("acceptance.max_active_components must be 5")

    for component in components:
        if not isinstance(component, dict):
            continue
        for field in ("owns", "files", "must", "must_not"):
            value = component.get(field)
            if not isinstance(value, list) or not value:
                errors.append(f"component {component.get('id')} missing non-empty {field}")
                continue
            joined = "\n".join(str(item).lower() for item in value)
            for token in FORBIDDEN_ACTIVE_BUDGET_TOKENS:
                if token in joined:
                    errors.append(
                        f"component {component.get('id')} keeps compute budget as active architecture content: {token}"
                    )

    if acceptance.get("compute_budget_is_not_active_gate") is not True:
        errors.append("acceptance.compute_budget_is_not_active_gate must be true")

    if errors:
        print(f"validate_architecture_pruned.py: {len(errors)} failure(s)")
        for error in errors:
            print(f"  FAIL {error}")
        return 1
    print("validate_architecture_pruned.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
