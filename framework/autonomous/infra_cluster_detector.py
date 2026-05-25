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

Recovery awareness (2026-05-26 mandate):
The detector accepts a `recovery_armed_at` cutoff. Tasks whose
failed_at / infra_reclassified_at predates this cutoff are treated as
*historical* and skipped — they belong to a prior incident that was
already resolved. Only failures *after* the cutoff count toward the
consecutive-same-signature threshold. This prevents the detector from
re-pausing the orchestrator on the very first tick after an unpause
just because old infra_failed corpses still live in the queue. A
default rolling lookback (DEFAULT_LOOKBACK_MINUTES) provides a second
safety net so unparseable/missing cutoffs do not silently re-enable
the legacy "all history is in scope" behaviour.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Threshold: pause when this many consecutive failed tasks share the
# same signature. User mandate (2026-05-25): 2.
DEFAULT_CONSECUTIVE_THRESHOLD = 2

# Sliding window for "recent" failures. We only look at the most
# recent N failed/infra_failed tasks, sorted by failed_at desc.
DEFAULT_WINDOW = 5

# Default rolling lookback (minutes). Failures older than this are
# skipped even when recovery_armed_at is missing — prevents historical
# corpses from triggering the detector. User mandate (2026-05-26): 60.
DEFAULT_LOOKBACK_MINUTES = 60

# Where the detector tracks the most recent recovery point. Touched by
# `mark_recovery_armed()` when the orchestrator is unpaused.
DEFAULT_STATE_FILENAME = "cluster_detector_state.json"

