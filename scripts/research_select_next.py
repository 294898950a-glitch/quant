#!/usr/bin/env python3
"""Deterministic selector for the next quant research thread.

Mode B must not invent a direction.  This reads the experience ledger open
threads, excludes blocked dependencies and rejected duplicates, then prints one
entry id or ``EMPTY``.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


PRIORITY_WORDS = {"高": 80, "中": 50, "低": 20, "high": 80, "medium": 50, "low": 20}


@dataclass(frozen=True)
class Thread:
    entry_id: str
    priority: int
    last_updated: str
    description: str
    dependency: tuple[str, ...]
    raw: str


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def section_between(text: str, start: str, end: str | None) -> str:
    start_idx = text.find(start)
    if start_idx < 0:
        return ""
    body = text[start_idx + len(start) :]
    if end is None:
        return body
    end_idx = body.find(end)
    return body if end_idx < 0 else body[:end_idx]


def priority_value(raw: str) -> int:
    match = re.search(r"priority\s*[:=]\s*(\d{1,3})", raw, flags=re.IGNORECASE)
    if match:
        return max(0, min(100, int(match.group(1))))
    for key, value in PRIORITY_WORDS.items():
        if key in raw:
            return value
    return 0


def first_date(raw: str) -> str:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
    if match:
        return match.group(1)
    return date.today().isoformat()


def dependency(raw: str) -> tuple[str, ...]:
    match = re.search(r"dependency\s*[:=]\s*\[([^\]]*)\]", raw, flags=re.IGNORECASE)
    if not match:
        return ()
    return tuple(item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip())


def explicit_id(raw: str, fallback: str) -> str:
    match = re.search(r"(?:entry_id|id)\s*[:=]\s*([A-Za-z0-9_.-]+)", raw, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return fallback


def parse_open_threads(ledger_text: str) -> list[Thread]:
    block = section_between(ledger_text, "## 三、未完成线索", "## 四、未来探索方向")
    threads: list[Thread] = []
    idx = 0
    for line in block.splitlines():
        raw = line.strip()
        if not raw or "---" in raw:
            continue
        if not (raw.startswith("|") or raw.startswith("-")):
            continue
        if "优先级" in raw and "描述" in raw:
            continue
        if "已完成" in raw or "done" in raw.lower():
            continue
        idx += 1
        parts = [part.strip() for part in raw.strip("|").split("|")]
        description = parts[1] if len(parts) >= 2 and raw.startswith("|") else raw.lstrip("- ").strip()
        threads.append(
            Thread(
                entry_id=explicit_id(raw, f"open-thread-{idx}"),
                priority=priority_value(raw),
                last_updated=first_date(raw),
                description=description,
                dependency=dependency(raw),
                raw=raw,
            )
        )
    return threads


def dependency_met(dep: str, ledger_text: str) -> bool:
    adopted = section_between(ledger_text, "## 一、已采用方向", "## 二、已确认无效")
    return dep in adopted


def is_rejected_duplicate(thread: Thread, ledger_text: str) -> bool:
    rejected = section_between(ledger_text, "## 二、已确认无效", "## 三、未完成线索")
    tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9_]{4,}|[\u4e00-\u9fff]{2,}", thread.description)}
    if not tokens:
        return False
    rejected_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9_]{4,}|[\u4e00-\u9fff]{2,}", rejected)}
    score = len(tokens & rejected_tokens) / max(1, len(tokens))
    return score >= 0.65


def select_next(ledger_text: str) -> str:
    threads = parse_open_threads(ledger_text)
    eligible = [
        thread
        for thread in threads
        if all(dependency_met(dep, ledger_text) for dep in thread.dependency)
        and not is_rejected_duplicate(thread, ledger_text)
    ]
    if not eligible:
        return "EMPTY"
    selected = sorted(eligible, key=lambda t: (-t.priority, t.last_updated, t.entry_id))[0]
    return selected.entry_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Select next quant research thread")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--mode", choices=("A", "B"), default="B")
    args = parser.parse_args()

    if args.mode != "B":
        print("EMPTY")
        return 0
    print(select_next(read_text(args.ledger)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
