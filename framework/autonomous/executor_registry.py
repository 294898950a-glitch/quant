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


def capability_catalog(registry: dict[str, Any]) -> dict[str, str]:
    raw = registry.get("capabilities") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for cid, item in raw.items():
        if isinstance(item, dict) and item.get("mechanic"):
            out[str(cid)] = str(item["mechanic"])
    return out


def capability_ids_to_mechanics(capability_ids: set[str], registry: dict[str, Any]) -> set[str]:
    catalog = capability_catalog(registry)
    return {catalog[cid] for cid in capability_ids if cid in catalog}


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
        for list_field in ("can_test_capability_ids", "cannot_test_capability_ids"):
            if list_field in executor and not isinstance(executor[list_field], list):
                errors.append(f"executor {eid} field {list_field} must be a list")
        if "budget_estimate" in executor and not isinstance(executor["budget_estimate"], dict):
            errors.append(f"executor {eid} field budget_estimate must be a mapping")
        if "vm_local_limits" in executor and not isinstance(executor["vm_local_limits"], dict):
            errors.append(f"executor {eid} field vm_local_limits must be a mapping")
        catalog = capability_catalog(registry)
        if catalog:
            for list_field in ("can_test_capability_ids", "cannot_test_capability_ids"):
                ids = executor.get(list_field)
                if ids is None:
                    errors.append(f"executor {eid} missing required field {list_field}")
                    continue
                for cid in ids:
                    if str(cid) not in catalog:
                        errors.append(f"executor {eid} unknown capability id {cid}")
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


def match_executor(
    proposal_mechanics: set[str],
    proposal_data: set[str],
    registry: dict[str, Any],
    proposal_capability_ids: set[str] | None = None,
    required_executor: str | None = None,
) -> ExecutorMatch | None:
    """Return the first strict full match, never a nearest fallback."""

    mechanics = set(proposal_mechanics)
    capability_ids = set(proposal_capability_ids or set())
    data_paths = set(proposal_data)
    required = str(required_executor or "").strip()
    for executor in _executors(registry):
        if _is_obsolete(executor):
            continue
        if required and required not in {str(executor.get("id") or ""), str(executor.get("family") or "")}:
            continue
        can_test = set(executor.get("can_test", []))
        cannot_test = set(executor.get("cannot_test", []))
        can_capabilities = set(str(item) for item in executor.get("can_test_capability_ids", []))
        cannot_capabilities = set(str(item) for item in executor.get("cannot_test_capability_ids", []))
        required_data = _required_data_paths(executor)
        if capability_ids:
            if not capability_ids.issubset(can_capabilities):
                continue
            if capability_ids & cannot_capabilities:
                continue
        else:
            if not mechanics.issubset(can_test):
                continue
            if mechanics & cannot_test:
                continue
        if not required_data.issubset(data_paths):
            continue
        return ExecutorMatch(executor)
    return None