_TRACEBACK_ERROR_RE = re.compile(
    r"^(?P<errtype>[A-Z][A-Za-z]*(?:Error|Exception|Warning)):\s*(?P<msg>.*)$"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    # Tolerate trailing Z (RFC3339) and offset-less timestamps.
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _failed_at_key(item: dict[str, Any]) -> str:
    return (
        str(item.get("failed_at") or "")
        or str(item.get("infra_reclassified_at") or "")
        or ""
    )


def _effective_cutoff(
    recovery_armed_at: str | None,
    lookback_minutes: int,
) -> tuple[datetime | None, str]:
    """Compute the effective time cutoff. Only failures whose
    failed_at >= cutoff are counted. Returns (cutoff_dt, source_label).

    The effective cutoff is `max(recovery_armed_at, now - lookback)`:
    - if recovery_armed_at is set, never go earlier than that
    - regardless of recovery_armed_at, never go earlier than the
      rolling lookback window
    """
    now = _now_dt()
    lookback_cutoff = now - timedelta(minutes=max(0, lookback_minutes))
    armed_cutoff = _parse_iso(recovery_armed_at or "")
    if armed_cutoff is None and lookback_minutes <= 0:
        return None, "none"
    if armed_cutoff is None:
        return lookback_cutoff, f"lookback_{lookback_minutes}m"
    if armed_cutoff >= lookback_cutoff:
        return armed_cutoff, "recovery_armed_at"
    return lookback_cutoff, f"lookback_{lookback_minutes}m"


def _candidate_failed_tasks(
    queue: list[Any],
    window: int,
    cutoff: datetime | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in {"failed", "infra_failed"}:
            continue
        failed_at_str = _failed_at_key(item)
        if not failed_at_str:
            continue
        if cutoff is not None:
            failed_dt = _parse_iso(failed_at_str)
            if failed_dt is None:
                # Unparseable timestamp — be conservative and skip it.
                # The task can re-enter the window once it has a valid
                # timestamp; until then it should not unbalance the
                # consecutive-signature accounting.
                continue
            if failed_dt < cutoff:
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


def load_recovery_armed_at(state_path: Path) -> str:
    """Read `recovery_armed_at` from the cluster detector state file.

    Returns an empty string when the file is missing or unparseable —
    callers can decide whether to fall back to a pure lookback window.
    """
    if not state_path.exists():
        return ""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("recovery_armed_at") or "")


def mark_recovery_armed(
    state_path: Path,
    *,
    armed_by: str,
    armed_at: str | None = None,
    notes: str | None = None,
) -> str:
    """Write the recovery cutoff to the state file. Called when the
    orchestrator is unpaused so the detector can ignore failures
    older than this moment.

    Returns the stored armed_at timestamp.
    """
    stamp = armed_at or _now_iso()
    payload: dict[str, Any] = {
        "recovery_armed_at": stamp,
        "armed_by": armed_by,
    }
    if notes:
        payload["notes"] = notes
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return stamp


def evaluate(
    queue_state: dict[str, Any],
    repo_root: Path,
    *,
    threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
    window: int = DEFAULT_WINDOW,
    recovery_armed_at: str | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> dict[str, Any]:
    """Inspect queue state and decide whether to pause.

    Returns a dict with keys:
      - should_pause: bool
      - reason: str (human-readable, identifies whether this is a
        recovery-mode trip or a fresh-failure trip)
      - signature: str (the cluster signature, if any)
      - consecutive: int (how many consecutive failures shared the signature)
      - inspected: list[str] (run_ids inspected, most recent first)
      - cutoff: str (effective cutoff ISO, empty if none applied)
      - cutoff_source: str ("recovery_armed_at", "lookback_<N>m", or "none")
    """
    cutoff_dt, cutoff_source = _effective_cutoff(recovery_armed_at, lookback_minutes)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds") if cutoff_dt else ""

    queue = queue_state.get("queue") if isinstance(queue_state, dict) else None
    if not isinstance(queue, list):
        return {
            "should_pause": False,
            "reason": "queue field is missing or not a list",
            "signature": "",
            "consecutive": 0,
            "inspected": [],
            "cutoff": cutoff_iso,
            "cutoff_source": cutoff_source,
        }
    recent = _candidate_failed_tasks(queue, window, cutoff_dt)
    if len(recent) < threshold:
        return {
            "should_pause": False,
            "reason": (
                f"only {len(recent)} recent failed tasks after cutoff "
                f"({cutoff_source}={cutoff_iso or 'n/a'}); need >= {threshold}"
            ),
            "signature": "",
            "consecutive": len(recent),
            "inspected": [str(it.get("id") or "") for it in recent],
            "cutoff": cutoff_iso,
            "cutoff_source": cutoff_source,
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
        scope = (
            "post-recovery"
            if cutoff_source == "recovery_armed_at"
            else f"rolling-window ({cutoff_source})"
        )
        return {
            "should_pause": True,
            "reason": (
                f"{consecutive} consecutive {scope} failed/infra_failed tasks "
                f"share signature '{head_sig}' "
                f"(threshold={threshold}, window={window}, "
                f"cutoff={cutoff_iso or 'n/a'})"
            ),
            "signature": head_sig,
            "consecutive": consecutive,
            "inspected": inspected,
            "cutoff": cutoff_iso,
            "cutoff_source": cutoff_source,
        }
    return {
        "should_pause": False,
        "reason": (
            f"head signature '{head_sig}' only ran {consecutive} consecutive "
            f"(threshold={threshold}, cutoff={cutoff_iso or 'n/a'})"
        ),
        "signature": head_sig,
        "consecutive": consecutive,
        "inspected": inspected,
        "cutoff": cutoff_iso,
        "cutoff_source": cutoff_source,
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
        f"cutoff: {decision.get('cutoff', '')}",
        f"cutoff_source: {decision.get('cutoff_source', '')}",
        f"reason: {decision.get('reason', '')}",
        "inspected:",
    ]
    for run_id in decision.get("inspected", []) or []:
        payload_lines.append(f"  - {run_id}")
    pause_flag_path.write_text(
        "\n".join(payload_lines) + "\n", encoding="utf-8"
    )
    return True
