"""Holdout pool guard for OOS data.

Slices the OOS event set into N disjoint pools persisted in
``sealed_pools.json``. Each pool may be read at most once; subsequent
reads raise. When all pools are read/sealed the orchestrator must pause
its loop until fresh OOS data arrives.

See ``docs/plans/2026-05-07-holdout-pool-design.md`` for the full spec.

Public API
----------
- :func:`slice_oos_into_pools`
- :func:`read_pool`
- :func:`seal_pool`
- :func:`pools_remaining`

Exceptions
----------
- :class:`PoolFileExistsError`
- :class:`PoolAlreadyReadError`
- :class:`InvalidPoolIdError`

The file lock uses ``fcntl.flock`` (POSIX). The repo is not run on
Windows; if that ever changes, swap in ``msvcrt.locking``.
"""

from __future__ import annotations

import fcntl
import json
import random
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

# Default location. Callers may override via ``pool_file=...``.
DEFAULT_POOL_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "cb_redemption"
    / "sealed_pools.json"
)

SCHEMA_VERSION = 1
DEFAULT_STRATEGY = "cb_redemption"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class PoolFileExistsError(RuntimeError):
    """Raised when ``slice_oos_into_pools`` is called but the pool file
    already exists. Re-slicing would shuffle already-observed events
    across pools and break the "each pool read at most once" guarantee.
    """


class PoolAlreadyReadError(RuntimeError):
    """Raised when ``read_pool`` is called on a pool whose
    ``read_count`` is already > 0.
    """

    def __init__(self, pool_id: int, read_count: int):
        super().__init__(
            f"pool_id={pool_id} already read (read_count={read_count}); "
            "OOS pools may only be consumed once."
        )
        self.pool_id = pool_id
        self.read_count = read_count


class InvalidPoolIdError(KeyError):
    """Raised when a pool_id does not exist in the pool file."""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _locked(path: Path, mode: str) -> Iterator:
    """Open ``path`` with an exclusive flock for the duration of the block.

    The lock is released when the file handle closes, which is enough
    for our single-host coordination needs (orchestrator + occasional
    manual reader).
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


def _load_pools(pool_file: Path) -> dict:
    """Load the pool file under an exclusive lock."""
    if not pool_file.exists():
        raise FileNotFoundError(
            f"sealed_pools.json not found at {pool_file}; "
            "call slice_oos_into_pools() first."
        )
    with _locked(pool_file, "r") as f:
        return json.load(f)


def _save_pools(pool_file: Path, data: dict) -> None:
    """Atomically write the pool file (write tmp + rename) under lock."""
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pool_file.with_suffix(pool_file.suffix + ".tmp")
    with _locked(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
    tmp.replace(pool_file)


def _find_pool(data: dict, pool_id: int) -> dict:
    for pool in data["pools"]:
        if pool["id"] == pool_id:
            return pool
    raise InvalidPoolIdError(
        f"pool_id={pool_id} not in {[p['id'] for p in data['pools']]}"
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def slice_oos_into_pools(
    events: pd.DataFrame,
    event_id_col: str = "bond_id_meeting_date",
    n_pools: int = 4,
    seed: int = 42,
    pool_file: Path = DEFAULT_POOL_FILE,
    strategy: str = DEFAULT_STRATEGY,
    split_at: str | None = None,
) -> dict:
    """Slice ``events`` into ``n_pools`` disjoint holdout pools.

    Parameters
    ----------
    events : pd.DataFrame
        OOS events. Must contain ``event_id_col`` with unique values.
    event_id_col : str
        Column to use as the event primary key.
    n_pools : int
        Number of pools. Once committed it MUST NOT change for this
        strategy — re-slicing would invalidate the read-at-most-once
        guarantee for already-touched events.
    seed : int
        Seed for the pool-assignment shuffle (deterministic).
    pool_file : Path
        Where to persist ``sealed_pools.json``. Raises
        :class:`PoolFileExistsError` if already present.
    strategy : str
        Strategy name recorded in metadata.
    split_at : str | None
        Optional IS/OOS cutoff date stamped into metadata for traceability.

    Returns
    -------
    dict
        The freshly persisted pool structure.
    """
    if pool_file.exists():
        raise PoolFileExistsError(
            f"{pool_file} already exists; re-slicing would break the "
            "statistical guarantee. Delete manually only if you know what "
            "you're doing."
        )
    if n_pools < 2:
        raise ValueError(f"n_pools must be >= 2, got {n_pools}")
    if event_id_col not in events.columns:
        raise KeyError(
            f"event_id_col={event_id_col!r} not in DataFrame columns "
            f"{list(events.columns)}"
        )

    ids = events[event_id_col].astype(str).tolist()
    if len(ids) != len(set(ids)):
        dups = [i for i in set(ids) if ids.count(i) > 1]
        raise ValueError(f"duplicate event ids: {dups[:5]} ...")
    if len(ids) < n_pools:
        raise ValueError(
            f"only {len(ids)} events but n_pools={n_pools}; "
            "cannot make non-empty pools."
        )

    rng = random.Random(seed)
    rng.shuffle(ids)

    # Round-robin assignment yields pools that differ by at most 1 in size.
    buckets: list[list[str]] = [[] for _ in range(n_pools)]
    for i, eid in enumerate(ids):
        buckets[i % n_pools].append(eid)

    data = {
        "version": SCHEMA_VERSION,
        "strategy": strategy,
        "split_at": split_at,
        "n_pools": n_pools,
        "seed": seed,
        "event_id_col": event_id_col,
        "created_at": _utcnow_iso(),
        "pools": [
            {
                "id": pid,
                "event_ids": sorted(buckets[pid]),
                "read_count": 0,
                "first_read_at": None,
                "sealed_at": None,
            }
            for pid in range(n_pools)
        ],
    }
    _save_pools(pool_file, data)
    return data


def read_pool(pool_id: int, pool_file: Path = DEFAULT_POOL_FILE) -> list[str]:
    """Return the event_ids of pool ``pool_id`` and mark it as read.

    Raises
    ------
    PoolAlreadyReadError
        If ``read_count > 0``.
    InvalidPoolIdError
        If ``pool_id`` does not exist in the pool file.
    """
    data = _load_pools(pool_file)
    pool = _find_pool(data, pool_id)
    if pool["read_count"] > 0:
        raise PoolAlreadyReadError(pool_id, pool["read_count"])

    pool["read_count"] += 1
    pool["first_read_at"] = _utcnow_iso()
    _save_pools(pool_file, data)
    return list(pool["event_ids"])


def seal_pool(pool_id: int, pool_file: Path = DEFAULT_POOL_FILE) -> None:
    """Explicitly seal a pool. Idempotent — repeat calls keep the
    original ``sealed_at`` timestamp.
    """
    data = _load_pools(pool_file)
    pool = _find_pool(data, pool_id)
    if pool["sealed_at"] is None:
        pool["sealed_at"] = _utcnow_iso()
        _save_pools(pool_file, data)


def pools_remaining(pool_file: Path = DEFAULT_POOL_FILE) -> list[int]:
    """Return ids of pools whose ``read_count == 0`` (still readable).

    An empty list is the orchestrator's signal to pause the loop until
    new OOS samples arrive.
    """
    data = _load_pools(pool_file)
    return [p["id"] for p in data["pools"] if p["read_count"] == 0]
