#!/usr/bin/env python3
"""Short-lived access tickets for quant automation writes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from time import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
TICKET_DIR = REPO_ROOT / "logs" / "quant_tickets"
AUDIT_PATH = REPO_ROOT / "data" / "research_framework" / "quant_access_audit.jsonl"
DEFAULT_TTL_SECONDS = 15 * 60
INTERNAL_CRON_ISSUER = "quant_internal_cron"
ALLOWED_ISSUERS = {INTERNAL_CRON_ISSUER}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _audit(event: str, action: str, payload: dict[str, Any] | None = None) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "event": event,
        "action": action,
        "pid": os.getpid(),
        "ts": _now_iso(),
        "payload": payload or {},
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _ticket_path_from_env() -> Path:
    raw = os.environ.get("QUANT_AUTOMATION_TICKET_PATH", "").strip()
    if not raw:
        raise PermissionError("missing QUANT_AUTOMATION_TICKET_PATH")
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    resolved = path.resolve()
    ticket_root = TICKET_DIR.resolve()
    try:
        resolved.relative_to(ticket_root)
    except ValueError as exc:
        raise PermissionError("quant ticket path is outside logs/quant_tickets") from exc
    return resolved


def issue_ticket(
    action: str,
    *,
    allowed_actions: list[str] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, str]:
    """Create a one-use ticket and return path plus plaintext token."""
    issuer = os.environ.get("QUANT_AUTOMATION_ISSUER", "").strip()
    if issuer not in ALLOWED_ISSUERS:
        _audit("issuer_denied", action, {"issuer": issuer or "(missing)"})
        allowed = ", ".join(sorted(ALLOWED_ISSUERS))
        raise PermissionError(f"quant automation issuer identity required: {allowed}")
    TICKET_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    ticket_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
    actions = allowed_actions or [action]
    ticket_path = TICKET_DIR / f"{ticket_id}.json"
    payload = {
        "schema_version": 1,
        "ticket_id": ticket_id,
        "issuer": issuer,
        "project": "quant",
        "action": action,
        "allowed_actions": actions,
        "issued_at_epoch": int(time()),
        "expires_at_epoch": int(time()) + int(ttl_seconds),
        "token_hash": _hash_token(token),
        "used": False,
    }
    ticket_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _audit("issued", action, {"ticket_id": ticket_id, "allowed_actions": actions, "ttl_seconds": ttl_seconds})
    return {"ticket_id": ticket_id, "token": token, "path": str(ticket_path)}


def verify_ticket(action: str, *, consume: bool = True) -> dict[str, Any]:
    """Validate the quant automation ticket carried in environment variables."""
    try:
        ticket_path = _ticket_path_from_env()
        token = os.environ.get("QUANT_AUTOMATION_TICKET_TOKEN", "").strip()
        if not token:
            raise PermissionError("missing QUANT_AUTOMATION_TICKET_TOKEN")
        if not ticket_path.exists():
            raise PermissionError("quant ticket file does not exist")
        data = json.loads(ticket_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise PermissionError("quant ticket is malformed")
        if data.get("issuer") not in ALLOWED_ISSUERS or data.get("project") != "quant":
            raise PermissionError("quant ticket issuer/project mismatch")
        if int(data.get("expires_at_epoch") or 0) < int(time()):
            raise PermissionError("quant ticket expired")
        if consume and data.get("used"):
            raise PermissionError("quant ticket already used")
        allowed = data.get("allowed_actions") or [data.get("action")]
        if action not in allowed:
            raise PermissionError(f"quant ticket does not allow action {action}")
        if data.get("token_hash") != _hash_token(token):
            raise PermissionError("quant ticket token mismatch")
    except Exception as exc:
        _audit("denied", action, {"reason": f"{type(exc).__name__}: {exc}"})
        raise

    if consume:
        data["used"] = True
        data["used_at_epoch"] = int(time())
        data["used_for_action"] = action
        data["used_by_pid"] = os.getpid()
        ticket_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _audit("allowed", action, {"ticket_id": data.get("ticket_id"), "consume": consume})
    return data


def require_ticket(action: str, *, consume: bool = True) -> None:
    try:
        verify_ticket(action, consume=consume)
    except Exception as exc:
        raise SystemExit(f"DENIED: quant automation ticket required for {action}: {exc}") from exc
