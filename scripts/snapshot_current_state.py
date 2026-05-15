#!/usr/bin/env python3
"""Snapshot CURRENT.md overview into memory/project_current_state.md (P1.5 spec).

Usage:
  python3 scripts/snapshot_current_state.py
  python3 scripts/snapshot_current_state.py --dry-run

Reads CURRENT.md overview section, generates a compact snapshot for Claude
memory at ~/.claude/projects/<>/memory/project_current_state.md.

Phase 1: manual trigger (run after committing CURRENT.md changes).
Phase 1.5: auto via git post-commit hook.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_MD = REPO_ROOT / "docs" / "research_framework" / "CURRENT.md"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-jay-projects-quant" / "memory"
SNAPSHOT = MEMORY_DIR / "project_current_state.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not CURRENT_MD.exists():
        print(f"ERROR: {CURRENT_MD} missing", file=sys.stderr)
        return 1

    text = CURRENT_MD.read_text(encoding="utf-8")

    # Extract overview section (## 总览 → next ##)
    overview_match = re.search(r"## 总览\n(.+?)(?=\n## |\Z)", text, re.DOTALL)
    if not overview_match:
        print("ERROR: 总览 section missing", file=sys.stderr)
        return 1
    overview = overview_match.group(1).strip()

    # Count active strategies (front-matter status: wip|adopted)
    active_count = len(re.findall(r"^status:\s*(wip|adopted)\b", text, re.MULTILINE))
    deployed_count = len(re.findall(r"^status:\s*adopted\b", text, re.MULTILINE))

    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M %z").strip()
    content = f"""---
name: project-current-state
description: 当前项目策略状态快照 (auto-generated from CURRENT.md by snapshot_current_state.py)
metadata:
  type: project
  generated_at: {timestamp}
  source: docs/research_framework/CURRENT.md
---

# 当前项目状态快照

**生成时间**: {timestamp}
**来源**: `docs/research_framework/CURRENT.md`
**自动生成**: 运行 `python3 scripts/snapshot_current_state.py` 重新生成

## 状态指标

- **active strategies (wip/adopted)**: {active_count}
- **deployable (adopted) strategies**: {deployed_count}

## CURRENT.md 总览段

{overview}

---

详细策略状态见 `docs/research_framework/CURRENT.md`. 当前 baseline 数字见
`data/research_framework/baseline_registry.md` (用 `python3 scripts/get_baseline.py --strategy <id>` 工具查).
"""

    if args.dry_run:
        print(content)
        print(f"\n(dry-run, would write to: {SNAPSHOT})")
        return 0

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(content, encoding="utf-8")
    print(f"Wrote {SNAPSHOT}")

    # Also update MEMORY.md index if entry not present
    memory_index = MEMORY_DIR / "MEMORY.md"
    if memory_index.exists():
        idx_text = memory_index.read_text(encoding="utf-8")
        entry = "- [Project current state](project_current_state.md) — auto-generated snapshot of docs/research_framework/CURRENT.md (当前策略真值)"
        if "project_current_state.md" not in idx_text:
            with memory_index.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
            print(f"Appended index entry to {memory_index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
