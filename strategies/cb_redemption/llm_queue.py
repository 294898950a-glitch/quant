"""Single-host queue guard for shared LLM calls.

The concurrent pool runner starts multiple worker processes. They may all
need DeepSeek advice, but they share one API key and one model quota. This
module serializes those calls with a filesystem lock and writes a compact
audit trail so later retrospectives can see who waited for the model.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_lock_path() -> Path:
    return Path("data") / "llm_queue" / "deepseek.lock"


def _lock_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_LLM_LOCK", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _append_event(path: Path, event: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


@contextmanager
def deepseek_slot(*, role: str) -> Iterator[None]:
    """Serialize one DeepSeek request across local worker processes."""
    if not _lock_enabled():
        yield
        return

    lock_path = Path(os.environ.get("LLM_QUEUE_LOCK_PATH") or _default_lock_path())
    log_path = Path(
        os.environ.get("LLM_QUEUE_LOG_PATH")
        or lock_path.with_name("requests.jsonl")
    )
    request_id = uuid.uuid4().hex
    pid = os.getpid()
    queued_at = time.monotonic()
    base = {
        "request_id": request_id,
        "pid": pid,
        "role": role,
    }

    _append_event(
        log_path,
        {
            **base,
            "event": "queued",
            "timestamp_iso": _utcnow_iso(),
        },
    )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        waited_s = round(time.monotonic() - queued_at, 3)
        _append_event(
            log_path,
            {
                **base,
                "event": "acquired",
                "timestamp_iso": _utcnow_iso(),
                "waited_s": waited_s,
            },
        )
        try:
            yield
        finally:
            _append_event(
                log_path,
                {
                    **base,
                    "event": "released",
                    "timestamp_iso": _utcnow_iso(),
                    "held_after_wait_s": round(time.monotonic() - queued_at, 3),
                },
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


__all__ = ["deepseek_slot"]
