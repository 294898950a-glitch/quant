from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from framework.autonomous.execution_result_ledger import claim_result
from framework.autonomous.execution_adapters.sig_spot import SigSpotExecutionAdapter
from framework.autonomous.execution_adapters.base import ExecutionHandle
from framework.autonomous.queue_remote_execution import QueueRemoteExecutionService
from scripts import auto_research_pipeline as pipeline
from scripts import validate_l4_ack, validate_report


def _service(tmp_path, *, audits=None):
    return QueueRemoteExecutionService(
        repo_root=tmp_path,
        save_state=lambda state: None,
        write_status=lambda status, extra=None: None,
        audit=lambda action, payload=None: (audits if audits is not None else []).append((action, payload)),
        log=lambda message: None,
        mark_history=lambda state, item, status, message: None,
        rel=lambda path: str(path.resolve().relative_to(tmp_path)),
        now_iso=lambda: "2026-07-12T00:00:00+08:00",
        issue_ticket=lambda purpose: {"path": "/tmp/ticket", "token": "token"},
    )


def test_remote_command_enables_compute_node_envelope(tmp_path, monkeypatch):
    service = _service(tmp_path)
    commands = []
    monkeypatch.setattr(service, "ssh_vm", lambda vm, command, **kwargs: commands.append(command) or "123\n")
    spec = tmp_path / "data/run/spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("run_id: run\n", encoding="utf-8")
    (spec.parent / "execution_request.yaml").write_text(
        yaml.safe_dump({"run_id": "run", "request_nonce": "n1", "spec_path": "spec.yaml"}), encoding="utf-8"
    )

    assert service.start_remote_pipeline({}, {"id": "run"}, spec, {"host": "sig", "remote_repo": "/repo"}) == "123"
    assert "QUANT_COMPUTE_NODE=1" in commands[0]


def test_result_replay_and_stale_result_noop_without_queue_mutation(tmp_path, monkeypatch):
    audits = []
    service = _service(tmp_path, audits=audits)
    monkeypatch.setattr(service, "remote_running_on_vm", lambda vm, pattern: False)
    monkeypatch.setattr(service, "sync_remote_run_dir", lambda state, item, run_dir: None)
    monkeypatch.setattr(service, "required_artifacts_present", lambda spec, run_dir: True)
    monkeypatch.setattr(service, "item_vm_config", lambda state, item: {"host": "sig", "remote_repo": "/repo"})
    monkeypatch.setattr(
        "framework.autonomous.queue_remote_execution.run_recorder.backfill_run_record",
        lambda **kwargs: {"manifest_path": tmp_path / "data/research_framework/experiments/run_manifests/run.yaml"},
    )
    run = tmp_path / "data/run"
    run.mkdir(parents=True)
    (run / "spec.yaml").write_text(yaml.safe_dump({"run_id": "run", "status": "RUNNING"}), encoding="utf-8")
    item = {"id": "run", "status": "running", "request_nonce": "n1", "spec_path": "data/run/spec.yaml"}
    result = {"run_id": "run", "request_nonce": "n1", "expected_prior_status": "running", "outcome": "passed"}
    (run / "execution_result.yaml").write_text(yaml.safe_dump(result), encoding="utf-8")

    assert service.settle_running_items({}, [item]) == 1
    assert item["status"] == "review_pending"
    assert yaml.safe_load((run / "spec.yaml").read_text(encoding="utf-8"))["status"] == "COMPLETE"
    assert validate_report.report_required(run) is True
    assert validate_l4_ack.is_ack_required(run) is True

    # A crash after ledger append but before queue persistence is reconciled.
    item["status"] = "running"
    assert claim_result(tmp_path / "data/research_framework/execution_result_ledger.jsonl", queue_item=item, envelope=result, actor="test")[0] is False
    assert service.settle_running_items({}, [item]) == 1
    assert item["status"] == "review_pending"
    assert yaml.safe_load((run / "spec.yaml").read_text(encoding="utf-8"))["status"] == "COMPLETE"

    # A stale envelope remains a pure no-op and never fails state.
    item["status"] = "running"
    result["request_nonce"] = "stale"
    (run / "execution_result.yaml").write_text(yaml.safe_dump(result), encoding="utf-8")
    assert service.settle_running_items({}, [item]) == 0
    assert item["status"] == "running"
    assert [action for action, _ in audits if action == "execution_result_noop"] == ["execution_result_noop"]
    assert [action for action, _ in audits if action == "execution_result_recovered_duplicate"] == [
        "execution_result_recovered_duplicate"
    ]


