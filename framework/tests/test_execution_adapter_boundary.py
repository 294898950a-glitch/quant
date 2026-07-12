from __future__ import annotations

from dataclasses import fields

from framework.autonomous.execution_adapters import ExecutionHandle


def test_execution_handle_has_transport_identity_only():
    """Adapters receive no queue state, paths to secrets, or write callbacks."""
    names = {field.name for field in fields(ExecutionHandle)}
    assert names == {"run_id", "request_nonce", "executor_id", "remote_handle"}
    assert not {"state", "save_state", "ticket", "secret"} & names
