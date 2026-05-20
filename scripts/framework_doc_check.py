#!/usr/bin/env python3
"""Post-write document check (实时验证, AI 自救窗口).

按用户 2026-05-17 提出 framework 新需求: commit 时才查不够, AI 写完那一刻
就该检查; 错了立即弹给 AI, 不带着错继续往下做.

Usage:
  python3 scripts/framework_doc_check.py <path>

路径推 schema:
- *.md except AGENTS.md/CLAUDE.md → validate_entrypoints.py (Markdown is not allowed)
- data/<run-id>/spec.yaml → validate_spec.py
- data/<run-id>/l4_ack.yaml → validate_l4_ack.py --run-dir
- data/<run-id>/diagnostic.yaml → validate_l5_diagnostic.py --run-dir
- data/research_framework/current.yaml → validate_current_yaml.py
- data/research_framework/baseline_registry.yaml → validate_baseline_registry.py
- data/research_framework/runtime_entrypoints.yaml → validate_entrypoints.py
- data/research_framework/protocol_rules.yaml → validate_entrypoints.py
- data/research_framework/experiments.yaml → validate_entrypoints.py
- data/research_framework/truth_sync_waivers/*.yaml → validate_truth_sync.py
- data/research_framework/strategies.yaml → validate_spec.py (依赖检查)
- AGENTS.md / CLAUDE.md → validate_entrypoints.py
- 其他路径 → skip (不在 framework 受管范围)

设计:
- 本工具自成一体, 不依赖 GateKeeper
- 由 pre-commit hook 或 AI 主动调用
- 失败 (exit != 0) 时, AI 工作流必须看到错并修
- 不再保留后台实时 watcher; 项目推进只走内部定时入口

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
TRUTH_SYNC_PATHS = {
    "data/research_framework/current.yaml",
    "data/research_framework/baseline_registry.yaml",
    "data/research_framework/strategies.yaml",
}


def is_markdown_artifact(path: Path) -> bool:
    name = path.name
    return name.endswith(".md") or ".md." in name


def dispatch(path: Path) -> tuple[str, list[str]]:
    """根据 path 推 (validator_script, args). 返回 ('', []) 表示 skip."""
    # 兼容相对路径 (用户手动调可能传 cwd 相对) + 绝对路径
    abs_path = path.resolve()
    try:
        rel = abs_path.relative_to(REPO_ROOT)
    except ValueError:
        # path 不在 repo 内, skip
        return ("", [])

    rel_str = str(rel)
    path = abs_path  # 后续 args 用绝对路径, 避免子 validator cwd 歧义

    allowed_markdown = {"AGENTS.md", "CLAUDE.md"}

    if is_markdown_artifact(rel) and rel_str not in allowed_markdown:
        return ("validate_entrypoints.py", [])

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

    if rel_str == "data/research_framework/current.yaml":
        return ("validate_current_yaml.py", [])

    if rel_str in {
        "data/research_framework/runtime_entrypoints.yaml",
        "data/research_framework/protocol_rules.yaml",
        "data/research_framework/experiments.yaml",
    }:
        return ("validate_entrypoints.py", [])

    # data/research_framework/truth_sync_waivers/*.yaml
    if rel_str.startswith("data/research_framework/truth_sync_waivers/") and rel_str.endswith((".yaml", ".yml")):
        return ("validate_truth_sync.py", ["--waivers-only"])

    # 按 Codex 01:26 review: data/research_framework/run_manifests/*.yaml 漏了
    if rel_str.startswith("data/research_framework/run_manifests/") and rel_str.endswith(".yaml"):
        return ("validate_run_manifest.py", [])

    if rel_str in allowed_markdown:
        return ("validate_entrypoints.py", [])

    # scripts/*.py 改的话也走 validate_gatekeeper_compliance + entrypoints
    # (脚本改动不算 doc 写, 但安全起见 framework_preflight 已 cover 在 commit 时)

    # 其他: skip
    return ("", [])


def needs_truth_sync_check(path: Path) -> bool:
    abs_path = path.resolve()
    try:
        rel = abs_path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    rel_str = str(rel)
    return rel_str in TRUTH_SYNC_PATHS or (
        rel_str.startswith("data/research_framework/run_manifests/")
        and rel_str.endswith(".yaml")
    )


def run_validator(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(SCRIPTS / script)] + args
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post-write document check (real-time, AI 自救窗口)"
    )
    parser.add_argument("path", type=Path, help="刚写的文件路径")
    parser.add_argument("--quiet", action="store_true",
                        help="只在 fatal 时打印 (适合 hook 调用)")
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

    return_code = run_validator(validator, validator_args)

    if return_code == 0:
        if not args.quiet:
            print(f"  ✓ {validator} OK")
    elif return_code == 2 and validator == "validate_data_schema.py":
        # warn-only, 不当 fatal
        if not args.quiet:
            print(f"  ⚠ {validator} 警告 (warn-only)")
    else:
        print(f"  ✗ {validator} FATAL (exit {return_code})", file=sys.stderr)
        print(f"  AI: 立即修这个文件, 不要带着错继续往下做.", file=sys.stderr)
        return 1

    if needs_truth_sync_check(args.path):
        if not args.quiet:
            print(f"framework_doc_check.py: 触发 validate_truth_sync.py on {args.path}")
        truth_rc = run_validator("validate_truth_sync.py", [])
        if truth_rc != 0:
            print(f"  ✗ validate_truth_sync.py FATAL (exit {truth_rc})", file=sys.stderr)
            print(f"  AI: 立即修这个文件, 不要带着错继续往下做.", file=sys.stderr)
            return 1
        if not args.quiet:
            print("  ✓ validate_truth_sync.py OK")

    return 0


if __name__ == "__main__":
    sys.exit(main())
