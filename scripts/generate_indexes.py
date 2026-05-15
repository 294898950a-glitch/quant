#!/usr/bin/env python3
"""Auto-generate docs/INDEX.md and reports/INDEX.md by scanning the repo.

Replaces manual index maintenance. Run after adding/moving files.

Usage:
  python3 scripts/generate_indexes.py             # write both indexes
  python3 scripts/generate_indexes.py --dry-run   # print, don't write
  python3 scripts/generate_indexes.py --docs      # only docs/INDEX.md
  python3 scripts/generate_indexes.py --reports   # only reports/INDEX.md

Classification rules:
- docs:
  - protocol_*.md → 协议
  - HDRF.md / questioning_checklist.md → 流程
  - *_role.md → 角色
  - *_template.md / run_manifest_schema.md → 模板
  - CURRENT.md / experience_ledger.md → 真值
  - data_source_summary.md / others → 其他
- reports:
  - cb_arb_* → cb_arb (按日期排序)
  - cb_redemption_* → cb_redemption
  - autonomous_summary_* → 自治总结
  - cb_data_*, cb_pricer_* → 数据验证
  - 其他 → 未分类

Triggered by:
- Manual run
- Optional: git post-commit hook (phase 1.5)
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_INDEX = REPO_ROOT / "docs" / "INDEX.md"
REPORTS_INDEX = REPO_ROOT / "reports" / "INDEX.md"
DOCS_FRAMEWORK = REPO_ROOT / "docs" / "research_framework"
DATA_FRAMEWORK = REPO_ROOT / "data" / "research_framework"
REPORTS = REPO_ROOT / "reports"


def classify_doc(filename: str) -> str:
    """Return category for a docs/research_framework/*.md file."""
    name = filename.lower()
    if name.endswith("_role.md"):
        return "角色定义"
    if name.endswith("_template.md") or name == "run_manifest_schema.md":
        return "模板"
    if name.startswith("protocol_") or name.endswith("_protocol.md"):
        return "协议层"
    if name in ("hdrf.md", "questioning_checklist.md"):
        return "流程层"
    if name in ("current.md", "experience_ledger.md"):
        return "真值层"
    return "其他"


# --- Reports classification ---

REPORTS_STRATEGY_PREFIXES = [
    ("cb_arb", "cb_arb 转债套利"),
    ("cb_redemption", "cb_redemption 强赎策略"),
    ("autonomous_summary", "每日自治总结"),
    ("cb_data", "数据验证"),
    ("cb_pricer", "数据验证"),
]


def classify_report(filename: str) -> str:
    name = filename.lower()
    for prefix, label in REPORTS_STRATEGY_PREFIXES:
        if name.startswith(prefix):
            return label
    return "未分类"


DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{8})")


def extract_date(filename: str) -> str:
    m = DATE_RE.search(filename)
    if not m:
        return ""
    raw = m.group(1)
    if len(raw) == 8 and "-" not in raw:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


# --- Generators ---


def generate_docs_index() -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Scan docs/research_framework
    framework_files = sorted([f.name for f in DOCS_FRAMEWORK.glob("*.md")])
    categories: dict[str, list[str]] = {}
    for f in framework_files:
        cat = classify_doc(f)
        categories.setdefault(cat, []).append(f)

    # Scan data/research_framework (truth/data assets)
    data_files = sorted([f.name for f in DATA_FRAMEWORK.glob("*.md")] +
                        [f.name for f in DATA_FRAMEWORK.glob("*.yaml")] +
                        [f.name for f in DATA_FRAMEWORK.glob("*.txt")])

    # Scan scripts (validator/tool inventory)
    scripts_dir = REPO_ROOT / "scripts"
    validators = sorted([f.name for f in scripts_dir.glob("validate_*.py")] +
                        [f.name for f in scripts_dir.glob("framework_preflight.py")] +
                        [f.name for f in scripts_dir.glob("install_pre_commit_hook.sh")])
    queries = sorted([f.name for f in scripts_dir.glob("get_baseline.py")] +
                     [f.name for f in scripts_dir.glob("search_ledger.py")] +
                     [f.name for f in scripts_dir.glob("snapshot_current_state.py")] +
                     [f.name for f in scripts_dir.glob("generate_indexes.py")])
    automation = sorted([f.name for f in scripts_dir.glob("backfill_run_manifests.py")] +
                        [f.name for f in scripts_dir.glob("process_quant_claude_outbox.py")])

    lines = [
        "# 文档地图 (INDEX)",
        "",
        f"**最后生成**: {now} (由 `scripts/generate_indexes.py` 自动扫描生成, 不要手工编辑)",
        f"**触发**: 加新文件后跑 `python3 scripts/generate_indexes.py` 重新生成",
        "",
        "---",
        "",
        "## 入口顺序 (新会话先看这 4 个)",
        "",
        "1. **`docs/research_framework/CURRENT.md`** — 当前真值, 每个策略状态 / 当前成绩 / 下一步等谁",
        "2. **`docs/INDEX.md`** — 本文件, 文档地图",
        "3. **`data/research_framework/baseline_registry.md`** — 成绩单档案, 历史每次回测出的数字 (immutable-ish)",
        "4. **`docs/research_framework/experience_ledger.md`** — 经验账本 (4 分区: 已采用 / 已无效 / 未完成 / 未来)",
        "",
        "看完这 4 个文件就知道 \"现在该做什么\". 其他文件按需翻.",
        "",
        "---",
        "",
    ]

    # Render categories in fixed order
    category_order = ["协议层", "流程层", "角色定义", "模板", "真值层", "其他"]
    for cat in category_order:
        if cat not in categories:
            continue
        lines.append(f"## {cat}")
        lines.append("")
        for f in categories[cat]:
            lines.append(f"- `docs/research_framework/{f}`")
        lines.append("")

    lines.append("## 真值数据 (data/research_framework/)")
    lines.append("")
    for f in data_files:
        lines.append(f"- `data/research_framework/{f}`")
    lines.append("")

    lines.append("## 自动校验工具 (commit 前自动跑)")
    lines.append("")
    for f in validators:
        lines.append(f"- `scripts/{f}`")
    lines.append("")

    lines.append("## 查询工具")
    lines.append("")
    for f in queries:
        lines.append(f"- `scripts/{f}`")
    lines.append("")

    lines.append("## 自动化脚本")
    lines.append("")
    for f in automation:
        lines.append(f"- `scripts/{f}`")
    lines.append("")

    lines.append("## 报告")
    lines.append("")
    lines.append("- `reports/INDEX.md` — 按策略 + 日期分类的报告索引 (也由 generate_indexes.py 自动生成)")
    lines.append("")

    lines.append("## 计划")
    lines.append("")
    plans_dir = REPO_ROOT / "docs" / "plans"
    if plans_dir.is_dir():
        for f in sorted(plans_dir.glob("*.md")):
            lines.append(f"- `docs/plans/{f.name}`")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## stable IDs (路径迁移用)")
    lines.append("")
    lines.append("未来若移文件位置, 用 stable ID 引用而不是具体路径:")
    lines.append("")
    lines.append("- strategy: 按 `data/research_framework/strategies.yaml` 的 `id` 字段")
    lines.append("- baseline: 按 `data/research_framework/baseline_registry.md` 的 `pk` 字段 (e.g. `cb_arb-main-yaml-current-20260515`)")
    lines.append("- hypothesis: 按 `strategies.yaml` 的 `hypotheses.id`")
    lines.append("- 报告: 按 \"策略 + 日期 + slug\" (e.g. `cb_arb/2026-05-15/panic-diagnostic`)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 自动生成规则")
    lines.append("")
    lines.append("本文件由 `scripts/generate_indexes.py` 扫描以下位置自动生成:")
    lines.append("- `docs/research_framework/*.md` — 按文件名分类")
    lines.append("- `data/research_framework/{*.md, *.yaml, *.txt}` — 真值/配置数据")
    lines.append("- `scripts/{validate_*, framework_preflight, get_baseline, ...}.py` — 工具脚本")
    lines.append("- `docs/plans/*.md` — 计划")
    lines.append("")
    lines.append("分类规则在脚本里的 `classify_doc()` 函数. 改分类规则时改脚本, 不改本文件.")
    return "\n".join(lines) + "\n"


def generate_reports_index() -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    all_files = sorted([f for f in REPORTS.iterdir() if f.is_file() and f.name not in ("INDEX.md",)])

    by_category: dict[str, list[tuple[str, str]]] = {}
    for path in all_files:
        category = classify_report(path.name)
        date = extract_date(path.name)
        by_category.setdefault(category, []).append((date, path.name))

    # Sort each category by date desc, then filename
    for category in by_category:
        by_category[category].sort(key=lambda x: (x[0] or "", x[1]), reverse=True)

    lines = [
        "# 复盘报告索引 (自动生成)",
        "",
        f"**最后生成**: {now} (由 `scripts/generate_indexes.py` 自动扫描生成)",
        f"**触发**: 加新报告后跑 `python3 scripts/generate_indexes.py` 重新生成",
        "",
        "报告物理位置仍在 `reports/` 平铺 (避免破坏 35+ 处现有引用). 按策略找请用本索引.",
        "",
        "---",
        "",
    ]

    category_order = [
        "cb_arb 转债套利",
        "cb_redemption 强赎策略",
        "每日自治总结",
        "数据验证",
        "未分类",
    ]
    for cat in category_order:
        if cat not in by_category:
            continue
        lines.append(f"## {cat}")
        lines.append("")
        # Group by date
        by_date: dict[str, list[str]] = {}
        for date, fname in by_category[cat]:
            by_date.setdefault(date or "无日期", []).append(fname)
        for date in sorted(by_date.keys(), reverse=True):
            if date != "无日期":
                lines.append(f"### {date}")
                lines.append("")
            for fname in sorted(by_date[date]):
                lines.append(f"- [{fname}]({fname})")
            lines.append("")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## arxiv 候选 (在 data/research_framework/paper_candidates/)")
    lines.append("")
    paper_dir = DATA_FRAMEWORK / "paper_candidates"
    if paper_dir.is_dir():
        for f in sorted(paper_dir.glob("*.md")):
            lines.append(f"- `data/research_framework/paper_candidates/{f.name}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 自动生成规则")
    lines.append("")
    lines.append("本文件由 `scripts/generate_indexes.py` 扫描 `reports/` 自动生成:")
    lines.append("- 按文件名 prefix 分类 (cb_arb_* / cb_redemption_* / autonomous_summary_* / cb_data_* / cb_pricer_*)")
    lines.append("- 按文件名内日期 (YYYY-MM-DD 或 YYYYMMDD) 排序倒序 + 分组")
    lines.append("- 改分类规则在脚本里的 `classify_report()` + `REPORTS_STRATEGY_PREFIXES`")
    return "\n".join(lines) + "\n"


# --- Main ---


def _strip_timestamp(content: str) -> str:
    """Remove auto-generated timestamps for diff comparison."""
    return re.sub(r"\*\*最后生成\*\*:.*?\n", "", content)


def check_outdated() -> tuple[bool, list[str]]:
    """Return (any_outdated, list_of_outdated_files). Compares timestamp-stripped content."""
    outdated = []
    for path, generator in [(DOCS_INDEX, generate_docs_index), (REPORTS_INDEX, generate_reports_index)]:
        new_content = _strip_timestamp(generator())
        if not path.exists():
            outdated.append(str(path.relative_to(REPO_ROOT)))
            continue
        existing = _strip_timestamp(path.read_text(encoding="utf-8"))
        if existing != new_content:
            outdated.append(str(path.relative_to(REPO_ROOT)))
    return (bool(outdated), outdated)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print, don't write")
    parser.add_argument("--docs", action="store_true", help="only docs index")
    parser.add_argument("--reports", action="store_true", help="only reports index")
    parser.add_argument("--check", action="store_true", help="check if outdated (exit 1 if yes)")
    args = parser.parse_args()

    if args.check:
        outdated_flag, files = check_outdated()
        if outdated_flag:
            print(f"OUTDATED: {len(files)} index file(s) need regeneration:")
            for f in files:
                print(f"  - {f}")
            print(f"Run: python3 scripts/generate_indexes.py")
            return 1
        print("indexes up-to-date")
        return 0

    do_docs = args.docs or not args.reports
    do_reports = args.reports or not args.docs

    if do_docs:
        content = generate_docs_index()
        if args.dry_run:
            print("=== docs/INDEX.md (dry-run) ===")
            print(content[:2000])
            print("... (truncated)" if len(content) > 2000 else "")
        else:
            DOCS_INDEX.write_text(content, encoding="utf-8")
            print(f"Wrote {DOCS_INDEX.relative_to(REPO_ROOT)} ({len(content)} chars)")

    if do_reports:
        content = generate_reports_index()
        if args.dry_run:
            print("=== reports/INDEX.md (dry-run) ===")
            print(content[:2000])
            print("... (truncated)" if len(content) > 2000 else "")
        else:
            REPORTS_INDEX.write_text(content, encoding="utf-8")
            print(f"Wrote {REPORTS_INDEX.relative_to(REPO_ROOT)} ({len(content)} chars)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
