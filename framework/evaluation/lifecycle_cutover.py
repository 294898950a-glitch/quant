"""Deterministic gate for compare-to-authoritative lifecycle read cutover."""
from __future__ import annotations

import json
import fcntl
from pathlib import Path
from typing import Any


def compare_window(diff_path: Path | str, minimum_cycles: int = 30) -> tuple[bool, str, int]:
    """Return eligibility from unique run-id compare facts in append order."""
    path = Path(diff_path)
    if minimum_cycles < 1:
        raise ValueError("minimum_cycles must be positive")
    rows: list[dict[str, Any]] = []
    if path.exists():
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            text = path.read_text(encoding="utf-8")
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("run_id"), str):
                rows.append(row)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reversed(rows):
        run_id = str(row["run_id"])
        if run_id not in seen:
            seen.add(run_id); unique.append(row)
    unique.reverse()
    trailing = 0
    for row in reversed(unique):
        if row.get("differences"):
            break
        trailing += 1
    if trailing < minimum_cycles:
        return False, f"requires {minimum_cycles} consecutive zero-diff cycles; has {trailing}", trailing
    return True, "cutover window satisfied", trailing
