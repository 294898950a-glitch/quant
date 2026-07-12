#!/usr/bin/env python3
"""Project-owned 10-minute tick for quant automation.

This is the internal cron entrypoint. Hermes may monitor its outputs, but the
workflow wake-up is owned by the quant project.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(os.environ.get("QUANT_REPO_ROOT") or Path(__file__).resolve().parents[1]).resolve()
SCRIPT_DIR = REPO_ROOT / "scripts"
for path in (REPO_ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from framework.autonomous.workflow_state import queue_counts
from framework.autonomous.controller_owner import audit_noop, owner_allows
from quant_access_guard import INTERNAL_CRON_ISSUER, issue_ticket


CURRENT_PATH = REPO_ROOT / "data" / "research_framework" / "current.yaml"
QUEUE_STATE_PATH = REPO_ROOT / "data" / "research_framework" / "research_queue.yaml"
STATUS_PATH = REPO_ROOT / "logs" / "research_queue_status.json"
LOCK_PATH = REPO_ROOT / "logs" / "quant_internal_tick.lock"
TICK_LOG_PATH = REPO_ROOT / "logs" / "quant_internal_tick.log"
CONTROLLER_AUDIT_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_log.jsonl"
BRIDGE_ROOT = os.environ.get("QUANT_CODEX_BRIDGE_PATH", "").strip()
BRIDGE_CODEX_OUTBOX = Path(BRIDGE_ROOT) / "codex" / "outbox.md" if BRIDGE_ROOT else None
BRIDGE_STATE = Path(BRIDGE_ROOT) / "state.md" if BRIDGE_ROOT else None
TZ = ZoneInfo("Asia/Shanghai")
RUNNER_TIMEOUT_SECONDS = 30 * 60


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_yaml(path: Path, default):
    if not path.exists():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_current(current: dict) -> list[str]:
    summary = current.get("summary") if isinstance(current, dict) else {}
    strategies = current.get("strategies") if isinstance(current, dict) else []
    main_id = summary.get("current_main_strategy_id") if isinstance(summary, dict) else None
    main = {}
    if isinstance(strategies, list):
        for item in strategies:
            if isinstance(item, dict) and item.get("strategy_id") == main_id:
                main = item
                break
    return [
        f"- current_main_strategy_id: {main_id or 'unknown'}",
        f"- current_status: {main.get('status', 'unknown')}",
        f"- deployment_contract_status: {main.get('deployment_contract_status', 'unknown')}",
        f"- research_direction: {main.get('research_direction', 'unknown')}",
    ]


def recent_queue(state: dict, limit: int = 6) -> list[str]:
    queue = state.get("queue") if isinstance(state, dict) else []
    if not isinstance(queue, list):
        return ["- queue malformed or missing"]
    lines: list[str] = []
    for item in queue[-limit:]:
        if not isinstance(item, dict):
            continue
        bits = [
            str(item.get("id") or "unknown"),
            f"status={item.get('status', 'unknown')}",
        ]
        if item.get("vm_id") or item.get("vm_host"):
            bits.append(f"vm={item.get('vm_id') or item.get('vm_host')}")
        if item.get("remote_pid"):
            bits.append(f"pid={item.get('remote_pid')}")
        if item.get("failure_reason"):
            bits.append(f"failure={item.get('failure_reason')}")
        if item.get("block_reason"):
            bits.append(f"block={item.get('block_reason')}")
        lines.append("- " + "; ".join(bits))
    return lines or ["- queue empty"]


def append_bridge_status(*, run_kind: str, returncode: int, output: str, state: dict, status: dict, counts: dict[str, int]) -> None:
    if BRIDGE_CODEX_OUTBOX is None or BRIDGE_STATE is None or not BRIDGE_CODEX_OUTBOX.parent.exists():
        return
    after_status = status.get("status", "unknown") if isinstance(status, dict) else "unknown"
    lines = [
        "",
        f"### {now()} - Quant Internal Cron - STATUS",
        "",
        "Project: quant",
        "Task: project-owned internal tick",
        "",
        "Status:",
        f"- tick_action: {run_kind}",
        f"- returncode: {returncode}",
        f"- runner_output: {output or '(empty)'}",
        f"- queue_status: {after_status}",
        f"- queue_counts: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}",
    ]
    escalation = state.get("escalation") if isinstance(state, dict) else {}
    if isinstance(escalation, dict) and escalation.get("status"):
        lines.append(f"- escalation_status: {escalation.get('status')}")
        lines.append(f"- escalation_reason: {escalation.get('reason', 'unknown')}")
    lines.append("- scope: quant-only")
    lines.append("")
    lines.append("Recent queue:")
    lines.extend(recent_queue(state))
    lines.append("")
    message = "\n".join(lines)
    for path in (BRIDGE_CODEX_OUTBOX, BRIDGE_STATE):
        if path is None:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(message)
        except OSError:
            continue


def run_once_under_lock() -> tuple[str, int, str]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ("skipped_locked", 0, "another quant internal tick is still running")

        os.environ["QUANT_AUTOMATION_ISSUER"] = INTERNAL_CRON_ISSUER
        ticket = issue_ticket("research_queue_runner_once")
        env = dict(**os.environ)
        env["QUANT_AUTOMATION_ACTOR"] = "quant_internal_cron"
        env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
        env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
        try:
            result = subprocess.run(
                [sys.executable, "scripts/research_queue_runner.py"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=RUNNER_TIMEOUT_SECONDS,
                check=False,
            )
            returncode = result.returncode
            output = (result.stdout or "").strip()
            run_kind = "ran_once"
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            output = (exc.stdout or exc.stderr or "")
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output = f"research_queue_runner timed out after {RUNNER_TIMEOUT_SECONDS}s\n{str(output).strip()}"
            run_kind = "runner_timeout"
        TICK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TICK_LOG_PATH.open("a", encoding="utf-8") as log_fh:
            log_fh.write(
                json.dumps(
                    {"ts": now(), "returncode": returncode, "output": output[-4000:]},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        return (run_kind, returncode, output)


def controller_owner_allows_tick() -> tuple[bool, str]:
    """Fence a stale controller before it can issue an automation ticket.

    The active controller is declared in current.yaml during a drained,
    cron-registry-managed cutover. Missing metadata preserves the existing WSL
    deployment until the migration writes an explicit controller block.
    """
    return owner_allows(current_path=CURRENT_PATH)


def audit_controller_noop(reason: str) -> None:
    audit_noop(audit_path=CONTROLLER_AUDIT_PATH, reason=reason)


PAUSE_FLAG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_paused.flag"
CLUSTER_DETECTOR_STATE_PATH = (
    REPO_ROOT / "data" / "research_framework" / "cluster_detector_state.json"
)


def _check_infra_cluster_pause() -> tuple[bool, str]:
    """Run the infra cluster detector before dispatching the queue tick.

    If the most recent failed tasks share the same root-cause signature
    (user mandate 2026-05-25: 2 consecutive same-signature failures →
    auto-pause), touch the orchestrator pause flag and surface the
    decision. Honors recovery_armed_at from the cluster detector state
    file so historical infra_failed corpses do not retrigger pause
    immediately after a recovery unpause (mandate 2026-05-26).
    Returns (newly_paused, reason).
    """
    try:
        from framework.autonomous import infra_cluster_detector
    except Exception as exc:  # pragma: no cover — defensive
        return False, f"cluster_detector_import_failed: {type(exc).__name__}: {exc}"
    try:
        state = load_yaml(QUEUE_STATE_PATH, {})
        if not isinstance(state, dict):
            return False, "queue state is not a mapping"
        recovery_armed_at = infra_cluster_detector.load_recovery_armed_at(
            CLUSTER_DETECTOR_STATE_PATH
        )
        decision = infra_cluster_detector.evaluate(
            state,
            REPO_ROOT,
            recovery_armed_at=recovery_armed_at,
        )
    except Exception as exc:
        return False, f"cluster_detector_evaluate_failed: {type(exc).__name__}: {exc}"
    created = infra_cluster_detector.maybe_touch_pause_flag(decision, PAUSE_FLAG_PATH)
    return created, str(decision.get("reason") or "")


def main() -> int:
    before_status = load_json(STATUS_PATH, {})
    allowed, controller_reason = controller_owner_allows_tick()
    if allowed:
        cluster_paused, cluster_reason = _check_infra_cluster_pause()
        run_kind, returncode, output = run_once_under_lock()
    else:
        cluster_paused, cluster_reason = False, controller_reason
        run_kind, returncode, output = "skipped_controller_owner", 0, controller_reason
        audit_controller_noop(controller_reason)
    current = load_yaml(CURRENT_PATH, {})
    state = load_yaml(QUEUE_STATE_PATH, {})
    status = load_json(STATUS_PATH, {})
    counts = queue_counts(state)

    print("# Quant Internal Tick")
    print(f"- timestamp: {now()}")
    print("- project_scope: quant only")
    print()
    print("## Current")
    for line in summarize_current(current):
        print(line)
    print()
    print("## Tick Result")
    print(f"- action: {run_kind}")
    print(f"- returncode: {returncode}")
    print(f"- output: {output or '(empty)'}")
    print(f"- controller_gate: {controller_reason}")
    if cluster_paused:
        print(f"- cluster_detector: NEWLY PAUSED — {cluster_reason}")
    elif cluster_reason:
        print(f"- cluster_detector: {cluster_reason}")
    print()
    print("## Loop Status")
    before = before_status.get("status", "unknown") if isinstance(before_status, dict) else "unknown"
    after = status.get("status", "unknown") if isinstance(status, dict) else "unknown"
    print(f"- before_status: {before}")
    print(f"- after_status: {after}")
    print(f"- queue_counts: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
    print()
    print("## Recent Queue")
    for line in recent_queue(state):
        print(line)

    append_bridge_status(
        run_kind=run_kind,
        returncode=returncode,
        output=output,
        state=state,
        status=status,
        counts=counts,
    )
    return 0 if returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
