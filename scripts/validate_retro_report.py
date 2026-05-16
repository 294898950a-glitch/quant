#!/usr/bin/env python3
"""Validate reports/*.md 复盘 / diagnostic markdown 报告基本结构.

按用户 2026-05-17 提出 framework Q2-C 漏洞: pre-commit hook 没覆盖 reports/*,
reports/*.md 没 validator, AI 可以"轻易绕过"乱写 retro.

本 validator 是轻量结构检查 (不限制内容自由度), 只确保几个基本元素必有:
- H1 标题 (# xxx)
- 日期 (YYYY-MM-DD 格式出现至少一次)
- "结论" / "Conclusion" / "总评" / "判断" 段落标题之一 (确保有结论 section)
- 引用源 (出现至少 1 个文件引用: ledger/baseline_registry/reports/data/scripts/docs)

跳过:
- 长度 < 200 字符的 stub (e.g. TODO/draft skeleton)
- 文件名包含 'template' / 'example'

Usage:
  python3 scripts/validate_retro_report.py            # check all reports/*.md
  python3 scripts/validate_retro_report.py path.md    # single

Exit codes:
  0 = OK
  1 = structural error
  2 = operational error
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"

CONCLUSION_KEYWORDS = (
    "结论", "总评", "判断", "Conclusion", "Summary", "总结", "核心结论", "评判",
)

CITATION_PATTERNS = (
    r"\b(?:ledger|baseline_registry|tried_directions|experience_ledger)\b",
    r"reports/\S+\.md",
    r"data/\S+",
    r"scripts/\S+",
    r"docs/\S+",
)


def validate_one(path: Path) -> list[str]:
    errors = []
    try:
        text = path.read_text(encoding="utf-8")
    except (IOError, UnicodeDecodeError) as e:
        return [f"read error: {e}"]

    # 跳过 stub (太短)
    if len(text) < 200:
        return []  # 太短, 不当 retro 处理, 跳过
    # 跳过 template
    if "template" in path.name.lower() or "example" in path.name.lower():
        return []

    # 1. H1 标题
    if not re.search(r"^# .{3,}", text, re.MULTILINE):
        errors.append("missing H1 title (# xxx)")

    # 2. 日期 YYYY-MM-DD
    if not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text):
        errors.append("missing date (YYYY-MM-DD format)")

    # 3. 结论 section (soft warning, 不 fatal — 老 retro 可能用别的措辞)
    has_conclusion = False
    for kw in CONCLUSION_KEYWORDS:
        if re.search(rf"^#+ .*{re.escape(kw)}", text, re.MULTILINE | re.IGNORECASE):
            has_conclusion = True
            break
        if re.search(rf"\*\*{re.escape(kw)}\*\*[::]", text, re.IGNORECASE):
            has_conclusion = True
            break
    # conclusion 缺 → 不加 errors (soft warn, 由 caller 决定要不要打印)

    # 4. 至少 1 个引用源
    has_citation = False
    for pattern in CITATION_PATTERNS:
        if re.search(pattern, text):
            has_citation = True
            break
    if not has_citation:
        errors.append(
            "missing citation (至少引用 1 个: ledger/baseline_registry/reports/path/data/path/scripts/path/docs/path)"
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate retro reports/*.md")
    parser.add_argument("path", nargs="?", type=Path, default=None,
                        help="single file (optional, 默认扫 reports/*.md)")
    args = parser.parse_args()

    if args.path is not None:
        if not args.path.exists():
            print(f"ERROR: {args.path} 不存在", file=sys.stderr)
            return 2
        targets = [args.path]
    else:
        if not REPORTS_DIR.exists():
            print(f"validate_retro_report.py: reports/ 不存在, skip")
            return 0
        # 按 Codex 01:12 review: 加 recursive 防 reports/subdir/*.md 绕过
        targets = sorted(REPORTS_DIR.rglob("*.md"))

    total_errors = []
    checked = 0
    for path in targets:
        errors = validate_one(path)
        checked += 1
        if errors:
            try:
                rel = path.relative_to(REPO_ROOT)
            except ValueError:
                rel = path
            for e in errors:
                total_errors.append(f"{rel}: {e}")

    print(f"validate_retro_report.py: {checked} report(s) checked")
    if total_errors:
        print(f"\nFATAL: {len(total_errors)} 结构问题:")
        for e in total_errors:
            print(f"  {e}")
        return 1

    print(f"  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
