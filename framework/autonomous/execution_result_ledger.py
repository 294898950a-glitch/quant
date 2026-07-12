"""Controller-owned idempotency ledger for remote execution results.

Compute nodes return envelopes; only the controller calls :func:`claim_result`.
The append-only JSONL ledger makes a replay visible without giving adapters
write access to queue state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ResultRejected(ValueError):
    """A result envelope cannot advance controller-owned state."""


def result_sha256(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def claim_result(
    ledger_path: Path,
    *,
    queue_item: dict[str, Any],
    envelope: dict[str, Any],
    actor: str,
) -> tuple[bool, str]:
    """Atomically claim a result before the caller changes queue state.

    Returns ``(True, sha256)`` for a first valid result. Replays and stale
    results return ``(False, reason)`` and are appended as audit noops.
    """
    run_id = str(envelope.get("run_id") or "")
    nonce = str(envelope.get("request_nonce") or "")
    expected = str(envelope.get("expected_prior_status") or "")
    current = str(queue_item.get("status") or "")
    queue_run_id = str(queue_item.get("id") or queue_item.get("run_id") or "")
    queue_nonce = str(queue_item.get("request_nonce") or "")
    digest = result_sha256(envelope)
    if not run_id or not nonce or not expected:
        raise ResultRejected("result envelope requires run_id, request_nonce, expected_prior_status")
    if run_id != queue_run_id:
        raise ResultRejected(f"run_id mismatch: {run_id} != {queue_run_id}")
    if nonce != queue_nonce:
        raise ResultRejected("request_nonce mismatch")
    if current != expected:
        raise ResultRejected(f"stale result: queue status={current}, expected={expected}")

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    prior = _rows(ledger_path)
    if any(row.get("result_sha256") == digest for row in prior):
        return False, "duplicate_result"
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "request_nonce": nonce,
        "result_sha256": digest,
        "expected_prior_status": expected,
        "claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claimed_by": actor,
        "outcome": str(envelope.get("outcome") or "unknown"),
    }
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return True, digest
