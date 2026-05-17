"""Coordinator shell for autonomous research cycles."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PAUSE_FLAG_PATH = Path("data/research_framework/orchestrator_paused.flag")
AUDIT_LOG_PATH = Path("data/research_framework/orchestrator_log.jsonl")
DEFAULT_BUDGET_CAP_YUAN = 100.0


class CycleResult:
    def __init__(self, status: str, actions: list[str] | None = None, reason: str | None = None):
        self.status = status
        self.actions = actions or []
        self.reason = reason


def is_paused() -> bool:
    return Path(PAUSE_FLAG_PATH).exists()


def _audit(action: str, payload: dict[str, Any] | None = None) -> None:
    path = Path(AUDIT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "payload": payload or {},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def run_cycle(config: dict[str, Any]) -> CycleResult:
    budget_cap = float(config.get("max_cycle_budget_yuan", DEFAULT_BUDGET_CAP_YUAN))
    if is_paused():
        _audit("paused", {"budget_cap": budget_cap})
        return CycleResult("paused", reason="pause flag exists")

    actions = []
    for action in ("check_running_jobs", "sync_completed", "review", "digest"):
        _audit(action, {"budget_cap": budget_cap})
        actions.append(action)

    ready_specs = config.get("ready_specs", [])
    draft_specs = config.get("draft_specs", [])
    if ready_specs:
        _audit("handoff_ready_to_runner", {"count": len(ready_specs), "draft_count": len(draft_specs)})
        actions.append("handoff_ready_to_runner")
        return CycleResult("ready_handoff", actions=actions)

    # DRAFT specs are queued for implementation review, never auto-run.
    if draft_specs:
        _audit("draft_specs_not_run", {"count": len(draft_specs)})
        actions.append("draft_specs_not_run")

    _audit("ideate_compile", {"budget_cap": budget_cap})
    actions.append("ideate_compile")
    return CycleResult("completed", actions=actions)
