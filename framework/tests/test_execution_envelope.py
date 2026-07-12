from __future__ import annotations

import yaml

from framework.autonomous.execution_envelope import write_request


def test_request_envelope_is_versioned_and_credential_free(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump({"compute_estimate": {"local_minutes": 3}}), encoding="utf-8")
    path = write_request(spec, queue_item={"id": "r1", "request_nonce": "nonce"}, executor_id="hzpc")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["request_nonce"] == "nonce"
    assert "ticket" not in str(data).lower()
    assert ".env" not in str(data)
