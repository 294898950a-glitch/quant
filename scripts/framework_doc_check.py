#!/usr/bin/env python3
"""Post-write document check (实时验证, AI 自救窗口).

按用户 2026-05-17 提出 framework 新需求: commit 时才查不够, AI 写完那一刻
就该检查; 错了立即弹给 AI, 不带着错继续往下做.

Usage:
  python3 scripts/framework_doc_check.py <path>

路径推 schema:
- reports/*.md → validate_retro_report.py --single
- data/<run-id>/spec.yaml → validate_spec.py
- data/<run-id>/l4_ack.yaml → validate_l4_ack.py --run-dir
- data/<run-id>/diagnostic.yaml → validate_l5_diagnostic.py --run-dir
- data/research_framework/baseline_registry.yaml → validate_baseline_registry.py
- data/research_framework/strategies.yaml → validate_spec.py (依赖检查)
- data/research_framework/compute_budget_config.json → validate_compute_budget.py
- docs/research_framework/*.md → validate_current_md.py / validate_entrypoints.py
- 其他路径 → skip (不在 framework 受管范围)

设计 (cross-AI, 完全解耦):
- 本工具自成一体, 不依赖 GateKeeper
- 由 2 层独立调用:
  1. framework_watch_daemon (cross-AI 实时, 任何 AI / 编辑器都触发)
  2. pre-commit hook (commit 前 retro 关, 防 daemon down)
- AI 主动调也可以 (e.g. user / Codex / etc 想立刻验证某个文件)
- 失败 (exit != 0) 时, AI 工作流必须看到错并修
- (原有 Claude Code PostToolUse hook 层 2026-05-17 删, 违反 cross-AI 哲学)

Exit codes:
  0 = OK (含 skip 非受管路径)
  1 = validator FATAL (AI 必须修)
  2 = operational error
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def dispatch(path: Path) -> tuple[str, list[str]]:
    """根据 path 推 (validator_script, args). 返回 ('', []) 表示 skip."""
    # 兼容相对路径 (daemon / 用户手动调可能传 cwd 相对) + 绝对路径
    abs_path = path.resolve()
    try:
        rel = abs_path.relative_to(REPO_ROOT)
    except ValueError:
        # path 不在 repo 内, skip
        return ("", [])

    rel_str = str(rel)
    path = abs_path  # 后续 args 用绝对路径, 避免子 validator cwd 歧义

    # reports/**/*.md
    if rel_str.startswith("reports/") and rel_str.endswith(".md"):
        return ("validate_retro_report.py", [str(path)])

    # data/<run-id>/spec.yaml
    if rel_str.startswith("data/") and rel.name == "spec.yaml":
        return ("validate_spec.py", [str(path)])

    # data/<run-id>/l4_ack.yaml
    if rel_str.startswith("data/") and rel.name == "l4_ack.yaml":
        run_dir = path.parent
        return ("validate_l4_ack.py", ["--run-dir", str(run_dir)])

    # data/<run-id>/diagnostic.yaml
    if rel_str.startswith("data/") and rel.name == "diagnostic.yaml":
        run_dir = path.parent
        return ("validate_l5_diagnostic.py", ["--run-dir", str(run_dir)])

    # data/research_framework/baseline_registry.yaml
    if rel_str == "data/research_framework/baseline_registry.yaml":
        return ("validate_baseline_registry.py", [])

    # data/research_framework/compute_budget_config.json
    if rel_str == "data/research_framework/compute_budget_config.json":
        return ("validate_compute_budget.py", [])

    # 按 Codex 01:26 review: data/research_framework/run_manifests/*.yaml 漏了
    if rel_str.startswith("data/research_framework/run_manifests/") and rel_str.endswith(".yaml"):
        return ("validate_run_manifest.py", [])

    # docs/research_framework/CURRENT.md / HDRF.md / etc
    if rel_str.startswith("docs/research_framework/") and rel_str.endswith(".md"):
        # validate_current_md 扫 CURRENT.md 等; 这里只是触发, 不传 path
        return ("validate_current_md.py", [])

    # scripts/*.py 改的话也走 validate_gatekeeper_compliance + entrypoints
    # (脚本改动不算 doc 写, 但安全起见 framework_preflight 已 cover 在 commit 时)

    # 其他: skip
    return ("", [])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post-write document check (real-time, AI 自救窗口)"
    )
    parser.add_argument("path", type=Path, help="刚写的文件路径")
    parser.add_argument("--quiet", action="store_true",
                        help="只在 fatal 时打印 (适合 daemon / hook 调用)")
    args = parser.parse_args()

    if not args.path.exists():
        # 文件被删除等情况, skip
        if not args.quiet:
            print(f"framework_doc_check.py: {args.path} 不存在, skip")
        return 0

    validator, validator_args = dispatch(args.path)
    if not validator:
        # 非受管路径, skip
        if not args.quiet:
            print(f"framework_doc_check.py: {args.path} 非受管路径, skip")
        return 0

    if not args.quiet:
        print(f"framework_doc_check.py: 触发 {validator} on {args.path}")

    cmd = [sys.executable, str(SCRIPTS / validator)] + validator_args
    result = subprocess.run(cmd, cwd=REPO_ROOT)

    if result.returncode == 0:
        if not args.quiet:
            print(f"  ✓ {validator} OK")
        return 0
    elif result.returncode == 2 and validator == "validate_data_schema.py":
        # warn-only, 不当 fatal
        if not args.quiet:
            print(f"  ⚠ {validator} 警告 (warn-only)")
        return 0
    else:
        print(f"  ✗ {validator} FATAL (exit {result.returncode})", file=sys.stderr)
        print(f"  AI: 立即修这个文件, 不要带着错继续往下做.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
