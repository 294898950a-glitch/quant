"""Versioned controller-to-compute execution request envelopes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml


FORBIDDEN_PARTS = {".env", "secrets", ".ssh", ".kimi", ".hermes"}


def write_request(spec_path: Path, *, queue_item: dict[str, Any], executor_id: str) -> Path:
    """Write the minimal, credential-free request consumed by an adapter."""
    run_id = str(queue_item.get("id") or queue_item.get("run_id") or spec_path.parent.name)
    nonce = str(queue_item.get("request_nonce") or "")
    if not nonce:
        raise ValueError("queue item missing request_nonce")
    spec_rel = "spec.yaml"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "request_nonce": nonce,
        "expected_prior_status": "running",
        "executor_id": executor_id,
        "spec_path": spec_rel,
        "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        "compute_estimate": (yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}).get("compute_estimate", {}),
    }
    if any(part in FORBIDDEN_PARTS for part in Path(spec_rel).parts):
        raise ValueError("forbidden path in execution request")
    path = spec_path.parent / "execution_request.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
