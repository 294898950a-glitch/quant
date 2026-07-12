"""Controller/compute transport contract.

Adapters deliberately receive no queue state and expose no state-writing API.
The controller owns validation, result application, ledgers, and requeue logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExecutionHandle:
    run_id: str
    request_nonce: str
    executor_id: str
    remote_handle: str


class ExecutionAdapter(Protocol):
    """Idempotent transport interface for a single compute target."""

    executor_id: str

    def submit(self, request_path: Path) -> ExecutionHandle: ...

    def probe(self, handle: ExecutionHandle) -> bool: ...

    def collect(self, handle: ExecutionHandle, destination: Path) -> Path | None: ...

    def cancel(self, handle: ExecutionHandle) -> None: ...
