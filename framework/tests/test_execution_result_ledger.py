from __future__ import annotations

import pytest

from framework.autonomous.execution_result_ledger import ResultRejected, claim_result


def _item():
    return {"id": "run-1", "status": "running", "request_nonce": "nonce-1"}


def _result(**updates):
    value = {"run_id": "run-1", "request_nonce": "nonce-1", "expected_prior_status": "running", "outcome": "passed"}
    value.update(updates)
    return value


def test_first_result_claims_and_duplicate_is_noop(tmp_path):
    path = tmp_path / "execution_result_ledger.jsonl"
    assert claim_result(path, queue_item=_item(), envelope=_result(), actor="macbook")[0] is True
    assert claim_result(path, queue_item=_item(), envelope=_result(), actor="macbook") == (False, "duplicate_result")


@pytest.mark.parametrize("updates", [{"request_nonce": "wrong"}, {"expected_prior_status": "queued"}, {"run_id": "other"}])
def test_invalid_result_is_rejected_without_ledger_write(tmp_path, updates):
    path = tmp_path / "execution_result_ledger.jsonl"
    with pytest.raises(ResultRejected):
        claim_result(path, queue_item=_item(), envelope=_result(**updates), actor="macbook")
    assert not path.exists()
