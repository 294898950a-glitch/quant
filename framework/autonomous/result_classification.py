"""Machine-owned result classification map."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLASSIFICATION_PATH = REPO_ROOT / "data" / "research_framework" / "result_classification_map.yaml"


class ResultClassificationError(RuntimeError):
    pass


def load_result_classification_map(path: Path | None = None) -> dict[str, Any]:
    source = path or DEFAULT_CLASSIFICATION_PATH
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ResultClassificationError(f"{source} root must be mapping")
    decisions = data.get("result_decisions")
    if not isinstance(decisions, dict):
        raise ResultClassificationError(f"{source} result_decisions must be mapping")
    return data


def decision_meta(decision: str, path: Path | None = None) -> dict[str, Any]:
    decision_id = str(decision or "").strip()
    data = load_result_classification_map(path)
    decisions = data["result_decisions"]
    meta = decisions.get(decision_id)
    if not isinstance(meta, dict):
        raise ResultClassificationError(f"unknown result decision: {decision_id!r}")
    if not isinstance(meta.get("status"), str) or not meta["status"]:
        raise ResultClassificationError(f"result decision {decision_id!r} missing status")
    return meta


def status_for_decision(decision: str, path: Path | None = None) -> str:
    return str(decision_meta(decision, path).get("status"))


def evidence_usable(decision: str, path: Path | None = None) -> bool:
    return bool(decision_meta(decision, path).get("evidence_usable"))


def closes_direction(decision: str, path: Path | None = None) -> bool:
    return bool(decision_meta(decision, path).get("closes_direction"))


def can_seed_ideation(decision: str, path: Path | None = None) -> bool:
    return bool(decision_meta(decision, path).get("can_seed_ideation"))
