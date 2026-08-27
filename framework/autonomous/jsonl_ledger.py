"""Single durable append path for the framework's append-only JSONL ledgers.

Owned by the ``state_and_rules`` node.  Every append-only JSONL ledger in the
repository (framework change log, orchestrator audit, quant access audit,
execution result ledger, verification log, truth-sync protected-action audit,
research-question events, pending-input history, lifecycle projection diffs)
goes through :func:`append_jsonl` so all of them get the same two guarantees:

1. **Byte format.**  One line per record, encoded exactly as
   ``json.dumps(row, ensure_ascii=False, sort_keys=True) + "\\n"``.  This is the
   format every existing ledger file on disk already uses; the historical files
   are append-only evidence and must stay parseable, so the encoding is frozen
   here in :func:`encode_line` and nowhere else.

2. **Durability + atomicity.**  The record is written under an exclusive
   ``flock`` on a sidecar lock file, then ``flush()`` + ``os.fsync()``.  ``O_APPEND``
   alone is only atomic for writes up to ``PIPE_BUF`` (4096 bytes) on Linux, and
   real rows exceed that (``framework_change_log.jsonl`` rows carry
   ``evidence.status_rows``; ``orchestrator_log.jsonl`` already has 4223-byte
   lines on disk), while parallel dispatch means several processes append
   concurrently.  Without the lock two long concurrent appends can interleave
   and produce a torn, unparseable line; without the fsync a crash after the
   call returns can lose an already-"recorded" fact.

This module deliberately knows nothing about de-duplication, claims, leases or
any other ledger semantics.  Those stay in the calling module: the writer only
appends one already-decided row.

Lock protocol and why re-entrancy is safe
-----------------------------------------
The lock file for ledger ``X`` is always ``X + ".lock"`` (see
:func:`ledger_lock_path`), which is the convention the pre-existing callers
already used, so a mixed old/new process pair still excludes each other
correctly.

``flock`` locks are attached to the *open file description*, not to the process.
Opening the same lock file twice in one process therefore creates two
independent descriptions, and a second blocking ``LOCK_EX`` would wait forever
on the first — a self-deadlock.  Three call sites already hold a lock around a
wider critical section (read-modify-append) that the writer must not break up:

* ``run_recorder.record_lifecycle_projection_diff`` and
  ``ResearchQuestionLedger.append`` hold *this module's* lock, via
  :func:`ledger_lock`, for the same target file.
* ``PendingResearchInputStore._append_event`` runs under the store's own lock on
  ``pending_research_input.yaml.lock``, which serialises every history append.

Those callers pass ``lock_held=True`` so the writer appends without taking any
lock at all.  That is an explicit, statically visible contract rather than
hidden re-entrancy bookkeeping, and it means the writer never acquires a second
lock while one is held — so no lock-ordering cycle can exist.

As a backstop against a future caller forgetting the flag, the lock is acquired
with ``LOCK_NB`` polling and a bounded timeout: a mistake surfaces as a
:class:`LedgerLockTimeout` in the logs instead of an unattended cron process
hanging forever.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "LOCK_SUFFIX",
    "LedgerLockTimeout",
    "append_jsonl",
    "encode_line",
    "ledger_lock",
    "ledger_lock_path",
]

LOCK_SUFFIX = ".lock"

# Each append holds the lock for well under a millisecond, so any wait longer
# than this is a bug (missing ``lock_held=True``, or a crashed holder), not
# contention.
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0

_MIN_RETRY_SECONDS = 0.001
_MAX_RETRY_SECONDS = 0.05


class LedgerLockTimeout(TimeoutError):
    """An exclusive ledger lock could not be acquired within the timeout."""


def ledger_lock_path(path: Path | str) -> Path:
    """Return the sidecar lock file protecting ``path``."""
    target = Path(path)
    return target.with_name(target.name + LOCK_SUFFIX)


def encode_line(row: Mapping[str, Any]) -> str:
    """Encode one ledger row into its exact on-disk bytes (including newline).

    Frozen format.  Do not add ``indent``, ``separators`` or ``default``: every
    historical ledger line was produced by exactly this call.
    """
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"


def _acquire(handle: Any, lock_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + max(float(timeout), 0.0)
    delay = _MIN_RETRY_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        if time.monotonic() >= deadline:
            raise LedgerLockTimeout(
                f"could not acquire {lock_path} within {timeout}s; "
                "an outer holder must pass lock_held=True"
            )
        time.sleep(delay)
        delay = min(delay * 2, _MAX_RETRY_SECONDS)


@contextmanager
def ledger_lock(
    path: Path | str,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the exclusive ledger lock for ``path`` across a wider critical section.

    Callers that must read the ledger and then decide whether to append (dedupe,
    claim, lease) wrap both steps in this, and then pass ``lock_held=True`` to
    :func:`append_jsonl`.  Never nest this with itself for the same path in one
    process: ``flock`` is per open file description, so that would self-deadlock.
    """
    lock_path = ledger_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        _acquire(handle, lock_path, timeout)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_line(target: Path, line: str, *, fsync_dir: bool) -> None:
    # Checked before the append so a newly created ledger's directory entry can
    # be made durable too; an fsync on the file alone does not persist the link.
    created = not target.exists()
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    if created or fsync_dir:
        _fsync_dir(target.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_jsonl(
    path: Path | str,
    row: Mapping[str, Any],
    *,
    lock_held: bool = False,
    fsync_dir: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> str:
    """Append one row to an append-only JSONL ledger, durably and atomically.

    Args:
        path: Ledger file.  Parent directories are created if missing.
        row: The already-decided record.  Must be JSON-serialisable; a
            ``TypeError`` from a non-serialisable value is intentionally
            propagated, matching the previous inline ``json.dumps`` calls.
        lock_held: ``True`` when the caller already holds the exclusive lock that
            serialises appends to this ledger (see the module docstring).  The
            writer then performs no locking of its own, which is what keeps a
            caller's read-then-append critical section indivisible and makes
            self-deadlock on a per-open-file-description ``flock`` impossible.
        fsync_dir: Force an fsync of the containing directory even when the
            ledger file already existed.  The directory is always fsynced when
            this call created the file.
        lock_timeout: Bounded wait for the lock when ``lock_held`` is ``False``.

    Returns:
        The exact line written, newline included.  Useful for tests and callers
        that want to hash what they just recorded.

    Raises:
        LedgerLockTimeout: The lock was not acquired within ``lock_timeout``.
    """
    target = Path(path)
    # Encode before touching the filesystem: a bad row must not leave a
    # half-created ledger or hold the lock while raising.
    line = encode_line(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    if lock_held:
        _write_line(target, line, fsync_dir=fsync_dir)
        return line
    with ledger_lock(target, timeout=lock_timeout):
        _write_line(target, line, fsync_dir=fsync_dir)
    return line
