"""Executor-owned input declarations for autonomous runs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be dict")
    return data


def executor_script_from_spec(spec: dict[str, Any]) -> str | None:
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    command = automation.get("command") or []
    if not isinstance(command, list):
        return None
    for part in command:
        text = str(part)
        if text.startswith("scripts/") and text.endswith(".py"):
            return text
    return None


def _load_executor(script_path: str) -> Any:
    path = REPO_ROOT / script_path
    if not path.exists():
        raise FileNotFoundError(f"executor script missing: {script_path}")
    module_name = "quant_executor_requirements_" + script_path.replace("/", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load executor script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def declared_requirements_for_spec(spec_path: Path) -> dict[str, Any]:
    spec = _load_yaml(spec_path)
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    command = automation.get("command") or []
    if not isinstance(command, list):
        raise ValueError("spec automation.command must be list")
    script_path = executor_script_from_spec(spec)
    if not script_path:
        raise ValueError("spec automation.command must name a scripts/*.py executor")
    module = _load_executor(script_path)
    declare = getattr(module, "declare_data_requirements", None)
    if declare is None or not callable(declare):
        raise ValueError(f"executor {script_path} does not declare data requirements")
    requirements = declare(command, spec)
    if not isinstance(requirements, dict):
        raise ValueError(f"executor {script_path} data requirements must be dict")
    required_files = requirements.get("required_files")
    if not isinstance(required_files, list) or not required_files:
        raise ValueError(f"executor {script_path} data requirements must include required_files")
    normalized: list[dict[str, Any]] = []
    for item in required_files:
        if isinstance(item, str):
            normalized.append({"path": item})
            continue
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError(f"executor {script_path} has invalid required_files item: {item!r}")
        normalized.append(dict(item))
    result: dict[str, Any] = {
        "schema_version": 1,
        "executor_script": script_path,
        "requirements_source": f"{script_path}::declare_data_requirements",
        "required_files": normalized,
    }
    for key in ("generated_columns", "derived_columns"):
        if requirements.get(key) is not None:
            result[key] = requirements[key]
    return result
