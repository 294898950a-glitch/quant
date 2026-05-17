from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from framework.autonomous.artifacts import ArtifactStore
    from framework.autonomous.framework_change_recorder import record_framework_change
    from framework.autonomous.paths import ResearchPaths
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from artifacts import ArtifactStore  # type: ignore
    from framework_change_recorder import record_framework_change  # type: ignore
    from paths import ResearchPaths  # type: ignore


REQUIRED_TOOL_DESCRIPTOR_FIELDS = {
    "id",
    "status",
    "owner",
    "module_path",
    "callable",
    "description",
    "allowed_roles",
    "max_budget_yuan",
    "registration_reason",
    "why_existing_tools_insufficient",
    "reviewed_existing_tool_ids",
    "existing_tools_manifest_sha256",
    "created_at",
}
DEFAULT_TOOL_REGISTRY_PATH = ResearchPaths.from_repo_root(Path(".")).evidence_tool_registry


class EvidenceToolRegistry:
    def __init__(
        self,
        path: Path | str | None = None,
        store: ArtifactStore | None = None,
        paths: ResearchPaths | None = None,
    ) -> None:
        self.paths = paths or ResearchPaths.from_repo_root(Path("."))
        self.path = Path(path) if path is not None else DEFAULT_TOOL_REGISTRY_PATH
        self.store = store or ArtifactStore()

    def load(self) -> dict[str, Any]:
        registry = self.store.read_yaml(self.path, default={"schema_version": 1, "tools": {}})
        if not isinstance(registry.get("tools"), dict):
            registry["tools"] = {}
        return registry

    def implemented_ids(self, registry: dict[str, Any] | None = None) -> set[str]:
        loaded = registry or self.load()
        return {
            str(tool_id)
            for tool_id, tool in (loaded.get("tools") or {}).items()
            if isinstance(tool, dict) and tool.get("status") == "implemented"
        }

    def manifest(self, registry: dict[str, Any] | None = None) -> dict[str, Any]:
        loaded = registry or self.load()
        tools = loaded.get("tools") or {}
        manifest_tools = {}
        for tool_id, tool in tools.items():
            if not isinstance(tool, dict):
                continue
            manifest_tools[str(tool_id)] = {
                "status": tool.get("status"),
                "module_path": tool.get("module_path"),
                "callable": tool.get("callable"),
                "request_type": tool.get("request_type", tool_id),
                "description": tool.get("description"),
                "allowed_roles": tool.get("allowed_roles", []),
            }
        return {
            "registry_path": str(self.path),
            "rule": "Before proposing or registering a new evidence tool, review these existing tool paths and explain why none is sufficient.",
            "tools": manifest_tools,
            "manifest_sha256": self.manifest_sha256(loaded),
        }

    def manifest_sha256(self, registry: dict[str, Any] | None = None) -> str:
        loaded = registry or self.load()
        reduced = {
            str(tool_id): {
                "module_path": tool.get("module_path"),
                "callable": tool.get("callable"),
                "description": tool.get("description"),
                "allowed_roles": tool.get("allowed_roles", []),
                "status": tool.get("status"),
            }
            for tool_id, tool in (loaded.get("tools") or {}).items()
            if isinstance(tool, dict)
        }
        payload = json.dumps(reduced, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ensure_implemented(self, request_type: str, registry: dict[str, Any] | None = None) -> None:
        if request_type not in self.implemented_ids(registry):
            raise ValueError(
                f"evidence tool is not registered or not implemented: {request_type}. "
                f"Review {self.path} before adding a new tool."
            )

    def validate_descriptor(self, descriptor: dict[str, Any], registry: dict[str, Any] | None = None) -> list[str]:
        loaded = registry or self.load()
        errors: list[str] = []
        missing = sorted(REQUIRED_TOOL_DESCRIPTOR_FIELDS - set(descriptor))
        for field in missing:
            errors.append(f"missing required field: {field}")

        tool_id = str(descriptor.get("id") or "")
        if tool_id in (loaded.get("tools") or {}):
            errors.append(f"tool id already exists: {tool_id}")

        status = descriptor.get("status")
        allowed_statuses = set((loaded.get("registration_rules") or {}).get("allowed_statuses") or [])
        if allowed_statuses and status not in allowed_statuses:
            errors.append(f"status must be one of {sorted(allowed_statuses)}, got {status!r}")

        allowed_roles = set((loaded.get("registration_rules") or {}).get("allowed_roles") or [])
        roles = descriptor.get("allowed_roles")
        if not isinstance(roles, list) or not roles:
            errors.append("allowed_roles must be a non-empty list")
        elif allowed_roles:
            unknown_roles = sorted(set(str(role) for role in roles) - allowed_roles)
            if unknown_roles:
                errors.append(f"allowed_roles contains unknown role(s): {unknown_roles}")

        existing_tools = loaded.get("tools") or {}
        reviewed = descriptor.get("reviewed_existing_tool_ids")
        if not isinstance(reviewed, list) or not reviewed:
            errors.append("reviewed_existing_tool_ids must be a non-empty list")
        else:
            unknown_tools = sorted(set(str(item) for item in reviewed) - set(existing_tools))
            if unknown_tools:
                errors.append(f"reviewed_existing_tool_ids contains unknown tool(s): {unknown_tools}")

        expected_hash = self.manifest_sha256(loaded)
        if descriptor.get("existing_tools_manifest_sha256") != expected_hash:
            errors.append(
                "existing_tools_manifest_sha256 does not match current registry; "
                "inject the current existing-tool manifest before registering"
            )

        for field in ("registration_reason", "why_existing_tools_insufficient", "description"):
            value = descriptor.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{field} must be a non-empty string")
        return errors

    def normalise_tool(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": descriptor["status"],
            "owner": descriptor["owner"],
            "module_path": descriptor["module_path"],
            "callable": descriptor["callable"],
            "request_type": descriptor.get("request_type", descriptor["id"]),
            "description": descriptor["description"],
            "allowed_roles": descriptor["allowed_roles"],
            "max_budget_yuan": descriptor["max_budget_yuan"],
            "registration_reason": descriptor["registration_reason"],
            "why_existing_tools_insufficient": descriptor["why_existing_tools_insufficient"],
            "reviewed_existing_tool_ids": descriptor["reviewed_existing_tool_ids"],
            "existing_tools_manifest_sha256": descriptor["existing_tools_manifest_sha256"],
            "created_at": descriptor["created_at"],
        }

    def register(self, descriptor: dict[str, Any]) -> str:
        registry = self.load()
        errors = self.validate_descriptor(descriptor, registry)
        if errors:
            raise ValueError("\n".join(errors))
        registry.setdefault("tools", {})[str(descriptor["id"])] = self.normalise_tool(descriptor)
        registry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.write_yaml(self.path, registry)
        return record_framework_change(
            change_type="evidence_tool_registered",
            summary=f"Registered evidence tool {descriptor['id']}",
            changed_paths=[str(self.path), str(descriptor["module_path"])],
            actor=str(descriptor.get("owner") or "unknown"),
            reason=str(descriptor["registration_reason"]),
            impact="EvidenceToolkit can now call this tool after registry validation.",
            evidence={
                "tool_id": descriptor["id"],
                "why_existing_tools_insufficient": descriptor["why_existing_tools_insufficient"],
                "reviewed_existing_tool_ids": descriptor["reviewed_existing_tool_ids"],
                "existing_tools_manifest_sha256": descriptor["existing_tools_manifest_sha256"],
            },
        )
