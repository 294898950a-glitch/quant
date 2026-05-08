"""Memory layer for the self-loop framework — layer 6.

Persists every loop iteration's artefacts (params, backtest result,
diagnosis, audit, hypothesis attempt) as one JSON line per run, and
keeps a separate index of *directions already tried* so the orchestrator
/ editor can refuse to retry an identical edit.

Two append-only JSONL stores live under ``data/cb_redemption/``:

- ``runs.jsonl``              — one :class:`RunRecord` per loop iteration
- ``tried_directions.jsonl``  — one :class:`AttemptKey` + outcome per attempt

This module does NOT:

- import ``backtest.py`` (caller passes ``BacktestResult.to_dict()`` in)
- shell out to git (orchestrator owns commits; we just store the hash)
- talk to Notion (lives in ``notion_logger.py``)

File access is serialised through ``fcntl.flock``. POSIX-only — same
caveat as ``holdout.py``.

Public API
----------
- :class:`RunRecord`,    :func:`append_run`,    :func:`read_runs`,    :func:`latest_iteration`
- :class:`AttemptKey`,   :func:`record_attempt`, :func:`has_been_tried`, :func:`search_history`
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# Default storage locations
# --------------------------------------------------------------------------- #

_DATA_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "cb_redemption"
)
DEFAULT_RUNS_FILE = _DATA_DIR / "runs.jsonl"
DEFAULT_ATTEMPTS_FILE = _DATA_DIR / "tried_directions.jsonl"


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


@contextmanager
def _locked(path: Path, mode: str) -> Iterator:
    """Open ``path`` with an exclusive ``flock`` for the duration of the block.

    Same pattern as ``holdout._locked`` — single-host coordination is
    enough; lock is released on close.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, mode)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file; missing file → empty list (never raises)."""
    if not path.exists():
        return []
    with _locked(path, "r") as f:
        out: list[dict] = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out


def _append_jsonl(path: Path, obj: dict) -> None:
    """Append one object as a single JSONL line under exclusive lock."""
    with _locked(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True))
        f.write("\n")
        f.flush()


def _bucket_for(value: Any) -> str:
    """Quantise ``value`` into a stable bucket string.

    Floats are rounded to 2 decimals — so 0.501 and 0.502 hash to the
    same bucket and are treated as the same direction. Other types
    (int / bool / str) are stringified verbatim.
    """
    if isinstance(value, bool):
        # bool is a subclass of int; keep its own branch first.
        return str(value)
    if isinstance(value, float):
        return f"{round(value, 2):.2f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


# --------------------------------------------------------------------------- #
# Run records
# --------------------------------------------------------------------------- #


@dataclass
class RunRecord:
    """One row of ``runs.jsonl`` — the full receipt of a single loop iteration.

    ``backtest`` is intentionally typed as ``dict`` so this module does
    not import ``backtest.py``; callers pass ``BacktestResult.to_dict()``.

    ``pool_stats`` carries the raw numerical description of the data
    slice the iteration ran on (see :mod:`pool_stats`). It is purely
    informational — never labels, just numbers — so future audits can
    reconstruct what the data looked like without re-loading parquet.
    Optional and ``None`` for strategies that do not wire the loader in.
    """

    run_id: str
    iteration: int
    timestamp_iso: str
    phase: str  # "inner" | "outer"
    params: dict
    backtest: dict
    diagnosis: dict | None = None
    hypothesis_attempt: dict | None = None
    audit: dict | None = None
    git_commit: str | None = None
    pool_stats: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(
            run_id=d["run_id"],
            iteration=d["iteration"],
            timestamp_iso=d["timestamp_iso"],
            phase=d["phase"],
            params=d.get("params", {}),
            backtest=d.get("backtest", {}),
            diagnosis=d.get("diagnosis"),
            hypothesis_attempt=d.get("hypothesis_attempt"),
            audit=d.get("audit"),
            git_commit=d.get("git_commit"),
            pool_stats=d.get("pool_stats"),
        )


def append_run(record: RunRecord, path: Path = DEFAULT_RUNS_FILE) -> None:
    """Append one :class:`RunRecord` to ``runs.jsonl``.

    Locks the file but does NOT read existing content — append-only.
    """
    _append_jsonl(Path(path), record.to_dict())


def read_runs(
    path: Path = DEFAULT_RUNS_FILE,
    last_n: int | None = None,
) -> list[RunRecord]:
    """Read all (or the last ``last_n``) :class:`RunRecord`\\ s.

    Missing file → empty list, no exception.
    """
    rows = _read_jsonl(Path(path))
    if last_n is not None:
        rows = rows[-last_n:]
    return [RunRecord.from_dict(r) for r in rows]


def latest_iteration(path: Path = DEFAULT_RUNS_FILE) -> int:
    """Return the largest ``iteration`` seen so far; 0 if file empty/missing."""
    rows = _read_jsonl(Path(path))
    if not rows:
        return 0
    return max(int(r.get("iteration", 0)) for r in rows)


# --------------------------------------------------------------------------- #
# Tried-direction index
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AttemptKey:
    """Identifies a directional edit so we can detect duplicates.

    - ``item_path``   : dotted path inside ``tunable_space.yaml``,
                        e.g. ``"parameters.w_redeem_progress"``.
    - ``direction``   : ``"increase" | "decrease" | "set"``.
    - ``bucket_value``: the *quantised* new value (see :func:`_bucket_for`).
                        Comparisons are bucket-level so 0.501 vs 0.502 are
                        considered the same try.
    """

    item_path: str
    direction: str
    bucket_value: str

    def to_str(self) -> str:
        return f"{self.item_path}|{self.direction}|{self.bucket_value}"

    @classmethod
    def from_value(
        cls,
        item_path: str,
        direction: str,
        new_value: Any,
    ) -> "AttemptKey":
        """Build an :class:`AttemptKey` from the raw post-edit value."""
        return cls(
            item_path=item_path,
            direction=direction,
            bucket_value=_bucket_for(new_value),
        )

    def to_dict(self) -> dict:
        return {
            "item_path": self.item_path,
            "direction": self.direction,
            "bucket_value": self.bucket_value,
        }


def record_attempt(
    key: AttemptKey,
    run_id: str,
    outcome: str,
    path: Path = DEFAULT_ATTEMPTS_FILE,
    timestamp_iso: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append one attempt row to ``tried_directions.jsonl``.

    ``outcome`` is one of ``"accepted" | "rejected" | "no_change"``.
    """
    if outcome not in {"accepted", "rejected", "no_change"}:
        raise ValueError(
            f"outcome must be accepted|rejected|no_change, got {outcome!r}"
        )
    row = {
        "key": key.to_str(),
        **key.to_dict(),
        "run_id": run_id,
        "outcome": outcome,
    }
    if timestamp_iso is not None:
        row["timestamp_iso"] = timestamp_iso
    if extra:
        row["extra"] = extra
    _append_jsonl(Path(path), row)