def test_claimed_result_retries_bookkeeping_instead_of_becoming_failed(tmp_path, monkeypatch):
    audits = []
    service = _service(tmp_path, audits=audits)
    monkeypatch.setattr(service, "remote_running_on_vm", lambda vm, pattern: False)
    monkeypatch.setattr(service, "sync_remote_run_dir", lambda state, item, run_dir: None)
    monkeypatch.setattr(service, "required_artifacts_present", lambda spec, run_dir: True)
    monkeypatch.setattr(service, "item_vm_config", lambda state, item: {"host": "sig", "remote_repo": "/repo"})
    monkeypatch.setattr("framework.autonomous.queue_remote_execution.run_recorder.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "framework.autonomous.queue_remote_execution.run_recorder.backfill_run_record",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("temporary manifest failure")),
    )
    run = tmp_path / "data/retry"
    run.mkdir(parents=True)
    (run / "spec.yaml").write_text(yaml.safe_dump({"run_id": "retry", "status": "RUNNING"}), encoding="utf-8")
    (run / "execution_result.yaml").write_text(
        yaml.safe_dump({"run_id": "retry", "request_nonce": "n1", "expected_prior_status": "running", "outcome": "passed"}),
        encoding="utf-8",
    )
    item = {"id": "retry", "status": "running", "request_nonce": "n1", "spec_path": "data/retry/spec.yaml"}

    assert service.settle_running_items({}, [item]) == 0
    assert item["status"] == "running"
    assert yaml.safe_load((run / "spec.yaml").read_text(encoding="utf-8"))["status"] == "COMPLETE"
    assert any(action == "execution_result_bookkeeping_retry" for action, _ in audits)

    monkeypatch.setattr(
        "framework.autonomous.queue_remote_execution.run_recorder.backfill_run_record",
        lambda **kwargs: {"manifest_path": tmp_path / "data/research_framework/run_manifests/retry.yaml"},
    )
    assert service.settle_running_items({}, [item]) == 1
    assert item["status"] == "review_pending"
    assert any(action == "execution_result_recovered_duplicate" for action, _ in audits)


def test_sig_spot_probe_reports_remote_pid_liveness(tmp_path):
    commands = []
    adapter = SigSpotExecutionAdapter(
        repo_root=tmp_path,
        rel=lambda path: str(path),
        vm={"host": "sig", "remote_repo": "/repo"},
        ssh_vm=lambda vm, command, **kwargs: commands.append(command) or "alive",
    )
    assert adapter.probe(ExecutionHandle("run", "nonce", "sig_spot", "123")) is True
    assert "kill -0 123" in commands[0]

    adapter.ssh_vm = lambda vm, command, **kwargs: "dead"
    assert adapter.probe(ExecutionHandle("run", "nonce", "sig_spot", "123")) is False


def test_compute_node_result_echoes_request_identity(tmp_path, monkeypatch):
    run = tmp_path / "data/run"
    run.mkdir(parents=True)
    spec = run / "spec.yaml"
    spec.write_text(yaml.safe_dump({"run_id": "run", "status": "READY"}), encoding="utf-8")
    (run / "execution_request.yaml").write_text(
        yaml.safe_dump({"request_nonce": "n1", "expected_prior_status": "running"}), encoding="utf-8"
    )
    monkeypatch.setattr(pipeline, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "ensure_runnable_status", lambda *args: None)
    monkeypatch.setattr(pipeline, "output_dir_from_spec", lambda spec, path: run)
    monkeypatch.setattr(pipeline, "estimate_compute_metadata", lambda spec: {})
    monkeypatch.setattr(pipeline, "enforce_compute_placement", lambda *args: None)
    monkeypatch.setattr(pipeline, "require_data_quality_decision", lambda *args: {})
    monkeypatch.setattr(
        pipeline.run_recorder,
        "backfill_run_record",
        lambda **kwargs: {"verdict": {"status": "passed", "missing_artifacts": []}, "manifest_path": run / "manifest.yaml", "record_type": "backfill"},
    )
    monkeypatch.setenv("QUANT_COMPUTE_NODE", "1")

    pipeline.run_pipeline(argparse.Namespace(spec=spec, allow_archived=False, dry_run=False, no_execute=True, quiet=True))
    result = yaml.safe_load((run / "execution_result.yaml").read_text(encoding="utf-8"))
    assert {key: result[key] for key in ("run_id", "request_nonce", "expected_prior_status")} == {
        "run_id": "run", "request_nonce": "n1", "expected_prior_status": "running"
    }
