"""Detect clusters of identical infrastructure failures and auto-pause.

When two consecutive queue tasks fail with the same root-cause signature
(e.g. both die with `ModuleNotFoundError: No module named 'scripts'`), it
means the system is burning compute on the same broken assumption. Touch
the orchestrator pause flag and surface a clear reason so the next cron
tick stops dispatching.

The detector is read-only on the queue and writes only the pause flag.
It does not change task status, does not touch spot/sig, and does not
re-queue anything — that is left to whoever resolves the underlying
defect.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Threshold: pause when this many consecutive failed tasks share the
# same signature. User mandate (2026-05-25): 2.
DEFAULT_CONSECUTIVE_THRESHOLD = 2

# Sliding window for "recent" failures. We only look at the most
# recent N failed/infra_failed tasks, sorted by failed_at desc.
DEFAULT_WINDOW = 5

_TRACEBACK_ERROR_RE = re.compile(
    r"^(?P<errtype>[A-Z][A-Za-z]*(?:Error|Exception|Warning)):\s*(?P<msg>.*)$"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _failed_at_key(item: dict[str, Any]) -> str:
    return (
        str(item.get("failed_at") or "")
        or str(item.get("infra_reclassified_at") or "")
        or ""
    )


def _candidate_failed_tasks(
    queue: list[Any], window: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in {"failed", "infra_failed"}:
            continue
        if not _failed_at_key(item):
            continue
        out.append(item)
    out.sort(key=_failed_at_key, reverse=True)
    return out[: max(window, 1)]


def signature_for(item: dict[str, Any], repo_root: Path) -> str:
    """Compute a stable cluster signature for a failed queue item.

    Priority:
    1. infra_failure_type field (set by reclassifier).
    2. Last `<Error>: <msg>` line in the task's auto_pipeline.log
       (truncated, error message normalized).
    3. The task's failure_reason string.

    The signature is intentionally coarse — we want to detect the
    *kind* of failure, not the exact file/line.
    """
    infra_type = str(item.get("infra_failure_type") or "")
    if infra_type:
        return f"infra_type:{infra_type}"

    run_dir_rel = str(item.get("run_dir") or "")
    if not run_dir_rel and item.get("id"):
        # Fall back: many tasks place the run dir under data/<id>/.
        run_dir_rel = f"data/{item['id']}"
    log_text = ""
    if run_dir_rel:
        log_path = repo_root / run_dir_rel / "auto_pipeline.log"
        if log_path.exists():
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                log_text = ""
    if log_text:
        # Look at the last 80 non-empty lines for the most recent
        # Python error tag. Tracebacks end with `<ErrorType>: <message>`.
        lines = [ln.rstrip() for ln in log_text.splitlines() if ln.strip()]
        for line in reversed(lines[-80:]):
            m = _TRACEBACK_ERROR_RE.match(line)
            if m:
                err_type = m.group("errtype")
                msg = m.group("msg")[:160]
                # Normalize a path-bug specifically — ModuleNotFoundError
                # on `scripts` is the only signature we want to collapse
                # exactly. Other messages stay verbatim (truncated).
                if err_type == "ModuleNotFoundError" and "'scripts'" in msg:
                    return "traceback:ModuleNotFoundError:scripts"
                return f"traceback:{err_type}:{msg}"
    reason = str(item.get("failure_reason") or "").strip()
    if reason:
        return f"reason:{reason[:160]}"
    return "unknown"


def evaluate(
    queue_state: dict[str, Any],
    repo_root: Path,
    *,
    threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Inspect queue state and decide whether to pause.

    Returns a dict with keys:
      - should_pause: bool
      - reason: str (human-readable)
      - signature: str (the cluster signature, if any)
      - consecutive: int (how many consecutive failures shared the signature)
      - inspected: list[str] (run_ids inspected, most recent first)
    """
    queue = queue_state.get("queue") if isinstance(queue_state, dict) else None
    if not isinstance(queue, list):
        return {
            "should_pause": False,
            "reason": "queue field is missing or not a list",
            "signature": "",
            "consecutive": 0,
            "inspected": [],
        }
    recent = _candidate_failed_tasks(queue, window)
    if len(recent) < threshold:
        return {
            "should_pause": False,
            "reason": f"only {len(recent)} recent failed tasks (need >= {threshold})",
            "signature": "",
            "consecutive": len(recent),
            "inspected": [str(it.get("id") or "") for it in recent],
        }
    head_sig = signature_for(recent[0], repo_root)
    consecutive = 1
    for item in recent[1:]:
        sig = signature_for(item, repo_root)
        if sig == head_sig:
            consecutive += 1
        else:
            break
    inspected = [str(it.get("id") or "") for it in recent]
    if consecutive >= threshold:
        return {
            "should_pause": True,
            "reason": (
                f"{consecutive} consecutive failed/infra_failed tasks "
                f"share signature '{head_sig}' "
                f"(threshold={threshold}, window={window})"
            ),
            "signature": head_sig,
            "consecutive": consecutive,
            "inspected": inspected,
        }
    return {
        "should_pause": False,
        "reason": (
            f"head signature '{head_sig}' only ran {consecutive} consecutive "
            f"(threshold={threshold})"
        ),
        "signature": head_sig,
        "consecutive": consecutive,
        "inspected": inspected,
    }


def maybe_touch_pause_flag(
    decision: dict[str, Any], pause_flag_path: Path
) -> bool:
    """If decision says should_pause and the flag is not already present,
    write the flag. Returns True if the flag was newly created."""
    if not decision.get("should_pause"):
        return False
    if pause_flag_path.exists():
        return False
    pause_flag_path.parent.mkdir(parents=True, exist_ok=True)
    payload_lines = [
        "paused_by: infra_cluster_detector",
        f"paused_at: {_now_iso()}",
        f"signature: {decision.get('signature', '')}",
        f"consecutive: {decision.get('consecutive', 0)}",
        f"reason: {decision.get('reason', '')}",
        "inspected:",
    ]
    for run_id in decision.get("inspected", []) or []:
        payload_lines.append(f"  - {run_id}")
    pause_flag_path.write_text(
        "\n".join(payload_lines) + "\n", encoding="utf-8"
    )
    return True
