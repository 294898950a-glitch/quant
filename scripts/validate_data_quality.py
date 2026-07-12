#!/usr/bin/env python3
"""Summarize run data quality and let the registered AI provider make the run/block decision."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_PROVIDERS = REPO_ROOT / "data" / "research_framework" / "ai_providers.yaml"
EXPECTATIONS = REPO_ROOT / "data" / "research_framework" / "data_schema_expectations.yaml"
ENTRYPOINT = "scripts/validate_data_quality.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.ai_provider_adapter import RegisteredProviderAdapter  # noqa: E402
from framework.autonomous.executor_requirements import declared_requirements_for_spec  # noqa: E402
from framework.autonomous.status_codes import prompt_code_menu, status_label  # noqa: E402
from scripts.quant_access_guard import require_ticket  # noqa: E402


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{_rel(path)} root must be dict")
    return data


def _command_value(command: list[Any], flag: str) -> str | None:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[index + 1]
    return None


def _run_periods(spec: dict[str, Any]) -> dict[str, str | None]:
    command = ((spec.get("automation") or {}).get("command") or []) if isinstance(spec.get("automation"), dict) else []
    if not isinstance(command, list):
        command = []
    return {
        "train_start": _command_value(command, "--train-start"),
        "train_end": _command_value(command, "--train-end"),
        "test_start": _command_value(command, "--test-start"),
        "test_end": _command_value(command, "--test-end"),
    }


def _executor_script(spec: dict[str, Any]) -> str | None:
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    command = automation.get("command") or []
    if not isinstance(command, list):
        return None
    for part in command:
        text = str(part)
        if text.startswith("scripts/") and text.endswith(".py"):
            return text
    return None


def _script_column_literals(script_path: str | None) -> list[str]:
    if not script_path:
        return []
    path = REPO_ROOT / script_path
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    words = set(re.findall(r"""["']([A-Za-z_][A-Za-z0-9_]*)["']""", text))
    markers = (
        "code",
        "date",
        "price",
        "close",
        "open",
        "high",
        "low",
        "vol",
        "gap",
        "stock",
        "cb",
        "ts",
        "stk",
        "rank",
        "regime",
        "trade",
        "entry",
        "exit",
    )
    return sorted(word for word in words if any(marker in word.lower() for marker in markers))


def _script_input_column_hints(script_path: str | None) -> dict[str, list[str]]:
    if not script_path:
        return {}
    path = REPO_ROOT / script_path
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    hints: dict[str, set[str]] = {}
    for match in re.finditer(r"(\w+)\s*\[\s*\[([^\]]+)\]\s*\]", text):
        name = match.group(1)
        cols = re.findall(r"""["']([^"']+)["']""", match.group(2))
        if cols:
            hints.setdefault(name, set()).update(cols)
    for match in re.finditer(r"(\w+)\.columns[^\n]+?\(([^\)]*)\)", text):
        name = match.group(1)
        cols = re.findall(r"""["']([^"']+)["']""", match.group(2))
        if cols:
            hints.setdefault(name, set()).update(cols)
    return {name: sorted(values) for name, values in sorted(hints.items())}


def _expected_by_path() -> dict[str, dict[str, Any]]:
    if not EXPECTATIONS.exists():
        return {}
    data = _load_yaml(EXPECTATIONS)
    result: dict[str, dict[str, Any]] = {}
    for entry in data.get("warehouse_files") or []:
        if isinstance(entry, dict) and entry.get("path"):
            result[str(entry["path"])] = entry
    return result


def _candidate_data_paths(spec_path: Path, spec: dict[str, Any]) -> list[str]:
    requirements = declared_requirements_for_spec(spec_path)
    paths = [
        str(item.get("path"))
        for item in requirements.get("required_files") or []
        if isinstance(item, dict) and item.get("path")
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _path_from_raw(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        return REPO_ROOT / path
    try:
        path.resolve().relative_to(REPO_ROOT)
        return path
    except ValueError:
        parts = path.parts
        if "data" in parts:
            data_index = parts.index("data")
            candidate = REPO_ROOT.joinpath(*parts[data_index:])
            if candidate.exists():
                return candidate
        return path


def _requirements_by_path(requirements: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(requirements, dict):
        return result
    for item in requirements.get("required_files") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = _path_from_raw(str(item["path"]))
        scoped: dict[str, Any] = {}
        for key in ("required_columns", "nonnull_columns", "recommended_columns", "expected_min_rows"):
            if item.get(key) is not None:
                scoped[key] = item[key]
        if scoped:
            result[_rel(path)] = scoped
    return result


def _normalize_requirement_paths(requirements: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(requirements, dict):
        return requirements
    normalized = dict(requirements)
    files: list[Any] = []
    for item in requirements.get("required_files") or []:
        if not isinstance(item, dict) or not item.get("path"):
            files.append(item)
            continue
        copied = dict(item)
        copied["path"] = _rel(_path_from_raw(str(item["path"])))
        files.append(copied)
    normalized["required_files"] = files
    return normalized


def _date_bounds(series: pd.Series) -> dict[str, str | None]:
    if series.empty:
        return {"min": None, "max": None}
    values = series.dropna().astype(str)
    if values.empty:
        return {"min": None, "max": None}
    return {"min": str(values.min()), "max": str(values.max())}


def _summarize_parquet(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    import pyarrow.parquet as pq

    meta = pq.read_metadata(path)
    schema = pq.read_schema(path)
    columns = list(schema.names)
    required = [str(col) for col in expected.get("required_columns") or []]
    nonnull = [str(col) for col in expected.get("nonnull_columns") or []]
    recommended = [str(col) for col in expected.get("recommended_columns") or []]
    key_cols = [col for col in ("ts_code", "stk_code", "trade_date") if col in columns]
    price_cols = [col for col in ("open", "high", "low", "close") if col in columns]
    read_cols = sorted(set(required + nonnull + recommended + key_cols + price_cols) & set(columns))
    summary: dict[str, Any] = {
        "path": _rel(path),
        "kind": "parquet",
        "exists": True,
        "readable": True,
        "rows": int(meta.num_rows),
        "columns": columns,
        "missing_required_columns": sorted(set(required) - set(columns)),
        "missing_recommended_columns": sorted(set(recommended) - set(columns)),
        "expected_min_rows": expected.get("expected_min_rows"),
    }
    if not read_cols:
        return summary
    df = pd.read_parquet(path, columns=read_cols)
    nulls = {col: int(df[col].isna().sum()) for col in read_cols if col in nonnull}
    summary["required_nulls"] = nulls
    if "trade_date" in df.columns:
        summary["trade_date_range"] = _date_bounds(df["trade_date"])
    for date_col in ("ann_date", "call_date", "expire_date"):
        if date_col in df.columns:
            summary[f"{date_col}_range"] = _date_bounds(df[date_col])
    duplicate_keys = [col for col in ("ts_code", "stk_code", "trade_date") if col in df.columns]
    if len(duplicate_keys) >= 2 and "trade_date" in duplicate_keys:
        summary["duplicate_key_rows"] = int(df.duplicated(duplicate_keys).sum())
        summary["duplicate_key_columns"] = duplicate_keys
    if "close" in df.columns:
        summary["nonpositive_close_rows"] = int((pd.to_numeric(df["close"], errors="coerce") <= 0).sum())
    if {"open", "high", "low", "close"} <= set(df.columns):
        numeric = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        summary["nonpositive_ohlc_rows"] = int((numeric <= 0).any(axis=1).sum())
        summary["bad_ohlc_rows"] = int(
            (
                (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1))
                | (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1))
            ).sum()
        )
    return summary


def _summarize_csv(path: Path) -> dict[str, Any]:
    df = pd.read_csv(path)
    summary: dict[str, Any] = {
        "path": _rel(path),
        "kind": "csv",
        "exists": True,
        "readable": True,
        "rows": int(len(df)),
        "columns": list(df.columns),
    }
    if "trade_date" in df.columns:
        summary["trade_date_range"] = _date_bounds(df["trade_date"])
    return summary


def summarize_data_quality(spec_path: Path) -> dict[str, Any]:
    spec = _load_yaml(spec_path)
    executor_script = _executor_script(spec)
    requirements: dict[str, Any] | None = None
    requirements_error: str | None = None
    try:
        requirements = declared_requirements_for_spec(spec_path)
        candidate_paths = _candidate_data_paths(spec_path, spec)
    except Exception as exc:
        requirements_error = f"{type(exc).__name__}: {exc}"
        candidate_paths = []
    expected_map = _expected_by_path()
    requirement_expectations = _requirements_by_path(requirements)
    files: list[dict[str, Any]] = []
    for raw_path in candidate_paths:
        path = _path_from_raw(raw_path)
        entry: dict[str, Any]
        expected = requirement_expectations.get(_rel(path)) or expected_map.get(_rel(path), {})
        if not path.exists():
            entry = {"path": _rel(path), "exists": False, "readable": False}
        else:
            try:
                if path.suffix == ".parquet":
                    entry = _summarize_parquet(path, expected)
                elif path.suffix == ".csv":
                    entry = _summarize_csv(path)
                else:
                    entry = {"path": _rel(path), "kind": path.suffix.lstrip("."), "exists": True, "readable": True}
            except Exception as exc:
                entry = {
                    "path": _rel(path),
                    "exists": True,
                    "readable": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        files.append(entry)
    return {
        "schema_version": 1,
        "spec_path": _rel(spec_path),
        "run_id": spec.get("run_id"),
        "strategy_id": spec.get("strategy_id"),
        "executor_script": executor_script,
        "executor_requirements": _normalize_requirement_paths(requirements),
        "executor_requirements_error": requirements_error,
        "executor_generated_columns": (requirements or {}).get("generated_columns") if isinstance(requirements, dict) else None,
        "executor_column_literals": _script_column_literals(executor_script),
        "executor_input_column_hints": _script_input_column_hints(executor_script),
        "periods": _run_periods(spec),
        "data_files": files,
    }


def _judge_prompt(summary: dict[str, Any]) -> str:
    compact = compact_for_ai(summary)
    return (
        "你是量化实验的数据质量主检查员。最终能不能开跑，由你直接决定。\n"
        "不要评价策略好坏。不要提出新策略。不要写代码。\n"
        "你必须根据输入的数据摘要，判断数据是否足够支持本轮实验。\n\n"
        "重点检查：\n"
        "1. 必需数据文件是否存在且可读。\n"
        "2. 必需字段是否缺失。\n"
        "3. 训练和测试日期是否被行情数据覆盖。\n"
        "4. 关键字段空值、重复键、价格小于等于 0、开高低收关系错误是否会影响实验。\n"
        "5. executor_input_column_hints 里的输入字段，和输入数据实际字段是否对得上。\n"
        "6. required_columns 只代表字段必须存在；只有 required_column_quality 里出现的字段才要求非空。\n"
        "7. 如果只有推荐字段缺失，或者未要求非空的字段存在部分空值，可以放行并说明。\n"
        "8. 如果缺失数据能通过改路径、派生字段、复制池参数等方式修复，返回 repair_candidate 并写清 fix_plan。\n"
        "9. executor_generated_columns 里的字段是执行器运行时生成的字段，不要求原始输入文件直接存在。\n"
        "10. executor_input_column_hints 是源码参考，不是硬性输入要求；硬性要求以 executor_requirements.required_files.required_columns 为准。\n"
        "注意：required_column_quality.null_rows 是缺失行数，valid_rows 才是有效行数，不能反着理解。\n\n"
        "只返回 YAML，字段固定为；状态类字段只能输出数字编号，不能输出文字状态：\n"
        f"status_code: {prompt_code_menu('data_quality_decision')}\n"
        f"confidence_code: {prompt_code_menu('data_quality_confidence')}\n"
        "blocking_issues: 列表\n"
        "warnings: 列表\n"
        "fix_plan: 列表；只有 status_code=2 时填写，只能包含 rename_field、derive_alias、rewrite_spec_data_root、derive_warehouse_columns、copy_config_pool\n"
        "decision_reason: 一句话\n\n"
        "数据摘要：\n"
        f"{json.dumps(compact, ensure_ascii=False, indent=2)}\n"
    )


def compact_for_ai(summary: dict[str, Any]) -> dict[str, Any]:
    compact_files: list[dict[str, Any]] = []
    for item in summary.get("data_files") or []:
        if not isinstance(item, dict):
            continue
        compact: dict[str, Any] = {
            "path": item.get("path"),
            "exists": item.get("exists"),
            "readable": item.get("readable"),
            "rows": item.get("rows"),
            "columns": item.get("columns"),
            "expected_min_rows": item.get("expected_min_rows"),
        }
        for key in ("error", "trade_date_range", "ann_date_range", "call_date_range", "expire_date_range"):
            if item.get(key) is not None:
                compact[key] = item.get(key)
        for key in ("missing_required_columns", "missing_recommended_columns"):
            values = item.get(key)
            if values:
                compact[key] = values
        nulls = {key: value for key, value in (item.get("required_nulls") or {}).items() if value}
        if nulls:
            rows = int(item.get("rows") or 0)
            compact["required_column_quality"] = {
                key: {
                    "null_rows": int(value),
                    "valid_rows": max(rows - int(value), 0),
                    "total_rows": rows,
                }
                for key, value in nulls.items()
            }
        for key in ("duplicate_key_rows", "nonpositive_close_rows", "nonpositive_ohlc_rows", "bad_ohlc_rows"):
            value = item.get(key)
            if value:
                compact[key] = value
        compact_files.append(compact)
    return {
        "schema_version": summary.get("schema_version"),
        "spec_path": summary.get("spec_path"),
        "run_id": summary.get("run_id"),
        "strategy_id": summary.get("strategy_id"),
        "executor_script": summary.get("executor_script"),
        "executor_requirements": summary.get("executor_requirements"),
        "executor_requirements_error": summary.get("executor_requirements_error"),
        "executor_generated_columns": summary.get("executor_generated_columns"),
        "executor_input_column_hints": summary.get("executor_input_column_hints"),
        "periods": summary.get("periods"),
        "data_files": compact_files,
    }


def judge_summary(summary: dict[str, Any]) -> dict[str, Any]:
    adapter = RegisteredProviderAdapter(AI_PROVIDERS, repo_root=REPO_ROOT, entrypoint=ENTRYPOINT)
    response = adapter.call_active_provider(_judge_prompt(summary), schema={})
    content = _strip_markdown_fence(response.content)
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("AI data-quality response root must be YAML mapping")
    if "status" in data or "confidence" in data:
        raise ValueError("AI data-quality response must use numeric status_code/confidence_code, not text status fields")
    status = status_label("data_quality_decision", data.get("status_code"))
    confidence = status_label("data_quality_confidence", data.get("confidence_code"))
    for list_key in ("blocking_issues", "warnings", "fix_plan"):
        if data.get(list_key) is None:
            data[list_key] = []
        if not isinstance(data.get(list_key), list):
            raise ValueError(f"AI data-quality response {list_key} must be list")
    if not isinstance(data.get("decision_reason"), str) or not data["decision_reason"].strip():
        raise ValueError("AI data-quality response decision_reason must be non-empty string")
    data["schema_version"] = int(data.get("schema_version") or 1)
    data["status"] = status
    data["confidence"] = confidence
    data["decision_source"] = "ai_data_quality_judge"
    data["ai_provider"] = response.provider_id
    data["response_hash"] = response.response_hash
    return data


def _strip_markdown_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", help="spec.yaml path used when creating a summary")
    parser.add_argument("--summary-only", action="store_true", help="print JSON summary and do not call AI")
    parser.add_argument("--judge-summary-stdin", action="store_true", help="read JSON summary from stdin and ask AI")
    args = parser.parse_args()

    if args.judge_summary_stdin:
        summary = json.loads(sys.stdin.read())
    else:
        if not args.spec:
            parser.error("--spec is required unless --judge-summary-stdin is used")
        spec_path = Path(args.spec)
        if not spec_path.is_absolute():
            spec_path = REPO_ROOT / spec_path
        summary = summarize_data_quality(spec_path)

    if args.summary_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    require_ticket("data_quality_judge")
    try:
        decision = judge_summary(summary)
    except Exception as exc:
        print(f"FAIL: data quality AI judge unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(yaml.safe_dump(decision, allow_unicode=True, sort_keys=False))
    return 0 if str(decision.get("status")).lower() == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
