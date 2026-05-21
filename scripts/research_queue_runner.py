#!/usr/bin/env python3
"""Research queue tick runner.

This entrypoint only reads queue state, decides the next workflow action, and
delegates specialized work to the owned framework services.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
for path in (REPO_ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from framework.autonomous.workflow_state import count_status, decide_scheduler_action
from framework.autonomous.queue_ideation import QueueIdeationService
from framework.autonomous.queue_remote_execution import QueueRemoteExecutionService
from framework.autonomous.queue_review_memory import QueueReviewMemoryService
from quant_access_guard import INTERNAL_CRON_ISSUER, issue_ticket, require_ticket


STATE_PATH = REPO_ROOT / "data" / "research_framework" / "research_queue.yaml"
LOG_PATH = REPO_ROOT / "logs" / "research_queue_runner.log"
STATUS_PATH = REPO_ROOT / "logs" / "research_queue_status.json"
ONCE_LOCK_PATH = REPO_ROOT / "logs" / "research_queue_runner_once.lock"
AUDIT_LOG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_log.jsonl"
PAUSE_FLAG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_paused.flag"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{now_iso()}] {message}\n")


def audit(action: str, payload: dict[str, Any] | None = None) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"action": action, "payload": payload or {}, "ts": now_iso()}
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def is_paused() -> bool:
    return PAUSE_FLAG_PATH.exists()


def _previous_status_payload() -> dict[str, Any] | None:
    if not STATUS_PATH.exists():
        return None
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_status(status: str, extra: dict[str, Any] | None = None) -> None:
    previous_status = _previous_status_payload()
    status_changed = previous_status is None or previous_status.get("status") != status
    payload: dict[str, Any] = {
        "status": status,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "updated_at": now_iso(),
        "state_path": rel(STATE_PATH),
        "status_changed": status_changed,
    }
    if extra:
        payload.update(extra)
    if not status_changed:
        payload["previous_status"] = previous_status.get("status") if previous_status else status
        payload["status_unchanged"] = True
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATUS_PATH)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        raise FileNotFoundError(f"missing state file: {STATE_PATH}")
    data = yaml.safe_load(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("research_queue.yaml root must be dict")
    return data


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    tmp = STATE_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def mark_history(state: dict[str, Any], item: dict[str, Any], status: str, message: str) -> None:
    history = state.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "id": item.get("id"),
                "spec_path": item.get("spec_path"),
                "status": status,
                "message": message,
                "at": now_iso(),
                "vm_id": item.get("vm_id"),
                "vm_host": item.get("vm_host"),
            }
        )


def _ideation_env() -> dict[str, str]:
    os.environ["QUANT_AUTOMATION_ISSUER"] = INTERNAL_CRON_ISSUER
    ticket = issue_ticket("strategy_ideation_once")
    env = dict(**os.environ)
    env["QUANT_AUTOMATION_ACTOR"] = "quant_internal_cron"
    env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
    env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
    return env


def _ideation_timeout_seconds() -> int:
    config_path = REPO_ROOT / "data" / "research_framework" / "strategy_ideator.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return 300
    if not isinstance(config, dict):
        return 300
    provider_timeout = int(config.get("timeout_seconds") or 240)
    return max(provider_timeout + 30, 300)


def ideation_service() -> QueueIdeationService:
    return QueueIdeationService(
        repo_root=REPO_ROOT,
        load_state=load_state,
        save_state=save_state,
        write_status=write_status,
        audit=audit,
        log=log,
        mark_history=mark_history,
        rel=rel,
        now_iso=now_iso,
        ideation_env=_ideation_env,
        timeout_seconds=_ideation_timeout_seconds(),
    )


def escalation_block(state: dict[str, Any]) -> dict[str, Any] | None:
    escalation = state.get("escalation") if isinstance(state, dict) else None
    if not isinstance(escalation, dict):
        return None
    status = str(escalation.get("status") or "")
    if status != "blocked_awaiting_user":
        return None
    if escalation.get("requires_user_decision") is not True and escalation.get("protected_action_required") is not True:
        return None
    return {
        "reason": str(escalation.get("reason") or "awaiting user decision"),
        "since": escalation.get("since"),
        "last_escalation": escalation.get("last_escalation"),
        "escalation_count": escalation.get("escalation_count"),
        "user_options": escalation.get("user_options"),
    }


def stop_for_user_block(state: dict[str, Any]) -> str:
    payload = escalation_block(state) or {"reason": "awaiting user decision"}
    write_status(
        "blocked_awaiting_user",
        {
            "note": "Research queue is blocked by explicit escalation; no new direction may be generated.",
            **payload,
        },
    )
    audit("blocked_awaiting_user", payload)
    log(f"blocked awaiting user: {payload.get('reason')}")
    return "blocked_awaiting_user"


def remote_execution_service() -> QueueRemoteExecutionService:
    return QueueRemoteExecutionService(
        repo_root=REPO_ROOT,
        save_state=save_state,
        write_status=write_status,
        audit=audit,
        log=log,
        mark_history=mark_history,
        rel=rel,
        now_iso=now_iso,
        issue_ticket=issue_ticket,
    )


def review_memory_service() -> QueueReviewMemoryService:
    remote = QueueRemoteExecutionService(
        repo_root=REPO_ROOT,
        save_state=save_state,
        write_status=write_status,
        audit=audit,
        log=log,
        mark_history=mark_history,
        rel=rel,
        now_iso=now_iso,
        issue_ticket=issue_ticket,
    )
    return QueueReviewMemoryService(
        repo_root=REPO_ROOT,
        run=remote.run,
        save_state=save_state,
        audit=audit,
        mark_history=mark_history,
        rel=rel,
        now_iso=now_iso,
    )


def tick() -> str:
    if is_paused():
        write_status("paused", {"pause_flag": rel(PAUSE_FLAG_PATH)})
        audit("paused", {"pause_flag": rel(PAUSE_FLAG_PATH)})
        return "paused"

    state = load_state()
    if not state.get("enabled", False):
        write_status("disabled")
        return "disabled"
    queue = state.get("queue") or []
    if not isinstance(queue, list):
        raise ValueError("research_queue.queue must be list")

    remote = remote_execution_service()
    review_memory = review_memory_service()
    settled_count = remote.settle_running_items(state, queue)
    reviewed_count = review_memory.review_pending_items(state, queue)
    requeued_repaired_count = remote.requeue_repaired_data_items(state, queue)
    stale_vm_avoidance_reset_count = remote.clear_stale_vm_avoidances(state, queue)
    decision = decide_scheduler_action(state)
    running_count = count_status(state, "running")
    review_pending_count = count_status(state, "review_pending")

    if decision.action != "start_or_continue_queued":
        if decision.action == "monitor_running":
            write_status(
                "waiting_remote_running",
                {
                    "running_count": running_count,
                    "reviewed_count": reviewed_count,
                    "settled_count": settled_count,
                    "requeued_repaired_count": requeued_repaired_count,
                    "stale_vm_avoidance_reset_count": stale_vm_avoidance_reset_count,
                    "note": "Remote work is still running; no new direction may be generated.",
                },
            )
            return "waiting_remote_running"
        if decision.action == "review_pending":
            write_status(
                "waiting_review_memory",
                {
                    "review_pending_count": review_pending_count,
                    "reviewed_count": reviewed_count,
                    "settled_count": settled_count,
                    "requeued_repaired_count": requeued_repaired_count,
                    "stale_vm_avoidance_reset_count": stale_vm_avoidance_reset_count,
                    "note": "Synced results are waiting for review_memory.",
                },
            )
            return "waiting_review_memory"
        if escalation_block(state):
            return stop_for_user_block(state)
        return ideation_service().generate_until_actionable(state)

    return remote.start_queued_items(
        state,
        queue,
        settled_count=settled_count,
        requeued_repaired_count=requeued_repaired_count,
        stale_vm_avoidance_reset_count=stale_vm_avoidance_reset_count,
    )


def tick_once_under_lock() -> str:
    ONCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ONCE_LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_status(
                "skipped_locked",
                {"lock_path": rel(ONCE_LOCK_PATH), "note": "another research queue tick is already active"},
            )
            audit("skipped_locked", {"lock_path": rel(ONCE_LOCK_PATH)})
            return "skipped_locked"
        return tick()


def main() -> int:
    require_ticket("research_queue_runner_once")
    print(tick_once_under_lock())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
