#!/usr/bin/env python3
"""Append quant option-value loop progress heartbeats to AI communication files."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_YAML = REPO_ROOT / "data" / "research_framework" / "option_value_loop.yaml"
STATUS_JSON = REPO_ROOT / "logs" / "option_value_loop_status.json"
AI_ROOT = Path(os.environ.get("AI_ROOT", "/mnt/c/Users/陈教授/Desktop/ai"))
QUANT_AI_ROOT = AI_ROOT / "projects" / "quant"
CODEX_OUTBOX = QUANT_AI_ROOT / "codex" / "outbox.md"
STATE_MD = QUANT_AI_ROOT / "state.md"
TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(TZ)


def load_yaml(path: Path, default):
    if not path.exists():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


def run_result(run_id: str) -> dict:
    run_dir = REPO_ROOT / "data" / run_id
    summary = load_json(run_dir / "summary.json", {})
    report = load_yaml(run_dir / "report.yaml", {})
    best_test = summary.get("best_test") if isinstance(summary, dict) else {}
    best_train = summary.get("best_train") if isinstance(summary, dict) else {}
    selected_2020 = summary.get("selected_2020") if isinstance(summary, dict) else {}
    return {
        "exists": bool(summary or report),
        "decision": report.get("l6_exit_decision") or summary.get("status") or "unknown",
        "adoption_pass": summary.get("adoption_pass"),
        "best_train": best_train if isinstance(best_train, dict) else {},
        "best_test": best_test if isinstance(best_test, dict) else {},
        "selected_2020": selected_2020 if isinstance(selected_2020, dict) else {},
        "summary": report.get("summary") or "",
    }


def queue_items(state: dict, status: str) -> list[dict]:
    queue = state.get("queue") if isinstance(state, dict) else []
    return queue if isinstance(queue, list) else []


def completed_items(items: list[dict]) -> list[dict]:
    done = [
        item for item in items
        if isinstance(item, dict) and item.get("status") == "complete"
    ]
    return sorted(done, key=lambda item: str(item.get("completed_at") or ""), reverse=True)


def running_items(items: list[dict]) -> list[dict]:
    return [
        item for item in items
        if isinstance(item, dict) and item.get("status") == "running"
    ]


def item_line(item: dict, include_result: bool = False) -> str:
    run_id = str(item.get("id") or "unknown")
    bits = [
        run_id,
        f"status={item.get('status')}",
        f"vm={item.get('vm_id') or item.get('vm_host') or 'n/a'}",
    ]
    if item.get("remote_pid"):
        bits.append(f"pid={item.get('remote_pid')}")
    if item.get("completed_at"):
        bits.append(f"completed_at={item.get('completed_at')}")
    if include_result:
        result = run_result(run_id)
        if result["exists"]:
            best_test = result["best_test"]
            best_train = result["best_train"]
            selected_2020 = result["selected_2020"]
            bits.append(f"decision={result['decision']}")
            bits.append(f"adoption_pass={result['adoption_pass']}")
            if best_train:
                bits.append(
                    "best_train="
                    f"{best_train.get('name')} excess={pct(best_train.get('excess_return'))}"
                )
            if best_test:
                bits.append(
                    "best_test="
                    f"{best_test.get('name')} excess={pct(best_test.get('excess_return'))}"
                )
            if selected_2020:
                bits.append(
                    "selected_2020="
                    f"{selected_2020.get('name')} excess={pct(selected_2020.get('excess_return'))}"
                )
    return "；".join(bits)


def build_message() -> str:
    stamp = now()
    state = load_yaml(STATE_YAML, {})
    status = load_json(STATUS_JSON, {})
    items = queue_items(state, str(status.get("status") or ""))
    running = running_items(items)
    complete = completed_items(items)
    next_due = stamp + timedelta(minutes=10)
    claim_seed = f"{stamp.isoformat()}|{status.get('status')}|{len(running)}|{len(complete)}"
    claim = abs(hash(claim_seed)) % 10_000_000_000

    lines = [
        f"### {stamp:%Y-%m-%d %H:%M} CST - Codex - HEARTBEAT/OPTION_VALUE_LOOP_PROGRESS",
        "",
        "Project: quant",
        "Task: option-value auto loop progress heartbeat",
        "",
        "Status:",
        f"- Loop status: `{status.get('status', 'unknown')}`; loop_pid={status.get('pid', 'n/a')}.",
        f"- Queue: running={len(running)}, complete={len(complete)}.",
    ]
    if running:
        lines.append("- Running:")
        lines.extend(f"  - {item_line(item)}" for item in running)
    else:
        lines.append("- Running: none.")

    if complete:
        lines.append("- Recent completed:")
        lines.extend(f"  - {item_line(item, include_result=True)}" for item in complete[:3])
    else:
        lines.append("- Recent completed: none.")

    lines.extend(
        [
            f"- Next heartbeat due by: {next_due:%Y-%m-%d %H:%M} CST.",
            "- Scope: quant-only; no other project files touched.",
            f"- claim: quant-option-value-progress-{claim}",
            "",
        ]
    )
    return "\n".join(lines)


def append(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n" + message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    message = build_message()
    if args.dry_run:
        print(message)
        return 0
    append(CODEX_OUTBOX, message)
    append(STATE_MD, message)
    print(f"wrote heartbeat to {CODEX_OUTBOX} and {STATE_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
