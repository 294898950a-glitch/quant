from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.framework_change_recorder import record_framework_change
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from framework_change_recorder import record_framework_change  # type: ignore


class NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


class ArtifactStore:
    def __init__(self, root: Path | str = Path(".")) -> None:
        self.root = Path(root)

    def resolve(self, path: Path | str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate

    def read_yaml(self, path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self.resolve(path)
        if not resolved.exists():
            return dict(default or {})
        loaded = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{resolved} root must be a mapping")
        return loaded

    def write_yaml(self, path: Path | str, payload: dict[str, Any], no_aliases: bool = False) -> Path:
        resolved = self.resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if no_aliases:
            text = yaml.dump(payload, Dumper=NoAliasSafeDumper, allow_unicode=True, sort_keys=False)
        else:
            text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        resolved.write_text(text, encoding="utf-8")
        return resolved

    def read_json(self, path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self.resolve(path)
        if not resolved.exists():
            return dict(default or {})
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{resolved} root must be an object")
        return loaded

    def write_json(self, path: Path | str, payload: dict[str, Any], indent: int | None = 2) -> Path:
        resolved = self.resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
        return resolved


class ChangeTrackedStore(ArtifactStore):
    def write_yaml_with_record(
        self,
        path: Path | str,
        payload: dict[str, Any],
        *,
        change_type: str,
        summary: str,
        actor: str,
        reason: str,
        impact: str | None = None,
        evidence: dict[str, Any] | None = None,
        no_aliases: bool = False,
    ) -> tuple[Path, str]:
        resolved = self.write_yaml(path, payload, no_aliases=no_aliases)
        event_hash = record_framework_change(
            change_type=change_type,
            summary=summary,
            changed_paths=[str(path)],
            actor=actor,
            reason=reason,
            impact=impact,
            evidence=evidence,
        )
        return resolved, event_hash