def has_been_tried(
    key: AttemptKey,
    path: Path = DEFAULT_ATTEMPTS_FILE,
) -> list[dict]:
    """Return every prior attempt row matching ``key`` (may be empty)."""
    rows = _read_jsonl(Path(path))
    needle = key.to_str()
    return [r for r in rows if r.get("key") == needle]


def search_history(
    item_path: str | None = None,
    since_iso: str | None = None,
    path: Path = DEFAULT_ATTEMPTS_FILE,
) -> list[dict]:
    """Generic query over ``tried_directions.jsonl``.

    Both filters are optional and AND-ed when supplied.

    - ``item_path`` : exact match on ``item_path`` field
    - ``since_iso`` : keep rows whose ``timestamp_iso`` is ``>= since_iso``
                      (lexicographic — works for ISO 8601 UTC).
    """
    rows = _read_jsonl(Path(path))
    out = []
    for r in rows:
        if item_path is not None and r.get("item_path") != item_path:
            continue
        if since_iso is not None:
            ts = r.get("timestamp_iso")
            if ts is None or ts < since_iso:
                continue
        out.append(r)
    return out


__all__ = [
    "RunRecord",
    "AttemptKey",
    "DEFAULT_RUNS_FILE",
    "DEFAULT_ATTEMPTS_FILE",
    "append_run",
    "read_runs",
    "latest_iteration",
    "record_attempt",
    "has_been_tried",
    "search_history",
]


# Silence unused-import lint for ``field`` if ever used.
_ = field
