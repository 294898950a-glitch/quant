#!/usr/bin/env python3
"""Independent spot idle shutdown guard.

Intended to run from the Singapore VM as an independent idle guard. It is
separate from the quant scheduler: it never starts jobs, never asks an AI, and
never changes strategy state.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_PROCESS_PATTERN = (
    "auto_research_pipeline.py|evaluate_cb_arb|search_cb_arb|run_cb_arb|"
    "evaluate_valuation|dynamic_exit"
)


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root_from_script() -> Path:
    env = os.environ.get("QUANT_REPO_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


def load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def quant_queue_active(queue_state: dict[str, Any]) -> bool:
    queue = queue_state.get("queue")
    if not isinstance(queue, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("status") or "") in {"queued", "running"}
        for item in queue
    )


def queue_file_fresh(path: Path, max_age_minutes: float) -> tuple[bool, float | None]:
    if not path.exists():
        return False, None
    age_seconds = max(0.0, now_ts() - path.stat().st_mtime)
    return age_seconds <= max_age_minutes * 60.0, age_seconds / 60.0


def find_vm(queue_state: dict[str, Any], vm_id: str) -> dict[str, Any]:
    for item in queue_state.get("vm_hosts", []) or []:
        if isinstance(item, dict) and str(item.get("id") or "") == vm_id:
            return dict(item)
    raise RuntimeError(f"vm_hosts entry not found: {vm_id}")


def ssh_base(vm: dict[str, Any]) -> list[str]:
    cmd = [
        "ssh",
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    identity = str(vm.get("identity_file_on_proxy") or vm.get("identity_file") or "").strip()
    if identity:
        cmd.extend(["-i", identity, "-o", "IdentitiesOnly=yes"])
    cmd.append(str(vm["host"]))
    return cmd


def run_ssh(vm: dict[str, Any], command: str, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*ssh_base(vm), command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def spot_processes(vm: dict[str, Any], pattern: str) -> tuple[bool, str]:
    quoted = shlex.quote(pattern)
    result = run_ssh(vm, f"pgrep -af {quoted} | grep -v pgrep || true", timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stdout or "").strip() or f"ssh returned {result.returncode}")
    output = result.stdout or ""
    return bool(output.strip()), output.strip()


def shutdown_spot(vm: dict[str, Any], dry_run: bool) -> tuple[bool, str]:
    command = "sudo -n /sbin/shutdown -h now || sudo -n shutdown -h now || /sbin/shutdown -h now"
    if dry_run:
        return True, "dry_run: shutdown skipped"
    result = run_ssh(vm, command, timeout=20)
    return result.returncode == 0, (result.stdout or "").strip()


def reset_idle_state(state_path: Path, reason: str) -> dict[str, Any]:
    state = {"idle_since": None, "updated_at": now_iso(), "status": "not_idle", "reason": reason}
    save_json(state_path, state)
    return state


def main() -> int:
    repo_root = repo_root_from_script()
    parser = argparse.ArgumentParser(description="Shutdown Guangzhou spot after 15 idle minutes and empty quant queue.")
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--queue-path", type=Path, default=repo_root / "data/research_framework/research_queue.yaml")
    parser.add_argument("--state-path", type=Path, default=repo_root / "logs/spot_idle_shutdown_state.json")
    parser.add_argument("--vm-id", default="guangzhou_spot")
    parser.add_argument("--idle-minutes", type=float, default=15.0)
    parser.add_argument("--max-queue-age-minutes", type=float, default=20.0)
    parser.add_argument("--process-pattern", default=DEFAULT_PROCESS_PATTERN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fresh, queue_age_minutes = queue_file_fresh(args.queue_path, args.max_queue_age_minutes)
    if not fresh:
        state = reset_idle_state(
            args.state_path,
            "quant queue file is missing or stale; refusing spot shutdown",
        )
        state.update(
            {
                "status": "keep_on",
                "reason": "quant_queue_file_stale",
                "queue_path": str(args.queue_path),
                "queue_age_minutes": queue_age_minutes,
                "max_queue_age_minutes": args.max_queue_age_minutes,
            }
        )
        save_json(args.state_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return 0

    queue_state = load_yaml(args.queue_path, {})
    if not isinstance(queue_state, dict):
        raise SystemExit("queue state root must be a mapping")
    vm = find_vm(queue_state, args.vm_id)

    active_queue = quant_queue_active(queue_state)
    if active_queue:
        reset_idle_state(args.state_path, "quant queue has queued/running work")
        print(json.dumps({"status": "keep_on", "reason": "quant_queue_active", "ts": now_iso()}, ensure_ascii=False))
        return 0

    try:
        has_process, processes = spot_processes(vm, args.process_pattern)
    except Exception as exc:
        state = {
            "status": "spot_unreachable",
            "updated_at": now_iso(),
            "idle_since": None,
            "error": f"{type(exc).__name__}: {exc}",
            "vm_id": args.vm_id,
            "vm_host": vm.get("host"),
        }
        save_json(args.state_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return 1

    if has_process:
        reset_idle_state(args.state_path, "spot has quant process")
        print(
            json.dumps(
                {
                    "status": "keep_on",
                    "reason": "spot_process_active",
                    "processes": processes,
                    "ts": now_iso(),
                },
                ensure_ascii=False,
            )
        )
        return 0

    state = load_json(args.state_path, {})
    idle_since = state.get("idle_since")
    if not isinstance(idle_since, (int, float)):
        idle_since = now_ts()
    idle_seconds = max(0.0, now_ts() - float(idle_since))
    threshold_seconds = args.idle_minutes * 60.0

    if idle_seconds < threshold_seconds:
        state = {
            "status": "idle_waiting",
            "idle_since": idle_since,
            "idle_seconds": round(idle_seconds, 1),
            "threshold_seconds": round(threshold_seconds, 1),
            "updated_at": now_iso(),
            "vm_id": args.vm_id,
            "vm_host": vm.get("host"),
        }
        save_json(args.state_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return 0

    # Final queue check immediately before shutdown.
    queue_state = load_yaml(args.queue_path, {})
    if isinstance(queue_state, dict) and quant_queue_active(queue_state):
        reset_idle_state(args.state_path, "quant queue became active before shutdown")
        print(json.dumps({"status": "keep_on", "reason": "quant_queue_active_final_check", "ts": now_iso()}, ensure_ascii=False))
        return 0

    ok, output = shutdown_spot(vm, args.dry_run)
    state = {
        "status": "shutdown_sent" if ok else "shutdown_failed",
        "idle_since": idle_since,
        "idle_seconds": round(idle_seconds, 1),
        "updated_at": now_iso(),
        "vm_id": args.vm_id,
        "vm_host": vm.get("host"),
        "dry_run": args.dry_run,
        "output": output,
    }
    save_json(args.state_path, state)
    print(json.dumps(state, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
