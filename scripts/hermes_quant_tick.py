#!/usr/bin/env python3
"""Hermes 10-minute driver for quant research progress."""

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

REPO_ROOT = Path("/home/jay/projects/quant")
SCRIPT_DIR = REPO_ROOT / "scripts"
for path in (REPO_ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from framework.autonomous.workflow_state import queue_counts
from hermes_access_guard import issue_ticket


CURRENT_PATH = REPO_ROOT / "data" / "research_framework" / "current.yaml"
OPTION_STATE_PATH = REPO_ROOT / "data" / "research_framework" / "option_value_loop.yaml"
STATUS_PATH = REPO_ROOT / "logs" / "option_value_loop_status.json"
LOCK_PATH = REPO_ROOT / "logs" / "hermes_quant_tick.lock"
TICK_LOG_PATH = REPO_ROOT / "logs" / "hermes_quant_tick.log"
TZ = ZoneInfo("Asia/Shanghai")


def load_yaml(path: Path, default):
    if not path.exists():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


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
    lines = [
        f"- current_main_strategy_id: {main_id or 'unknown'}",
        f"- current_status: {main.get('status', 'unknown')}",
        f"- deployment_contract_status: {main.get('deployment_contract_status', 'unknown')}",
        f"- research_direction: {main.get('research_direction', 'unknown')}",
    ]
    next_default = main.get("next_default") if isinstance(main, dict) else {}
    if isinstance(next_default, dict):
        lines.append(f"- no_reply_default_action: {next_default.get('action', 'unknown')}")
    digest = current.get("recent_results_digest") if isinstance(current, dict) else {}
    if isinstance(digest, dict):
        lines.append(f"- recent_results_digest: {digest.get('path', 'missing')}")
    return lines


def recent_queue(option_state: dict, limit: int = 6) -> list[str]:
    queue = option_state.get("queue") if isinstance(option_state, dict) else []
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


def run_once_under_lock() -> tuple[str, int, str]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ("skipped_locked", 0, "another Hermes quant tick is still running")

        ticket = issue_ticket("option_value_loop_once")
        env = dict(**os.environ)
        env["QUANT_HERMES_ACTOR"] = "hermes"
        env["QUANT_HERMES_TICKET_PATH"] = ticket["path"]
        env["QUANT_HERMES_TICKET_TOKEN"] = ticket["token"]
        result = subprocess.run(
            [sys.executable, "scripts/option_value_loop_daemon.py", "--once", "--no-ideation"],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=540,
            check=False,
        )
        output = (result.stdout or "").strip()
        TICK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TICK_LOG_PATH.open("a", encoding="utf-8") as log_fh:
            log_fh.write(
                json.dumps(
                    {
                        "ts": now(),
                        "returncode": result.returncode,
                        "output": output[-4000:],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        return ("ran_once", result.returncode, output)


def main() -> int:
    before_current = load_yaml(CURRENT_PATH, {})
    before_state = load_yaml(OPTION_STATE_PATH, {})
    before_status = load_json(STATUS_PATH, {})

    run_kind, returncode, output = run_once_under_lock()

    after_current = load_yaml(CURRENT_PATH, {})
    after_state = load_yaml(OPTION_STATE_PATH, {})
    after_status = load_json(STATUS_PATH, {})
    counts = queue_counts(after_state)
    ideation = after_state.get("ideation") if isinstance(after_state, dict) else {}
    blocked_reason = ideation.get("blocked_reason") if isinstance(ideation, dict) else None

    print("# Hermes Quant Tick")
    print(f"- timestamp: {now()}")
    print("- project_scope: quant only")
    print()
    print("## Current")
    for line in summarize_current(after_current or before_current):
        print(line)
    print()
    print("## Tick Result")
    print(f"- action: {run_kind}")
    print(f"- returncode: {returncode}")
    print(f"- output: {output or '(empty)'}")
    print()
    print("## Loop Status")
    before = before_status.get("status", "unknown") if isinstance(before_status, dict) else "unknown"
    after = after_status.get("status", "unknown") if isinstance(after_status, dict) else "unknown"
    print(f"- before_status: {before}")
    print(f"- after_status: {after}")
    print(f"- queue_counts: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
    if blocked_reason:
        print(f"- blocked_reason: {blocked_reason}")
    print()
    print("## Recent Queue")
    for line in recent_queue(after_state):
        print(line)
    print()
    print("## Hermes Instruction")
    print("- If returncode is non-zero, inspect logs/hermes_quant_tick.log and fix the blocker.")
    print("- If queue_counts has queued or running work, do not create a new direction. Monitor only.")
    print("- Hermes must not decide workflow state by itself; scripts/option_value_loop_daemon.py is the mechanical state authority.")
    print("- If there is no queued/running work and the daemon asks for a new research direction, Hermes may write one READY spec through the registered common entrypoint.")
    print("- If after_status is running_remote or waiting_remote_running, stay mostly silent unless the user asked for status.")
    print("- If a run completed or failed, summarize the result and update the quant communication files.")
    print("- Do not promote current.yaml automatically. Do not modify baseline_registry automatically.")
    print("- Do not read or modify other projects.")

    if returncode != 0 or blocked_reason:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
