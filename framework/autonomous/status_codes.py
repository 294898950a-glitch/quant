"""Numeric code maps for LLM-facing status-like values."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_CODE_MAPS = REPO_ROOT / "data" / "research_framework" / "status_code_maps.yaml"


class StatusCodeError(ValueError):
    pass


def load_status_code_maps(path: Path = STATUS_CODE_MAPS) -> dict[str, dict[int, str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise StatusCodeError(f"{path} root must be mapping")
    raw_maps = data.get("maps")
    if not isinstance(raw_maps, dict):
        raise StatusCodeError(f"{path} maps must be mapping")
    maps: dict[str, dict[int, str]] = {}
    for family, raw_values in raw_maps.items():
        if not isinstance(raw_values, dict):
            raise StatusCodeError(f"status map {family} must be mapping")
        values: dict[int, str] = {}
        for raw_code, raw_label in raw_values.items():
            try:
                code = int(raw_code)
            except (TypeError, ValueError) as exc:
                raise StatusCodeError(f"status map {family} code {raw_code!r} is not int") from exc
            label = str(raw_label).strip()
            if not label:
                raise StatusCodeError(f"status map {family} code {code} has empty label")
            values[code] = label
        maps[str(family)] = values
    return maps


def status_label(family: str, code: Any, *, maps: dict[str, dict[int, str]] | None = None) -> str:
    loaded = maps or load_status_code_maps()
    family_map = loaded.get(family)
    if not family_map:
        raise StatusCodeError(f"unknown status code family {family!r}")
    try:
        numeric = int(code)
    except (TypeError, ValueError) as exc:
        raise StatusCodeError(f"{family} status code must be int, got {code!r}") from exc
    if numeric not in family_map:
        raise StatusCodeError(f"{family} status code must be one of {sorted(family_map)}, got {numeric}")
    return family_map[numeric]


def prompt_code_menu(family: str, *, maps: dict[str, dict[int, str]] | None = None) -> str:
    loaded = maps or load_status_code_maps()
    family_map = loaded.get(family)
    if not family_map:
        raise StatusCodeError(f"unknown status code family {family!r}")
    return ", ".join(f"{code}={label}" for code, label in sorted(family_map.items()))
