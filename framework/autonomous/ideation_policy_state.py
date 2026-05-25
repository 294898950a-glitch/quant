"""Ideation policy state — tracks recent gate-skip events so the
ideator can show Hermes what was just rejected and refuse to keep
calling the model on directions that have already been closed.

User mandate (2026-05-26): the Research Delta Gate works as a
post-hoc filter — it stops bad proposals from entering the queue,
but it does not stop the ideation API call that produced them.
After two consecutive same-family SKIPPED_BY_DELTA_GATE outcomes,
the system should refuse to ideate again on that family until new
evidence overturns the closure.

State file: data/research_framework/ideation_policy_state.json

Shape::

    {
      "schema_version": 1,
      "updated_at": "<iso>",
      "recent_skips": [
        {
          "ts": "<iso>",
          "proposal_id": "...",
          "family": "...",
          "closing_insight": "...",
          "reason": "..."
        },
        ...
      ]
    }

`recent_skips` is bounded to the most recent
``MAX_RECENT_SKIPS`` entries. Older entries are dropped on every
record.

`cooldown_families()` derives the family-level cooldown list from
``recent_skips``: any family that appears at least
``COOLDOWN_THRESHOLD`` times across the last ``COOLDOWN_WINDOW``
skips is in cooldown.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Module-level constants — visible to tests and easy to override.
MAX_RECENT_SKIPS = 20
COOLDOWN_THRESHOLD = 2
COOLDOWN_WINDOW = 5
RECENT_SUMMARY_DEFAULT = 3  # how many recent skips to surface to Hermes by default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(state_path: Path) -> dict[str, Any]:
    """Read the policy state file; return an empty-but-valid skeleton
    if the file is missing or malformed."""
    skeleton: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": "",
        "recent_skips": [],
    }
    if not state_path.exists():
        return skeleton
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return skeleton
    if not isinstance(data, dict):
        return skeleton
    recent = data.get("recent_skips")
    if not isinstance(recent, list):
        recent = []
    cleaned: list[dict[str, Any]] = []
    for entry in recent:
        if isinstance(entry, dict):
            cleaned.append(entry)
    skeleton["recent_skips"] = cleaned
    skeleton["updated_at"] = str(data.get("updated_at") or "")
    return skeleton


def record_skip(
    state_path: Path,
    *,
    proposal_id: str,
    family: str,
    closing_insight: str,
    reason: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append a new SKIPPED_BY_DELTA_GATE event and trim to
    MAX_RECENT_SKIPS. Returns the updated state dict.
    """
    state = load_state(state_path)
    entry = {
        "ts": ts or _now_iso(),
        "proposal_id": str(proposal_id or ""),
        "family": str(family or ""),
        "closing_insight": str(closing_insight or ""),
        "reason": str(reason or ""),
    }
    recent = state.get("recent_skips") or []
    recent.append(entry)
    # Keep only the newest MAX_RECENT_SKIPS entries.
    if len(recent) > MAX_RECENT_SKIPS:
        recent = recent[-MAX_RECENT_SKIPS:]
    state["recent_skips"] = recent
    state["updated_at"] = _now_iso()
    state["schema_version"] = SCHEMA_VERSION
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return state


def cooldown_families(
    state: dict[str, Any],
    *,
    threshold: int = COOLDOWN_THRESHOLD,
    window: int = COOLDOWN_WINDOW,
) -> list[str]:
    """Derive the list of families currently in cooldown.

    A family is in cooldown when it appears at least ``threshold``
    times across the most recent ``window`` skip events.
    """
    recent = state.get("recent_skips") if isinstance(state, dict) else None
    if not isinstance(recent, list):
        return []
    # Take the tail (most recent) of the window, regardless of ts ordering
    # — entries are appended in order by record_skip, so list order is
    # already chronological.
    tail = recent[-max(window, 1):]
    counts: Counter[str] = Counter()
    for entry in tail:
        if not isinstance(entry, dict):
            continue
        fam = str(entry.get("family") or "").strip()
        if fam:
            counts[fam] += 1
    return sorted(fam for fam, c in counts.items() if c >= threshold)


def recent_skip_summary(
    state: dict[str, Any],
    *,
    n: int = RECENT_SUMMARY_DEFAULT,
) -> list[dict[str, Any]]:
    """Return the last ``n`` skip entries, newest last, as a list of
    summary dicts (suitable for showing to Hermes).
    """
    recent = state.get("recent_skips") if isinstance(state, dict) else None
    if not isinstance(recent, list):
        return []
    tail = recent[-max(n, 0):]
    out: list[dict[str, Any]] = []
    for entry in tail:
        if not isinstance(entry, dict):
            continue
        out.append({
            "ts": str(entry.get("ts") or ""),
            "proposal_id": str(entry.get("proposal_id") or ""),
            "family": str(entry.get("family") or ""),
            "closing_insight": str(entry.get("closing_insight") or ""),
            "reason": str(entry.get("reason") or ""),
        })
    return out
