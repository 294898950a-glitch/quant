"""Shared controller ownership fence for controller-side entrypoints."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


def owner_allows(*, current_path: Path, local_host: str | None = None) -> tuple[bool, str]:
    """Return whether this host may mutate controller-owned runtime state.

    A missing controller block is an explicitly temporary legacy mode used only
    before the cron-registry-managed controller cutover.
    """
    if not current_path.exists():
        return True, "controller metadata absent; legacy controller allowed"
    try:
        current = yaml.safe_load(current_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        # During an explicit cutover, failing open could permit two controllers.
        # Deny without crashing so cron sees a safe, auditable no-op instead.
        return False, f"controller metadata unreadable; controller denied: {type(exc).__name__}"
    controller = current.get("controller") if isinstance(current, dict) else None
    if not isinstance(controller, dict) or not controller.get("owner_host"):
        return True, "controller metadata absent; legacy controller allowed"
    owner = str(controller["owner_host"])
    local = local_host or os.environ.get("QUANT_CONTROLLER_HOST") or os.uname().nodename
    if local == owner:
        return True, f"controller owner matched: {owner}"
    return False, f"controller owner mismatch: local={local} owner={owner}"


def audit_noop(*, audit_path: Path, reason: str, action: str = "controller_owner_noop") -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "reason": reason,
    }
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
