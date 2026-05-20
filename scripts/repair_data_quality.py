#!/usr/bin/env python3
"""Generate and run a run-local data repair package through the registered AI provider."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_PROVIDERS = REPO_ROOT / "data" / "research_framework" / "ai_providers.yaml"
ENTRYPOINT = "scripts/repair_data_quality.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.ai_provider_adapter import RegisteredProviderAdapter  # noqa: E402
from scripts.quant_access_guard import require_ticket  # noqa: E402
from scripts.validate_data_quality import compact_for_ai, summarize_data_quality  # noqa: E402


ALLOWED_IMPORT_ROOTS = {"argparse", "json", "pathlib", "pandas", "pyarrow", "shutil", "yaml"}
FORBIDDEN_ATTRS = {
    "chdir",
    "chmod",
    "chown",
    "exec",
    "execv",
    "execve",
    "fork",
    "kill",
    "move",
    "popen",
    "remove",
    "removedirs",
    "rename",
    "replace",
    "rmdir",
    "rmtree",
    "spawn",
    "system",
    "unlink",
}
FORBIDDEN_NAMES = {"eval", "exec", "open", "__import__", "compile", "input"}


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def ensure_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{rel(path)} root must be dict")
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def command_value(command: list[Any], flag: str) -> str:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[index + 1]
    raise ValueError(f"automation.command missing {flag}")


def update_command_path(command: list[Any], flag: str, value: str) -> list[str]:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            parts[index + 1] = value
            return parts
    raise ValueError(f"automation.command missing {flag}")


def warehouse_source(original_data_root: Path, rel_path: str) -> Path | None:
    for candidate in (original_data_root / rel_path, REPO_ROOT / rel_path):
        if candidate.exists():
            return candidate
    return None


def build_repair_context(spec_path: Path, decision_path: Path) -> dict[str, Any]:
    spec = read_yaml(spec_path)
    decision = read_yaml(decision_path)
    if str(decision.get("status") or "").lower() not in {"repair_candidate", "fixable"}:
        raise ValueError("data repair may run only for status=repair_candidate")
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    command = automation.get("command") or []
    if not isinstance(command, list):
        raise ValueError("spec automation.command must be list")

    data_root = ensure_path(command_value(command, "--data-root"))
    base_ranks = ensure_path(command_value(command, "--base-ranks-path"))
    prepared_root = spec_path.parent / "prepared_data"
    prepared_data_root = prepared_root / "data_root"
    prepared_ranks = prepared_root / "daily_value_gap_amounts.parquet"
    allowed_reads: dict[str, str] = {
        "base_ranks_path": rel(base_ranks),
    }
    for key, warehouse_rel in {
        "cb_basic": "data/cb_warehouse/cb_basic.parquet",
        "cb_daily": "data/cb_warehouse/cb_daily.parquet",
        "cb_call": "data/cb_warehouse/cb_call.parquet",
        "stk_daily_qfq": "data/cb_warehouse/stk_daily_qfq.parquet",
    }.items():
        source = warehouse_source(data_root, warehouse_rel)
        if source is not None:
            allowed_reads[key] = rel(source)

    return {
        "schema_version": 1,
        "repo_root": str(REPO_ROOT),
        "run_id": str(spec.get("run_id") or spec_path.parent.name),
        "spec_path": rel(spec_path),
        "decision_path": rel(decision_path),
        "decision": decision,
        "data_quality_summary": compact_for_ai(summarize_data_quality(spec_path)),
        "allowed_read_paths": allowed_reads,
        "allowed_write_root": rel(prepared_root),
        "prepared_data_root": rel(prepared_data_root),
        "prepared_base_ranks_path": rel(prepared_ranks),
        "required_outputs": {
            "base_ranks_path": rel(prepared_ranks),
            "cb_basic": rel(prepared_data_root / "data/cb_warehouse/cb_basic.parquet"),
            "stk_daily_qfq": rel(prepared_data_root / "data/cb_warehouse/stk_daily_qfq.parquet"),
            "report": rel(prepared_root / "data_fix_report.yaml"),
        },
    }


def repair_prompt(context: dict[str, Any], previous_errors: list[str]) -> str:
    retry_note = ""
    if previous_errors:
        retry_note = "\n前一次生成失败，必须修正这些错误：\n" + "\n".join(f"- {item}" for item in previous_errors[-6:])
    return (
        "你是量化实验的数据修复器。你的任务不是判断策略，也不是修改原始数据。\n"
        "你只为本轮实验生成一个 Python 修复脚本，把可修复的数据问题写入 run-local prepared_data。\n\n"
        "硬要求：\n"
        "1. 只能读取 allowed_read_paths 里的文件。\n"
        "2. 只能写入 allowed_write_root 目录下的文件。\n"
        "3. 不能覆盖 raw warehouse 或原始输入。\n"
        "4. 不能联网，不能调用 AI，不能删除 allowed_write_root 之外的任何文件。\n"
        "5. 脚本必须接受一个参数：--context repair_context.json。\n"
        "6. 脚本必须生成 required_outputs 里列出的输出；如果某个可选源文件不存在，可以跳过。\n"
        "7. 脚本必须写 data_fix_report.yaml，说明读了哪些文件、写了哪些文件、做了哪些字段修复。\n"
        "8. 修复目标只限本轮 fix_plan；常见操作是复制 parquet，并补齐字段别名。\n\n"
        "只返回 YAML，不要 Markdown。格式固定：\n"
        "status: repairable 或 unrepairable\n"
        "reason: 一句话\n"
        "files:\n"
        "  - path: generated_repair.py\n"
        "    content: |-\n"
        "      # python code here\n"
        "expected_outputs: 列表\n"
        f"{retry_note}\n\n"
        "修复上下文：\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n"
    )


def strip_markdown_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def call_repair_ai(context: dict[str, Any], previous_errors: list[str]) -> dict[str, Any]:
    adapter = RegisteredProviderAdapter(AI_PROVIDERS, repo_root=REPO_ROOT, entrypoint=ENTRYPOINT)
    response = adapter.call_active_provider(repair_prompt(context, previous_errors), schema={})
    payload = yaml.safe_load(strip_markdown_fence(response.content))
    if not isinstance(payload, dict):
        raise ValueError("AI repair response root must be YAML mapping")
    payload["ai_provider"] = response.provider_id
    payload["response_hash"] = response.response_hash
    return payload


def extract_repair_code(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").lower()
    if status != "repairable":
        raise ValueError(f"AI marked repair as {status or 'missing'}: {payload.get('reason')}")
    files = payload.get("files")
    if not isinstance(files, list):
        raise ValueError("repair payload files must be list")
    for item in files:
        if isinstance(item, dict) and str(item.get("path") or "") == "generated_repair.py":
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                return content
    raise ValueError("repair payload missing generated_repair.py content")


def validate_generated_code(code: str) -> None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in ALLOWED_IMPORT_ROOTS:
                    raise ValueError(f"generated repair imports forbidden module {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                raise ValueError(f"generated repair imports forbidden module {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
                raise ValueError(f"generated repair calls forbidden function {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ATTRS:
                raise ValueError(f"generated repair calls forbidden attribute {func.attr}")


def run_generated_repair(code: str, context: dict[str, Any], workspace: Path) -> dict[str, Any]:
    prepared_root = ensure_path(context["allowed_write_root"])
    if prepared_root.exists():
        shutil.rmtree(prepared_root)
    prepared_root.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    code_path = workspace / "generated_repair.py"
    context_path = workspace / "repair_context.json"
    code_path.write_text(code, encoding="utf-8")
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "HOME": str(workspace),
        "NO_PROXY": "*",
    }
    result = subprocess.run(
        [sys.executable, str(code_path), "--context", str(context_path)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    output = result.stdout or ""
    if result.returncode != 0:
        raise ValueError(f"generated repair failed rc={result.returncode}: {output[-3000:]}")
    return {"code_path": rel(code_path), "context_path": rel(context_path), "stdout": output[-3000:]}


def ensure_outputs(context: dict[str, Any]) -> None:
    for name, raw_path in (context.get("required_outputs") or {}).items():
        path = ensure_path(str(raw_path))
        if not path.exists():
            raise ValueError(f"generated repair missing required output {name}: {rel(path)}")


def update_spec_for_prepared_data(spec_path: Path, context: dict[str, Any], repair_payload: dict[str, Any]) -> dict[str, Any]:
    spec = read_yaml(spec_path)
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    command = automation.get("command") or []
    if not isinstance(command, list):
        raise ValueError("spec automation.command must be list")
    command = update_command_path(command, "--data-root", str(context["prepared_data_root"]))
    command = update_command_path(command, "--base-ranks-path", str(context["prepared_base_ranks_path"]))
    automation["command"] = command
    sync_paths = [str(path) for path in automation.get("sync_paths") or []]
    sync_paths.extend(
        [
            str(context["allowed_write_root"]),
            str(context["prepared_data_root"]),
            str(context["prepared_base_ranks_path"]),
        ]
    )
    seen: set[str] = set()
    automation["sync_paths"] = [path for path in sync_paths if not (path in seen or seen.add(path))]
    spec["automation"] = automation
    spec["data_quality_repair"] = {
        "status": "prepared",
        "mode": "ai_generated_repair_code",
        "prepared_root": str(context["allowed_write_root"]),
        "prepared_data_root": str(context["prepared_data_root"]),
        "prepared_base_ranks_path": str(context["prepared_base_ranks_path"]),
        "source_decision": str(context["decision_path"]),
        "ai_provider": repair_payload.get("ai_provider"),
        "response_hash": repair_payload.get("response_hash"),
    }
    write_yaml(spec_path, spec)
    return spec


def repair(spec_path: Path, decision_path: Path | None = None, attempts: int = 3) -> dict[str, Any]:
    decision_file = decision_path or spec_path.parent / "data_quality_decision.yaml"
    context = build_repair_context(spec_path, decision_file)
    workspace = spec_path.parent / "repair_workspace"
    errors: list[str] = []
    last_payload: dict[str, Any] = {}
    for _ in range(max(int(attempts), 1)):
        try:
            last_payload = call_repair_ai(context, errors)
            code = extract_repair_code(last_payload)
            validate_generated_code(code)
            execution = run_generated_repair(code, context, workspace)
            ensure_outputs(context)
            spec = update_spec_for_prepared_data(spec_path, context, last_payload)
            report_path = ensure_path(str((context.get("required_outputs") or {})["report"]))
            report = read_yaml(report_path)
            report.update(
                {
                    "schema_version": int(report.get("schema_version") or 1),
                    "run_id": context["run_id"],
                    "status": "prepared",
                    "spec_path": rel(spec_path),
                    "decision_path": rel(decision_file),
                    "prepared_root": context["allowed_write_root"],
                    "mode": "ai_generated_repair_code",
                    "ai_provider": last_payload.get("ai_provider"),
                    "response_hash": last_payload.get("response_hash"),
                    "principle": "original data was not overwritten; repaired inputs are run-local prepared data",
                    "execution": execution,
                }
            )
            write_yaml(report_path, report)
            return {
                "schema_version": 1,
                "run_id": context["run_id"],
                "status": "prepared",
                "spec_path": rel(spec_path),
                "prepared_root": context["allowed_write_root"],
                "prepared_data_root": context["prepared_data_root"],
                "prepared_base_ranks_path": context["prepared_base_ranks_path"],
                "data_fix_report": rel(report_path),
                "ai_provider": last_payload.get("ai_provider"),
                "response_hash": last_payload.get("response_hash"),
                "spec_updated": bool(spec.get("data_quality_repair")),
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    raise RuntimeError("AI data repair failed after retry loop: " + " | ".join(errors[-3:]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--decision")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    require_ticket("data_quality_repair")
    spec_path = ensure_path(args.spec)
    decision_path = ensure_path(args.decision) if args.decision else None
    try:
        report = repair(spec_path, decision_path, attempts=args.attempts)
    except Exception as exc:
        print(f"repair_data_quality.py: FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
