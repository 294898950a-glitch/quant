#!/usr/bin/env python3
"""Summarize run data quality and ask the registered AI provider for a run/block decision."""

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
WAREHOUSE_REL_PATHS = (
    "data/cb_warehouse/cb_basic.parquet",
    "data/cb_warehouse/cb_daily.parquet",
    "data/cb_warehouse/cb_call.parquet",
    "data/cb_warehouse/stk_daily_qfq.parquet",
)
DERIVABLE_RECOMMENDED_COLUMNS = {
    "data/cb_warehouse/cb_daily.parquet": {"pct_chg", "cb_over_rate"},
    "data/cb_warehouse/cb_call.parquet": {"call_type"},
}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.ai_provider_adapter import RegisteredProviderAdapter  # noqa: E402
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


def _candidate_data_paths(spec: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    command = ((spec.get("automation") or {}).get("command") or []) if isinstance(spec.get("automation"), dict) else []
    command_paths: list[str] = []
    data_root = _command_value(command, "--data-root") if isinstance(command, list) else None
    base_ranks = _command_value(command, "--base-ranks-path") if isinstance(command, list) else None
    if base_ranks:
        command_paths.append(base_ranks)
    if data_root:
        for rel_path in WAREHOUSE_REL_PATHS:
            command_paths.append(str(Path(data_root) / rel_path))
    paths.extend(command_paths)
    if not command_paths:
        for entry in spec.get("new_data_sources") or []:
            if isinstance(entry, dict) and entry.get("path"):
                paths.append(str(entry["path"]))
        proposal = spec.get("proposal") if isinstance(spec.get("proposal"), dict) else {}
        for path in proposal.get("required_data") or []:
            paths.append(str(path))
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    if not command_paths:
        for path in automation.get("sync_paths") or []:
            path_str = str(path)
            if path_str.startswith("data/") and (
                path_str.endswith(".parquet") or path_str.endswith(".csv") or path_str.endswith(".yaml")
            ):
                paths.append(path_str)
    if not command_paths:
        for path in _expected_by_path():
            paths.append(path)
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _warehouse_rel_from_path(raw_path: str) -> str | None:
    text = raw_path.replace("\\", "/")
    for rel_path in WAREHOUSE_REL_PATHS:
        marker = "/" + rel_path
        if text == rel_path or text.endswith(marker) or marker in text:
            return rel_path
    return None


def _current_warehouse_has(rel_path: str) -> bool:
    return (REPO_ROOT / rel_path).exists()


def deterministic_decision(summary: dict[str, Any]) -> dict[str, Any]:
    """Return the non-AI risk gate decision for run data.

    This gate handles facts that should not depend on model judgment:
    missing files, unreadable inputs, required schema breakage, and obvious
    corrupt price rows. The AI judge may add concerns only after this gate
    passes or after a repairable path issue is fixed.
    """
    blocking: list[str] = []
    warnings: list[str] = []
    fix_plan: list[dict[str, Any]] = []
    seen_repairs: set[tuple[str, str]] = set()

    for item in summary.get("data_files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if item.get("exists") is False:
            warehouse_rel = _warehouse_rel_from_path(path)
            if warehouse_rel and _current_warehouse_has(warehouse_rel):
                repair_key = ("rewrite_spec_data_root", warehouse_rel)
                if repair_key not in seen_repairs:
                    fix_plan.append(
                        {
                            "action": "rewrite_spec_data_root",
                            "missing_path": path,
                            "replacement_path": warehouse_rel,
                            "new_data_root": ".",
                        }
                    )
                    seen_repairs.add(repair_key)
                continue
            blocking.append(f"required data file missing: {path}")
            continue
        if item.get("readable") is False:
            blocking.append(f"required data file unreadable: {path} {item.get('error') or ''}".strip())
            continue
        missing_required = item.get("missing_required_columns") or []
        if missing_required:
            blocking.append(f"{path} missing required columns: {missing_required}")
        expected_min_rows = item.get("expected_min_rows")
        if expected_min_rows is not None:
            try:
                if int(item.get("rows") or 0) < int(expected_min_rows):
                    blocking.append(f"{path} rows below expected minimum: {item.get('rows')} < {expected_min_rows}")
            except (TypeError, ValueError):
                blocking.append(f"{path} has invalid expected_min_rows: {expected_min_rows}")
        for key in ("nonpositive_close_rows", "nonpositive_ohlc_rows", "bad_ohlc_rows"):
            if int(item.get(key) or 0) > 0:
                blocking.append(f"{path} has {key}: {item.get(key)}")
        duplicate_rows = int(item.get("duplicate_key_rows") or 0)
        if duplicate_rows:
            warnings.append(f"{path} has duplicate key rows: {duplicate_rows}")
        missing_recommended = item.get("missing_recommended_columns") or []
        if missing_recommended:
            warehouse_rel = _warehouse_rel_from_path(path)
            derivable = DERIVABLE_RECOMMENDED_COLUMNS.get(warehouse_rel or "", set())
            repairable = sorted(set(missing_recommended) & derivable)
            not_repairable = sorted(set(missing_recommended) - derivable)
            if repairable:
                repair_key = ("derive_warehouse_columns", warehouse_rel or path)
                if repair_key not in seen_repairs:
                    fix_plan.append(
                        {
                            "action": "derive_warehouse_columns",
                            "path": warehouse_rel or path,
                            "columns": repairable,
                            "new_data_root": "prepared_data/data_root",
                        }
                    )
                    seen_repairs.add(repair_key)
            if not_repairable:
                warnings.append(f"{path} missing recommended columns: {not_repairable}")

    if blocking:
        status = "fail"
        reason = "deterministic data gate found blocking data defects"
    elif fix_plan:
        status = "repair_candidate"
        reason = "deterministic data gate found data issues with registered deterministic repairs"
    else:
        status = "pass"
        reason = "deterministic data gate passed"

    return {
        "schema_version": 1,
        "status": status,
        "confidence": "high",
        "decision_source": "deterministic_data_gate",
        "blocking_issues": blocking,
        "warnings": warnings,
        "fix_plan": fix_plan,
        "decision_reason": reason,
    }


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
    recommended = [str(col) for col in expected.get("recommended_columns") or []]
    key_cols = [col for col in ("ts_code", "stk_code", "trade_date") if col in columns]
    price_cols = [col for col in ("open", "high", "low", "close") if col in columns]
    read_cols = sorted(set(required + recommended + key_cols + price_cols + ["ann_date", "call_date", "expire_date"]) & set(columns))
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
    nulls = {col: int(df[col].isna().sum()) for col in read_cols if col in required}
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
    expected_map = _expected_by_path()
    files: list[dict[str, Any]] = []
    for raw_path in _candidate_data_paths(spec):
        path = Path(raw_path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        entry: dict[str, Any]
        expected = expected_map.get(_rel(path), {})
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
        "executor_column_literals": _script_column_literals(executor_script),
        "executor_input_column_hints": _script_input_column_hints(executor_script),
        "periods": _run_periods(spec),
        "data_files": files,
    }


def _judge_prompt(summary: dict[str, Any]) -> str:
    compact = compact_for_ai(summary)
    return (
        "你是量化实验的数据质量检查员。只判断这次实验能不能开跑，或是否值得交给数据修复器尝试处理。\n"
        "不要评价策略好坏。不要提出新策略。不要写代码。\n"
        "你必须根据输入的数据摘要，判断数据是否足够支持本轮实验。\n\n"
        "重点检查：\n"
        "1. 必需数据文件是否存在且可读。\n"
        "2. 必需字段是否缺失。\n"
        "3. 训练和测试日期是否被行情数据覆盖。\n"
        "4. 关键字段空值、重复键、价格小于等于 0、开高低收关系错误是否会影响实验。\n"
        "5. executor_input_column_hints 里的输入字段，和输入数据实际字段是否对得上。\n"
        "6. 如果只有推荐字段缺失，但当前实验未必需要，可以放行并说明。\n"
        "注意：required_column_quality.null_rows 是缺失行数，valid_rows 才是有效行数，不能反着理解。\n\n"
        "只返回 YAML，字段固定为：\n"
        "status: pass 或 repair_candidate 或 fail\n"
        "confidence: high 或 medium 或 low\n"
        "blocking_issues: 列表\n"
        "warnings: 列表\n"
        "fix_plan: 列表；只有 status=repair_candidate 时填写，只能包含 rename_field、derive_alias、rewrite_spec_data_root、derive_warehouse_columns\n"
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
        "executor_input_column_hints": summary.get("executor_input_column_hints"),
        "periods": summary.get("periods"),
        "data_files": compact_files,
    }


def judge_summary(summary: dict[str, Any]) -> dict[str, Any]:
    gate_decision = deterministic_decision(summary)
    if str(gate_decision.get("status") or "").lower() != "pass":
        return gate_decision
    adapter = RegisteredProviderAdapter(AI_PROVIDERS, repo_root=REPO_ROOT, entrypoint=ENTRYPOINT)
    response = adapter.call_active_provider(_judge_prompt(summary), schema={})
    content = _strip_markdown_fence(response.content)
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("AI data-quality response root must be YAML mapping")
    status = str(data.get("status") or "").strip().lower()
    if status == "fixable":
        status = "repair_candidate"
        data["status"] = status
    if status not in {"pass", "repair_candidate", "fail"}:
        raise ValueError("AI data-quality response status must be pass, repair_candidate, or fail")
    data["ai_provider"] = response.provider_id
    data["response_hash"] = response.response_hash
    data["deterministic_gate"] = gate_decision
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
    parser.add_argument("--hard-decision-only", action="store_true", help="print deterministic data gate decision and do not call AI")
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

    if args.hard_decision_only:
        decision = deterministic_decision(summary)
        print(yaml.safe_dump(decision, allow_unicode=True, sort_keys=False))
        return 0 if str(decision.get("status")).lower() == "pass" else 1

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
