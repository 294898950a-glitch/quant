"""Executor registry loading and strict matching."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml


REQUIRED_EXECUTOR_FIELDS = {
    "id",
    "version",
    "script_path",
    "can_test",
    "cannot_test",
    "required_data",
    "required_config_fields",
    "artifacts_produced",
    "command_template",
    "budget_estimate",
    "vm_local_limits",
}


class ExecutorMatch(dict):
    """Dict-compatible match object with attribute access."""

    def __init__(self, executor: dict[str, Any]):
        super().__init__(executor)
        self.executor_id = executor.get("id")
        self.version = executor.get("version")
        self.script_path = executor.get("script_path")
        self.command_template = executor.get("command_template")
        self.budget_estimate = executor.get("budget_estimate")
        self.required_data = executor.get("required_data")


def _executors(registry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = registry.get("executors", [])
    if isinstance(raw, dict):
        return [dict(v) for v in raw.values()]
    if isinstance(raw, list):
        return [dict(v) for v in raw]
    return []


def load_registry(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("executor registry must be a mapping")
    return data


def validate_registry_schema(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for idx, executor in enumerate(_executors(registry)):
        eid = executor.get("id", f"<index {idx}>")
        missing = sorted(REQUIRED_EXECUTOR_FIELDS - set(executor))
        for field in missing:
            errors.append(f"executor {eid} missing required field {field}")
        if eid in seen:
            errors.append(f"duplicate executor id {eid}")
        seen.add(eid)
        for list_field in ("can_test", "cannot_test", "required_data", "required_config_fields", "artifacts_produced", "command_template"):
            if list_field in executor and not isinstance(executor[list_field], list):
                errors.append(f"executor {eid} field {list_field} must be a list")
        if "budget_estimate" in executor and not isinstance(executor["budget_estimate"], dict):
            errors.append(f"executor {eid} field budget_estimate must be a mapping")
        if "vm_local_limits" in executor and not isinstance(executor["vm_local_limits"], dict):
            errors.append(f"executor {eid} field vm_local_limits must be a mapping")
    return errors


def _required_data_paths(executor: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for item in executor.get("required_data", []):
        if isinstance(item, dict) and item.get("path"):
            paths.add(str(item["path"]))
        elif isinstance(item, str):
            paths.add(item)
    return paths


def _is_obsolete(executor: dict[str, Any]) -> bool:
    value = executor.get("obsolescence_date")
    if not value:
        return False
    try:
        return date.fromisoformat(str(value)) < date.today()
    except ValueError:
        return False


def match_executor(proposal_mechanics: set[str], proposal_data: set[str], registry: dict[str, Any]) -> ExecutorMatch | None:
    """Return the first strict full match, never a nearest fallback."""

    mechanics = set(proposal_mechanics)
    data_paths = set(proposal_data)
    for executor in _executors(registry):
        if _is_obsolete(executor):
            continue
        can_test = set(executor.get("can_test", []))
        cannot_test = set(executor.get("cannot_test", []))
        required_data = _required_data_paths(executor)
        if not mechanics.issubset(can_test):
            continue
        if mechanics & cannot_test:
            continue
        if not required_data.issubset(data_paths):
            continue
        return ExecutorMatch(executor)
    return None
