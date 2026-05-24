#!/usr/bin/env python3
"""Symmetric counterpart to spot_idle_shutdown.py — auto-start Guangzhou spot
when the queue has work waiting and spot is currently STOPPED.

Runs from the Singapore sig VM on the same cadence as the shutdown guard
(`*/5 * * * *`). Reads queue state from the synced
`research_queue.yaml`, probes the spot's Tencent CVM state via the SDK,
and — if both signals say "spot off, work waiting" — invokes the existing
`scripts/start_spot.py` to call StartInstances and wait for RUNNING.

Boundaries (mirrors spot_idle_shutdown.py):
- Never queues experiments, never asks AI, never changes strategy state.
- Independent process; only ssh / Tencent CVM API + filesystem state.
- All decisions are journaled to a state file so the next cron tick can
  reason about cooldown / backoff / daily caps without losing memory.

Safety rails:
- Queue-stable window: only start when there has been a queued item for at
  least ``--queued-stable-minutes`` consecutive ticks (default 2 min).
  This prevents a race against shutdown when work has just landed.
- Stop cooldown: refuse to start if shutdown_state.updated_at is within
  ``--stop-cooldown-minutes`` of now (default 10 min). Prevents yo-yo.
- Failed-start backoff: after a failed start attempt, refuse to retry for
  ``--failed-start-backoff-minutes`` (default 30 min).
- Daily cap: refuse if today's start count >= ``--daily-cap`` (default 6).
  Counter resets at UTC midnight.

A start attempt that succeeds writes ``status: started`` to the state file
and bumps the daily counter; a failed attempt writes ``status: start_failed``
and arms the backoff window.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def quant_queue_active(queue_state: dict[str, Any]) -> bool:
    queue = queue_state.get("queue")
    if not isinstance(queue, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("status") or "") in {"queued", "running"}
        for item in queue
    )


def find_spot_instance_id(queue_state: dict[str, Any], vm_id: str) -> str | None:
    for item in queue_state.get("vm_hosts", []) or []:
        if isinstance(item, dict) and str(item.get("id") or "") == vm_id:
            iid = item.get("instance_id") or item.get("tencent_instance_id")
            if isinstance(iid, str) and iid:
                return iid
    return None


def probe_spot_state(instance_id: str, region: str | None) -> tuple[str, str]:
    """Return (InstanceState, LatestOperation) via Tencent CVM SDK."""
    try:
        from tencentcloud.common import credential
        from tencentcloud.cvm.v20170312 import cvm_client, models
    except ImportError as exc:
        raise RuntimeError(
            "tencentcloud SDK not installed on sig; install "
            "tencentcloud-sdk-python-common + tencentcloud-sdk-python-cvm"
        ) from exc
    sid = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    if not sid or not skey:
        raise RuntimeError("TENCENTCLOUD_SECRET_ID / _KEY not in env")
    region = region or os.environ.get("TENCENTCLOUD_REGION") or "ap-guangzhou"
    cred = credential.Credential(sid, skey)
    client = cvm_client.CvmClient(cred, region)
    req = models.DescribeInstancesRequest()
    req.InstanceIds = [instance_id]
    resp = client.DescribeInstances(req)
    if not resp.InstanceSet:
        raise RuntimeError(f"instance {instance_id} not found in DescribeInstances")
    inst = resp.InstanceSet[0]
    return str(inst.InstanceState or ""), str(inst.LatestOperation or "")


def stop_cooldown_active(
    shutdown_state_path: Path, cooldown_seconds: float
) -> tuple[bool, float | None]:
    """Return (cooldown_still_active, seconds_since_stop).

    Reads spot_idle_shutdown_state.json; if it records a recent shutdown_sent
    within ``cooldown_seconds`` of now, we refuse to start. Mirrors the
    shutdown's own idle_since-style timer.
    """
    state = load_json(shutdown_state_path, {})
    status = str(state.get("status") or "")
    if status != "shutdown_sent":
        return False, None
    updated_at = state.get("updated_at") or ""
    if not isinstance(updated_at, str) or not updated_at:
        return False, None
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return False, None
    elapsed = now_ts() - ts.timestamp()
    return elapsed < cooldown_seconds, elapsed


def failed_start_backoff_active(
    state: dict[str, Any], backoff_seconds: float
) -> tuple[bool, float | None]:
    if str(state.get("last_status") or "") != "start_failed":
        return False, None
    updated_at = state.get("last_attempt_at") or ""
    if not isinstance(updated_at, str) or not updated_at:
        return False, None
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return False, None
    elapsed = now_ts() - ts.timestamp()
    return elapsed < backoff_seconds, elapsed


def daily_cap_reached(state: dict[str, Any], cap: int) -> tuple[bool, int]:
    counter = state.get("daily_counter") or {}
    if not isinstance(counter, dict):
        counter = {}
    today_count = int(counter.get(today_utc(), 0) or 0)
    return today_count >= cap, today_count


def bump_daily_counter(state: dict[str, Any]) -> dict[str, int]:
    counter = state.get("daily_counter") or {}
    if not isinstance(counter, dict):
        counter = {}
    today = today_utc()
    counter[today] = int(counter.get(today, 0) or 0) + 1
    # Garbage-collect entries older than 8 days.
    keep = {today}
    for day in counter:
        try:
            d = datetime.fromisoformat(day).date()
        except ValueError:
            continue
        if (datetime.now(timezone.utc).date() - d).days <= 7:
            keep.add(day)
    return {k: v for k, v in counter.items() if k in keep}


def queue_stable_for(state: dict[str, Any], stable_seconds: float) -> tuple[bool, float]:
    """Return (queue_has_been_queued_long_enough, seconds_since_first_seen).

    Caller has already checked the queue is currently active. We just need
    to know whether we have observed the active queue across at least
    ``stable_seconds`` of cron history.
    """
    queued_since = state.get("queued_since")
    if not isinstance(queued_since, (int, float)):
        return False, 0.0
    elapsed = max(0.0, now_ts() - float(queued_since))
    return elapsed >= stable_seconds, elapsed


def call_start_spot(start_script: Path, dry_run: bool, wait_seconds: int) -> tuple[bool, str]:
    if dry_run:
        return True, "dry_run: start skipped"
    cmd = [sys.executable, str(start_script), "--wait", str(wait_seconds)]
    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=wait_seconds + 30,
        check=False,
    )
    return result.returncode == 0, (result.stdout or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    repo_root = repo_root_from_script()
    parser.add_argument("--vm-id", default="guangzhou_spot")
    parser.add_argument("--queue-path", type=Path, default=repo_root / "data/research_framework/research_queue.yaml")
    parser.add_argument(
        "--state-path",
        type=Path,
        default=repo_root / "logs/spot_idle_start_state.json",
    )
    parser.add_argument(
        "--shutdown-state-path",
        type=Path,
        default=repo_root / "logs/spot_idle_shutdown_state.json",
    )
    parser.add_argument(
        "--start-script",
        type=Path,
        default=repo_root / "scripts/start_spot.py",
    )
    parser.add_argument("--queued-stable-minutes", type=float, default=2.0)
    parser.add_argument("--stop-cooldown-minutes", type=float, default=10.0)
    parser.add_argument("--failed-start-backoff-minutes", type=float, default=30.0)
    parser.add_argument("--daily-cap", type=int, default=6)
    parser.add_argument("--start-wait-seconds", type=int, default=180)
    parser.add_argument("--region", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    queue_state = load_yaml(args.queue_path, {})
    if not isinstance(queue_state, dict):
        queue_state = {}
    state = load_json(args.state_path, {})

    # 1. Is there work waiting?
    if not quant_queue_active(queue_state):
        state["queued_since"] = None
        state["last_status"] = "no_queue"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(json.dumps({"status": "no_queue", "ts": now_iso()}, ensure_ascii=False))
        return 0

    # 2. Stamp the "queue first observed active" timestamp if not set.
    if not isinstance(state.get("queued_since"), (int, float)):
        state["queued_since"] = now_ts()

    # 3. Stable window — avoid racing the shutdown guard.
    stable_ok, seconds_since_queued = queue_stable_for(
        state, args.queued_stable_minutes * 60.0
    )
    if not stable_ok:
        state["last_status"] = "queue_unstable_wait"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {
                    "status": "queue_unstable_wait",
                    "seconds_since_queued": round(seconds_since_queued, 1),
                    "stable_threshold_seconds": args.queued_stable_minutes * 60.0,
                },
                ensure_ascii=False,
            )
        )
        return 0

    # 4. Cooldown after recent shutdown.
    cooldown_active, seconds_since_stop = stop_cooldown_active(
        args.shutdown_state_path, args.stop_cooldown_minutes * 60.0
    )
    if cooldown_active:
        state["last_status"] = "stop_cooldown_wait"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {
                    "status": "stop_cooldown_wait",
                    "seconds_since_stop": round(seconds_since_stop or 0, 1),
                    "cooldown_seconds": args.stop_cooldown_minutes * 60.0,
                },
                ensure_ascii=False,
            )
        )
        return 0

    # 5. Failed-start backoff.
    backoff_active, seconds_since_attempt = failed_start_backoff_active(
        state, args.failed_start_backoff_minutes * 60.0
    )
    if backoff_active:
        state["last_status"] = "failed_start_backoff_wait"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {
                    "status": "failed_start_backoff_wait",
                    "seconds_since_failed_attempt": round(seconds_since_attempt or 0, 1),
                    "backoff_seconds": args.failed_start_backoff_minutes * 60.0,
                },
                ensure_ascii=False,
            )
        )
        return 0

    # 6. Daily cap.
    cap_hit, today_count = daily_cap_reached(state, args.daily_cap)
    if cap_hit:
        state["last_status"] = "daily_cap_reached"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {
                    "status": "daily_cap_reached",
                    "today_count": today_count,
                    "daily_cap": args.daily_cap,
                },
                ensure_ascii=False,
            )
        )
        return 0

    # 7. Probe spot state.
    instance_id = find_spot_instance_id(queue_state, args.vm_id)
    if not instance_id:
        state["last_status"] = "instance_id_missing"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {"status": "instance_id_missing", "vm_id": args.vm_id}, ensure_ascii=False
            )
        )
        return 1
    try:
        spot_state, latest_op = probe_spot_state(instance_id, args.region)
    except Exception as exc:
        state["last_status"] = "probe_failed"
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {"status": "probe_failed", "error": state["last_error"]},
                ensure_ascii=False,
            )
        )
        return 1

    if spot_state != "STOPPED":
        # Already running, starting, terminated, or in a state we should not
        # touch. Reset the queued_since timer because keeping it stale leads
        # to a future immediate-start after spot transitions back to STOPPED.
        state["last_status"] = f"spot_not_stopped:{spot_state}"
        state["queued_since"] = None
        state["updated_at"] = now_iso()
        save_json(args.state_path, state)
        print(
            json.dumps(
                {
                    "status": "spot_not_stopped",
                    "spot_state": spot_state,
                    "latest_op": latest_op,
                },
                ensure_ascii=False,
            )
        )
        return 0

    # 8. All gates passed — start spot.
    ok, output = call_start_spot(args.start_script, args.dry_run, args.start_wait_seconds)
    state["last_attempt_at"] = now_iso()
    if ok:
        state["last_status"] = "started"
        state["last_output"] = output[-2000:]
        state["daily_counter"] = bump_daily_counter(state)
        state["queued_since"] = None  # reset for next idle->busy cycle
    else:
        state["last_status"] = "start_failed"
        state["last_output"] = output[-2000:]
    state["updated_at"] = now_iso()
    save_json(args.state_path, state)
    print(
        json.dumps(
            {
                "status": "started" if ok else "start_failed",
                "spot_state_before": spot_state,
                "output": output[-500:],
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
