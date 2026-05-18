#!/usr/bin/env python3
"""Cross-session quant project health watcher.

Designed to be called by Hermes cron every 15 min. Outputs a structured
report to stdout. If any alert condition triggers, exit code 2 and emit
the alert markers (⚠/❌) for Hermes prompt injection.

Hermes-side wiring (example):
    hermes cron create "*/15 * * * *" \\
        --name quant_watcher \\
        --workdir /home/jay/projects/quant \\
        --script /home/jay/projects/quant/scripts/quant_health_check.py \\
        --deliver telegram \\
        "你是 quant 项目守护者。下面是健康检查报告。如有 ⚠ 或 ❌ 行，请总结并发送 Telegram 通知给我；否则保持 silent，不发消息。"

Exit codes:
  0 = healthy
  1 = warnings only
  2 = alert (one or more critical conditions)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path("/home/jay/projects/quant")
WSL_CODEX_OUTBOX = Path("/mnt/c/Users/陈教授/Desktop/ai/projects/quant/codex/outbox.md")
WSL_CLAUDE_OUTBOX = Path("/mnt/c/Users/陈教授/Desktop/ai/projects/quant/claude/outbox.md")

# Thresholds
CODEX_SILENCE_ALERT_HOURS = 2
DAEMON_ERROR_ALERT_MINUTES = 30
DAILY_SPEND_ALERT_YUAN = 100.0
PREFLIGHT_REQUIRED = True


def _hours_ago(path: Path) -> float | None:
    """How many hours ago was this file modified? None if missing."""
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    delta = datetime.now(tz=timezone.utc) - mtime
    return delta.total_seconds() / 3600


def _daemon_status() -> dict | None:
    status_path = REPO_ROOT / "logs" / "option_value_loop_status.json"
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text())
    except Exception:
        return None


def _daily_spend_yuan() -> float | None:
    """Sum compute_cost_yuan from manifests created in last 24h."""
    manifests_dir = REPO_ROOT / "data" / "research_framework" / "run_manifests"
    if not manifests_dir.exists():
        return None
    import yaml
    cutoff = datetime.now(tz=timezone.utc).timestamp() - 24 * 3600
    total = 0.0
    for manifest_path in manifests_dir.glob("*.yaml"):
        if manifest_path.stat().st_mtime < cutoff:
            continue
        try:
            data = yaml.safe_load(manifest_path.read_text())
            auto = (data or {}).get("automation") or {}
            budget = auto.get("budget") or {}
            cost = budget.get("estimated_budget_yuan") or 0
            total += float(cost)
        except Exception:
            continue
    return total


def _preflight_ok() -> tuple[bool, str]:
    """Run framework_preflight.py --quiet and return (ok, last_line)."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "framework_preflight.py"), "--quiet"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    last_line = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else "(no output)"
    return (result.returncode in {0, 2}, last_line)


def _last_commit() -> tuple[str, float] | None:
    """Return (hash, hours_ago) of last commit. None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h|%ct"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=10,
        )
        if result.returncode != 0:
            return None
        commit_hash, ts = result.stdout.strip().split("|")
        commit_time = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        delta_hours = (datetime.now(tz=timezone.utc) - commit_time).total_seconds() / 3600
        return commit_hash, delta_hours
    except Exception:
        return None


def main() -> int:
    print(f"# Quant Health Check — {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")
    print()

    exit_code = 0
    alerts = []
    warnings = []

    # 1. Codex outbox silence
    codex_hours = _hours_ago(WSL_CODEX_OUTBOX)
    if codex_hours is None:
        warnings.append(f"⚠ Codex outbox not accessible at {WSL_CODEX_OUTBOX}")
    elif codex_hours > CODEX_SILENCE_ALERT_HOURS:
        alerts.append(f"❌ Codex outbox silent {codex_hours:.1f}h (> {CODEX_SILENCE_ALERT_HOURS}h)")
    else:
        print(f"✓ Codex outbox active ({codex_hours*60:.0f} min ago)")

    # 2. Daemon status
    status = _daemon_status()
    if status is None:
        warnings.append("⚠ option_value_loop_status.json missing")
    else:
        loop_status = status.get("status", "unknown")
        if loop_status == "error":
            updated_at_str = status.get("updated_at", "")
            try:
                updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                error_minutes = (datetime.now(tz=timezone.utc) - updated_at).total_seconds() / 60
            except Exception:
                error_minutes = -1
            if error_minutes > DAEMON_ERROR_ALERT_MINUTES:
                alerts.append(f"❌ Daemon status=error for {error_minutes:.0f} min (> {DAEMON_ERROR_ALERT_MINUTES})")
            else:
                warnings.append(f"⚠ Daemon status=error, {error_minutes:.0f} min")
        else:
            print(f"✓ Daemon status={loop_status}")

    # 3. Pause flag
    pause_flag = REPO_ROOT / "data" / "research_framework" / "orchestrator_paused.flag"
    if pause_flag.exists():
        pause_hours = _hours_ago(pause_flag) or 0
        if pause_hours > 4:
            warnings.append(f"⚠ Pause flag held {pause_hours:.1f}h (long pause may be stale)")
        else:
            print(f"✓ Pause flag active ({pause_hours*60:.0f} min ago)")
    else:
        print("✓ No pause flag")

    # 4. Daily spend
    spend = _daily_spend_yuan()
    if spend is None:
        pass
    elif spend > DAILY_SPEND_ALERT_YUAN:
        alerts.append(f"❌ Daily spend ¥{spend:.2f} > ¥{DAILY_SPEND_ALERT_YUAN}")
    elif spend > DAILY_SPEND_ALERT_YUAN * 0.5:
        warnings.append(f"⚠ Daily spend ¥{spend:.2f} approaching ¥{DAILY_SPEND_ALERT_YUAN}")
    else:
        print(f"✓ Daily spend ¥{spend:.2f}")

    # 5. Preflight
    if PREFLIGHT_REQUIRED:
        ok, last_line = _preflight_ok()
        if ok:
            print(f"✓ Preflight: {last_line}")
        else:
            alerts.append(f"❌ Preflight FAIL: {last_line}")

    # 6. Last commit
    commit_info = _last_commit()
    if commit_info:
        commit_hash, commit_hours_ago = commit_info
        if commit_hours_ago > 48:
            warnings.append(f"⚠ Last commit {commit_hash} {commit_hours_ago:.1f}h ago (working tree may accumulate)")
        else:
            print(f"✓ Last commit {commit_hash} ({commit_hours_ago:.1f}h ago)")

    print()
    if alerts:
        print("# Alerts")
        for a in alerts:
            print(a)
        exit_code = 2
    if warnings:
        print("# Warnings")
        for w in warnings:
            print(w)
        if exit_code == 0:
            exit_code = 1
    if not alerts and not warnings:
        print("# All healthy")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
