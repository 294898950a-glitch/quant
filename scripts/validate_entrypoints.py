#!/usr/bin/env python3
"""Validate the hard-coded new-session entrypoint contract."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

ENTRY_FILES = [
    "docs/research_framework/CURRENT.md",
    "data/research_framework/baseline_registry.md",
    "docs/INDEX.md",
]

CHECK_FILES = [
    "README.md",
    "docs/INDEX.md",
    "docs/research_framework/HDRF.md",
    "docs/research_framework/CURRENT.md",
]

# Files that must mention "3 个入口" or "3 入口" (not "4 个入口" or "唯一入口")
ENTRY_COUNT_CHECK = [
    "README.md",
    "docs/INDEX.md",
    "docs/research_framework/CURRENT.md",
    "docs/research_framework/HDRF.md",
]

NO_REPLY_DEFAULT_FILES = [
    "README.md",
    "docs/research_framework/CURRENT.md",
    "docs/research_framework/protocol_redline.md",
    "docs/research_framework/autonomous_loop_protocol.md",
]

REDLINE_SYNC_FILES = [
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/research_framework/protocol_redline.md",
    "docs/research_framework/autonomous_loop_protocol.md",
]

STALE_RULE_PHRASES = [
    "起 spot 前用户最后批",
    "必须用户最后批准",
    "grid / backtest / VM 一行命令都不发",
    "永远人工",
    "不能自决起 spot",
    "月预算: ¥90",
    "模式 B 沙盒",
    "沙盒约束",
    "新增数据/改核心代码/加预算仍停等用户",
    "若需要新增数据源、改变核心代码、提高预算",
    "当前策略族",
    "R3 实时",
    "红线 1: 写文档",
    "7 条红线",
    "OS 文件系统层 (cross-AI 真硬)",
]


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def ordered(text: str, needles: list[str]) -> bool:
    pos = -1
    for needle in needles:
        next_pos = text.find(needle, pos + 1)
        if next_pos < 0:
            return False
        pos = next_pos
    return True


def main() -> int:
    issues: list[str] = []
    readme = read("README.md")
    docs_index = read("docs/INDEX.md")

    for path, text in (("README.md", readme), ("docs/INDEX.md", docs_index)):
        if not ordered(text, ENTRY_FILES):
            issues.append(f"{path}: new-session entry files are missing or out of order")
        if "非入口文件" not in text:
            issues.append(f"{path}: must explicitly label non-entry files as 非入口文件")

    for path in CHECK_FILES:
        text = read(path)
        if "参考层" in text:
            issues.append(f"{path}: forbidden soft label 参考层 remains")

    # Forbid stale wording about single entry, in core docs.
    # 注: "4 入口"在 HDRF L0 历史日志里有合法用法 (v3.1 第 4 入口), 不 forbid.
    for path in ENTRY_COUNT_CHECK:
        text = read(path)
        if "唯一权威入口" in text or "唯一入口" in text:
            issues.append(f"{path}: stale entry wording (唯一入口) — should say one of 3 个入口")

    for path in NO_REPLY_DEFAULT_FILES:
        text = read(path)
        if "30 分钟" not in text or "当前策略" not in text or "换一个研究方向" not in text:
            issues.append(f"{path}: missing 30-minute no-reply default rule")
        if "¥100" not in text or "scripts/estimate_compute_budget.py" not in text:
            issues.append(f"{path}: no-reply rule must include mechanical budget calculator and ¥100 auto limit")
        if "当前策略族" in text:
            issues.append(f"{path}: no-reply rule must use parameter 当前策略, not fixed 当前策略族")
        if "新增数据/改核心代码/加预算仍停等用户" in text or "若需要新增数据源、改变核心代码、提高预算" in text:
            issues.append(f"{path}: stale no-reply blocker remains for new data/core code/budget")

    for path in REDLINE_SYNC_FILES:
        text = read(path)
        for phrase in STALE_RULE_PHRASES:
            if phrase in text:
                issues.append(f"{path}: stale redline wording remains: {phrase}")

    for path in ("AGENTS.md", "CLAUDE.md", "docs/research_framework/protocol_redline.md"):
        text = read(path)
        if "¥100" not in text or "estimate_compute_budget.py" not in text:
            issues.append(f"{path}: current autonomy rule must mention budget calculator and ¥100")
        if "实盘" not in text or "归档" not in text:
            issues.append(f"{path}: current autonomy rule must preserve no-auto-live/no-auto-archive boundary")

    if issues:
        for issue in issues:
            print(issue)
        return 1

    print("validate_entrypoints.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
