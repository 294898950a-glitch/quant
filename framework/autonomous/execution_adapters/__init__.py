"""Transport-only execution adapters owned by the runner node."""

from .base import ExecutionAdapter, ExecutionHandle
from .sig_spot import SigSpotExecutionAdapter

__all__ = ["ExecutionAdapter", "ExecutionHandle", "SigSpotExecutionAdapter"]
