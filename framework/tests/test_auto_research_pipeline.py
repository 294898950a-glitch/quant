from __future__ import annotations

import json
import yaml
import pytest
import pandas as pd

from scripts import auto_research_pipeline as pipeline
from scripts.gatekeeper import GateKeeper
from scripts import repair_data_quality
from scripts import research_queue_runner
from scripts import validate_data_quality
from framework.autonomous import executor_requirements
from framework.autonomous import result_classification
from framework.autonomous import run_recorder
from framework.autonomous.queue_remote_execution import QueueRemoteExecutionService
from framework.autonomous.queue_remote_execution import _declared_path_to_repo_path
from framework.autonomous.queue_remote_execution import pipeline_execution_failed
from framework.autonomous.queue_remote_execution import should_sync_path_for_run
from framework.autonomous.queue_ideation import QueueIdeationService


def _remote_service(tmp_path):
    return QueueRemoteExecutionService(
        repo_root=tmp_path,
        save_state=lambda state: None,
        write_status=lambda status, extra=None: None,
        audit=lambda action, payload=None: None,
        log=lambda message: None,
        mark_history=research_queue_runner.mark_history,
        rel=lambda path: str(path.resolve().relative_to(tmp_path)) if path.resolve().is_relative_to(tmp_path) else str(path),
        now_iso=lambda: "2026-05-21T00:00:00",
        issue_ticket=lambda purpose: {"path": "/tmp/ticket", "token": "token"},
    )


def _ideation_service_for_test(tmp_path, *, state=None, statuses=None):
    state_holder = {"state": state or {"enabled": True, "queue": []}, "statuses": statuses or []}
    return QueueIdeationService(
        repo_root=tmp_path,
        load_state=lambda: state_holder["state"],
        save_state=lambda new_state: state_holder.update(state=new_state),
        write_status=lambda status, extra=None: state_holder["statuses"].append((status, extra)),
        audit=lambda action, payload=None: None,
        log=lambda message: None,
        mark_history=lambda state, item, status, message: None,
        rel=lambda path: str(path.resolve().relative_to(tmp_path)) if path.resolve().is_relative_to(tmp_path) else str(path),
        now_iso=lambda: "2026-05-22T00:00:00",
        ideation_env=lambda: {},
        timeout_seconds=1,
    )


def test_ideation_retries_non_runnable_until_actionable(tmp_path, monkeypatch):
    service = _ideation_service_for_test(tmp_path)
    calls = []

    def fake_generate_next_spec_if_idle(state):
        calls.append(state)
        return "ideation_not_runnable" if len(calls) == 1 else "queued_ideation_spec"

    monkeypatch.setattr(service, "generate_next_spec_if_idle", fake_generate_next_spec_if_idle)

    result = service.generate_until_actionable({"queue": []}, max_attempts=3)

    assert result == "queued_ideation_spec"
    assert len(calls) == 2


def test_non_runnable_draft_is_suppressed_for_next_ideation_scan(tmp_path):
    run_dir = tmp_path / "data" / "draft_run"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "status": "DRAFT",
                "notes": "no strict executor match",
                "proposal": {"proposal_id": "draft_run"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    request_path = run_dir / "executor_tool_request.yaml"
    request_path.write_text(
        yaml.safe_dump({"status": "awaiting_hermes_executor_code"}, allow_unicode=True),
        encoding="utf-8",
    )
    service = _ideation_service_for_test(tmp_path)

    service.suppress_non_runnable_draft(
        {
            "proposal_id": "draft_run",
            "spec_path": str(spec_path),
            "reason": "no strict executor match",
        },
        "DRAFT",
    )

    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    request = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    assert spec["automation_skip"]["status"] == "skipped_non_runnable"
    assert request["status"] == "automation_skipped_non_runnable"


def test_estimate_compute_metadata_is_record_only():
    spec = {
        "compute_estimate": {
            "sig_minutes": 0,
            "spot_minutes": 60,
            "estimated_cost_yuan": 10,
        },
    }

    result = pipeline.estimate_compute_metadata(spec)

    assert result["estimated_compute_cost_yuan"] == 10
    assert result["decision"] == "record-only"


def test_result_decision_status_comes_from_mapping():
    assert result_classification.status_for_decision("no_adoption_decision_unusable") == "rejected"
    assert result_classification.evidence_usable("no_adoption_decision_unusable") is False


def test_command_placeholder_expansion(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    output_dir = tmp_path / "out"
    spec = {
        "run_id": "run_a",
        "automation": {
            "command": ["python3", "x.py", "--spec", "{spec_path}", "--out", "{output_dir}", "--run", "{run_id}"]
        },
    }

    command = pipeline.command_from_spec(spec, spec_path, output_dir)

    assert command[0:2] == ["python3", "x.py"]
    assert "run_a" in command
    assert any(part.endswith("spec.yaml") for part in command)


def test_pipeline_normalizes_local_repo_absolute_paths():
    spec_path = pipeline.REPO_ROOT / "data" / "run_a" / "spec.yaml"
    spec = {
        "run_id": "run_a",
        "automation": {
            "output_dir": "/home/jay/projects/quant/data/run_a",
            "command": [
                "python3",
                "x.py",
                "--output-dir",
                "/home/jay/projects/quant/data/run_a",
            ],
        },
    }

    output_dir = pipeline.output_dir_from_spec(spec, spec_path)
    command = pipeline.command_from_spec(spec, spec_path, output_dir)

    assert output_dir == pipeline.REPO_ROOT / "data" / "run_a"
    assert command[-1] == "data/run_a"


def test_compute_placement_reads_protocol_allowed_hostnames(monkeypatch, tmp_path):
    protocol = tmp_path / "protocol_rules.yaml"
    protocol.write_text(
        yaml.safe_dump({
            "rules": [
                {"id": "R10", "allowed_hostnames": ["allowed-vm"]},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "PROTOCOL_RULES", protocol)
    monkeypatch.setattr(pipeline.socket, "gethostname", lambda: "allowed-vm")
    spec = {"compute_estimate": {"spot_minutes": 1, "local_minutes": 0}}

    pipeline.enforce_compute_placement(spec, dry_run=False, no_execute=False)

    monkeypatch.setattr(pipeline.socket, "gethostname", lambda: "local-box")
    with pytest.raises(pipeline.PipelineError):
        pipeline.enforce_compute_placement(spec, dry_run=False, no_execute=False)


def test_executor_requires_data_quality_decision_before_run(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec = {"run_id": "run_a", "automation": {"command": ["python3", "x.py"]}}

    with pytest.raises(pipeline.PipelineError, match="data quality decision missing"):
        pipeline.require_data_quality_decision(spec, spec_path, dry_run=False, no_execute=False)

    (tmp_path / pipeline.DATA_QUALITY_DECISION_FILE).write_text(
        yaml.safe_dump({"schema_version": 1, "run_id": "run_a", "status": "pass"}),
        encoding="utf-8",
    )

    decision = pipeline.require_data_quality_decision(spec, spec_path, dry_run=False, no_execute=False)

    assert decision["status"] == "pass"


def test_pipeline_delegates_run_recording_to_recorder():
    source = pipeline.Path("scripts/auto_research_pipeline.py").read_text(encoding="utf-8")
    assert "from framework.autonomous import run_recorder" in source
    assert "run_recorder.record_executed_run" in source
    assert "run_recorder.backfill_run_record" in source


def test_run_recorder_separates_execution_from_backfill():
    source = pipeline.Path("framework/autonomous/run_recorder.py").read_text(encoding="utf-8")
    assert "def record_executed_run" in source
    assert "def backfill_run_record" in source
    assert "executed run record requires executed command" in source
    assert "record_type" in source
    assert "may_trigger_next_research" in source


def test_executed_run_with_missing_artifact_records_failure(tmp_path, monkeypatch):
    spec_path = tmp_path / "spec.yaml"
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    spec = {
        "run_id": "r_missing",
        "strategy_id": "s1",
        "artifacts_required": ["summary.json"],
        "data_window": {"start": "2020-01-01", "end": "2020-12-31"},
    }
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    experiments = tmp_path / "experiments.yaml"
    manifests = tmp_path / "manifests"
    monkeypatch.setattr(run_recorder, "EXPERIMENTS", experiments)
    monkeypatch.setattr(run_recorder, "MANIFEST_DIR", manifests)

    record = run_recorder.record_executed_run(
        spec=spec,
        spec_path=spec_path,
        output_dir=output_dir,
        command=["python3", "x.py"],
        start_at="2026-05-20T00:00:00Z",
        end_at="2026-05-20T00:01:00Z",
        exit_code=0,
        compute_metadata={"estimated_compute_cost_yuan": 1, "decision": "record-only"},
        data_quality_decision={"status": "pass"},
    )

    assert record["record_type"] == run_recorder.NORMAL_RECORD_TYPE
    assert record["verdict"]["status"] == "abandoned"
    assert record["verdict"]["decision"] == "missing_artifacts"
    assert record["verdict"]["missing_artifacts"]


def test_backfill_record_cannot_trigger_next_research(tmp_path, monkeypatch):
    spec_path = tmp_path / "spec.yaml"
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    spec = {"run_id": "r_backfill", "strategy_id": "s1", "artifacts_required": []}
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    experiments = tmp_path / "experiments.yaml"
    manifests = tmp_path / "manifests"
    monkeypatch.setattr(run_recorder, "EXPERIMENTS", experiments)
    monkeypatch.setattr(run_recorder, "MANIFEST_DIR", manifests)

    record = run_recorder.backfill_run_record(
        spec=spec,
        spec_path=spec_path,
        output_dir=output_dir,
        reason="historical import",
        actor="test",
        evidence_paths=[str(output_dir)],
    )

    manifest = yaml.safe_load(record["manifest_path"].read_text(encoding="utf-8"))
    assert record["record_type"] == run_recorder.BACKFILL_RECORD_TYPE
    assert manifest["record_type"] == run_recorder.BACKFILL_RECORD_TYPE
    assert manifest["backfill"]["new_execution"] is False
    assert manifest["backfill"]["may_trigger_next_research"] is False


def test_data_quality_ai_judge_requires_ticket():
    source = pipeline.Path("scripts/validate_data_quality.py").read_text(encoding="utf-8")
    assert 'require_ticket("data_quality_judge")' in source
    assert 'if args.judge_summary_stdin:\n        require_ticket("data_quality_judge")' not in source
    assert "deterministic_decision" not in source
    assert "repair_candidate" in source
    assert "status_code:" in source
    assert "data_quality_decision" in source


def test_data_repairer_is_separate_and_revalidated():
    remote_runner = pipeline.Path("framework/autonomous/queue_remote_execution.py").read_text(encoding="utf-8")
    repairer = pipeline.Path("scripts/repair_data_quality.py").read_text(encoding="utf-8")
    assert "scripts/repair_data_quality.py" in remote_runner
    assert 'issue_ticket("data_quality_repair")' in remote_runner
    assert "repaired_summary = self.remote_data_quality_summary" in remote_runner
    assert 'require_ticket("data_quality_repair")' in repairer
    assert "status=repair_candidate" in repairer or "repair_candidate" in repairer
    assert "RegisteredProviderAdapter" in repairer
    assert "generated_repair.py" in repairer
    assert "original data was not overwritten" in repairer


def test_required_large_data_is_synced_for_run(tmp_path):
    spec_path = tmp_path / "data" / "run_a" / "spec.yaml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("run_id: run_a\n", encoding="utf-8")
    required = {"data/cb_warehouse/cb_daily.parquet"}

    assert should_sync_path_for_run(
        tmp_path / "data" / "cb_warehouse" / "cb_daily.parquet",
        "data/cb_warehouse/cb_daily.parquet",
        spec_path,
        tmp_path,
        required,
    )
    assert not should_sync_path_for_run(
        tmp_path / "data" / "other" / "historical.parquet",
        "data/other/historical.parquet",
        spec_path,
        tmp_path,
        required,
    )


def test_declared_absolute_data_path_is_normalized_to_repo_data(tmp_path):
    repo_data = tmp_path / "data" / "run_a" / "input.parquet"
    repo_data.parent.mkdir(parents=True)
    repo_data.write_bytes(b"stub")

    normalized = _declared_path_to_repo_path(tmp_path, "/home/jay/data/run_a/input.parquet")

    assert normalized == repo_data


def test_recovered_vm_avoidance_is_cleared_before_retry(tmp_path):
    state = {"queue": []}
    item = {
        "id": "run_a",
        "status": "queued",
        "avoid_vm_ids": ["guangzhou_spot"],
        "last_start_error_at": "2026-05-22T01:00:00",
    }
    state["queue"].append(item)
    service = _remote_service(tmp_path)

    changed = service.clear_recovered_vm_avoidances(state, state["queue"], [{"id": "guangzhou_spot"}])

    assert changed == 1
    assert "avoid_vm_ids" not in item
    assert item["workflow_stage"] == "queued_after_recovered_vm_probe"


def test_data_quality_ai_judge_is_primary_for_old_warehouse_pointer(monkeypatch):
    summary = {
        "schema_version": 1,
        "data_files": [
            {
                "path": f"data/old_run/{rel_path}",
                "exists": False,
                "readable": False,
            }
            for rel_path in (
                "data/cb_warehouse/cb_basic.parquet",
                "data/cb_warehouse/cb_daily.parquet",
            )
        ],
    }

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def call_active_provider(self, prompt, schema):
            assert "data/old_run/data/cb_warehouse/cb_basic.parquet" in prompt
            return type(
                "Resp",
                (),
                {
                    "content": yaml.safe_dump(
                        {
                            "status_code": 2,
                            "confidence_code": 1,
                            "blocking_issues": [],
                            "warnings": [],
                            "fix_plan": [{"action": "rewrite_spec_data_root", "new_data_root": "."}],
                            "decision_reason": "old data root can be redirected to the current warehouse",
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    "provider_id": "fake",
                    "response_hash": "hash",
                },
            )()

    monkeypatch.setattr(validate_data_quality, "RegisteredProviderAdapter", FakeAdapter)

    decision = validate_data_quality.judge_summary(summary)

    assert decision["status"] == "repair_candidate"
    assert decision["decision_source"] == "ai_data_quality_judge"
    assert {item["action"] for item in decision["fix_plan"]} == {"rewrite_spec_data_root"}


def test_data_root_repair_updates_spec_without_base_ranks(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_data_quality, "REPO_ROOT", tmp_path)
    run_dir = tmp_path / "data" / "run_a"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    decision_path = run_dir / "data_quality_decision.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_a",
                "automation": {
                    "command": ["python3", "scripts/evaluate.py", "--data-root", "data/old_run"],
                    "sync_paths": [],
                },
            }
        ),
        encoding="utf-8",
    )
    decision_path.write_text(
        yaml.safe_dump(
            {
                "status": "repair_candidate",
                "fix_plan": [{"action": "rewrite_spec_data_root", "new_data_root": "."}],
            }
        ),
        encoding="utf-8",
    )

    report = repair_data_quality.repair(spec_path, decision_path)
    updated = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert report["status"] == "prepared"
    assert report["mode"] == "deterministic_spec_path_rewrite"
    assert updated["automation"]["command"][-1] == "."
    assert "data/cb_warehouse/cb_daily.parquet" in updated["automation"]["sync_paths"]


def test_data_quality_ai_judge_can_request_derivable_recommended_columns(monkeypatch):
    summary = {
        "schema_version": 1,
        "data_files": [
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "exists": True,
                "readable": True,
                "rows": 10,
                "missing_required_columns": [],
                "missing_recommended_columns": ["pct_chg", "cb_over_rate"],
            },
            {
                "path": "data/cb_warehouse/cb_call.parquet",
                "exists": True,
                "readable": True,
                "rows": 10,
                "missing_required_columns": [],
                "missing_recommended_columns": ["call_type"],
            },
        ],
    }

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def call_active_provider(self, prompt, schema):
            assert "pct_chg" in prompt
            return type(
                "Resp",
                (),
                {
                    "content": yaml.safe_dump(
                        {
                            "status_code": 2,
                            "confidence_code": 1,
                            "blocking_issues": [],
                            "warnings": ["recommended derived fields are missing"],
                            "fix_plan": [
                                {"action": "derive_warehouse_columns", "path": "data/cb_warehouse/cb_daily.parquet"}
                            ],
                            "decision_reason": "missing derived fields can be prepared for this run",
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    "provider_id": "fake",
                    "response_hash": "hash",
                },
            )()

    monkeypatch.setattr(validate_data_quality, "RegisteredProviderAdapter", FakeAdapter)

    decision = validate_data_quality.judge_summary(summary)

    assert decision["status"] == "repair_candidate"
    assert decision["decision_source"] == "ai_data_quality_judge"
    assert {item["action"] for item in decision["fix_plan"]} == {"derive_warehouse_columns"}


def test_executor_declares_all_value_gap_switch_inputs(tmp_path):
    run_dir = tmp_path / "data" / "run_requirements"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_requirements",
                "automation": {
                    "command": [
                        "python3",
                        "scripts/evaluate_cb_arb_value_gap_switch.py",
                        "--data-root",
                        "data/run_requirements/prepared_data/data_root",
                        "--fixed-source",
                        "2",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    requirements = executor_requirements.declared_requirements_for_spec(spec_path)
    paths = {item["path"] for item in requirements["required_files"]}

    assert "data/run_requirements/prepared_data/data_root/pool_0/best_params.json" in paths
    assert "data/run_requirements/prepared_data/data_root/pool_2/best_params.json" in paths
    assert "data/run_requirements/prepared_data/data_root/pool_4/best_params.json" in paths
    assert "data/run_requirements/prepared_data/data_root/pool_6/best_params.json" in paths


def test_data_quality_ai_judge_handles_executor_without_declared_inputs(tmp_path, monkeypatch):
    script = tmp_path / "scripts" / "dummy_executor.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('no declaration')\n", encoding="utf-8")
    spec_path = tmp_path / "data" / "run_missing_decl" / "spec.yaml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_missing_decl",
                "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
            }
        ),
        encoding="utf-8",
    )
    original_root = executor_requirements.REPO_ROOT
    executor_requirements.REPO_ROOT = tmp_path
    try:
        summary = validate_data_quality.summarize_data_quality(spec_path)
    finally:
        executor_requirements.REPO_ROOT = original_root

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def call_active_provider(self, prompt, schema):
            assert "executor_requirements_error" in prompt
            return type(
                "Resp",
                (),
                {
                    "content": yaml.safe_dump(
                        {
                            "status_code": 3,
                            "confidence_code": 1,
                            "blocking_issues": ["executor data requirements unavailable"],
                            "warnings": [],
                            "fix_plan": [],
                            "decision_reason": "the run cannot prove which data it needs",
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    "provider_id": "fake",
                    "response_hash": "hash",
                },
            )()

    monkeypatch.setattr(validate_data_quality, "RegisteredProviderAdapter", FakeAdapter)

    decision = validate_data_quality.judge_summary(summary)

    assert decision["status"] == "fail"
    assert decision["decision_source"] == "ai_data_quality_judge"
    assert "executor data requirements unavailable" in decision["blocking_issues"][0]


def test_warehouse_column_repair_writes_run_local_data(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_data_quality, "REPO_ROOT", tmp_path)
    warehouse = tmp_path / "data" / "cb_warehouse"
    warehouse.mkdir(parents=True)
    for pool_id in (0, 2, 4, 6):
        pool = tmp_path / "data" / "cb_arb_concurrent_supervised_20260511_094500" / f"pool_{pool_id}"
        pool.mkdir(parents=True)
        (pool / "best_params.json").write_text('{"params": {}}\n', encoding="utf-8")
    pd.DataFrame(
        [{"ts_code": "113001.SH", "stk_code": "600001.SH", "conv_price": 10.0}]
    ).to_parquet(warehouse / "cb_basic.parquet", index=False)
    pd.DataFrame(
        [
            {"ts_code": "113001.SH", "trade_date": "20200101", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "vol": 10},
            {"ts_code": "113001.SH", "trade_date": "20200102", "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0, "vol": 20},
        ]
    ).to_parquet(warehouse / "cb_daily.parquet", index=False)
    pd.DataFrame(
        [{"ts_code": "113001.SH", "ann_date": "20200101", "call_date": "20200102", "is_call": "公告实施强赎"}]
    ).to_parquet(warehouse / "cb_call.parquet", index=False)
    pd.DataFrame(
        [
            {"stk_code": "600001.SH", "trade_date": "20200101", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0},
            {"stk_code": "600001.SH", "trade_date": "20200102", "open": 11.0, "high": 12.0, "low": 10.0, "close": 11.0},
        ]
    ).to_parquet(warehouse / "stk_daily_qfq.parquet", index=False)

    run_dir = tmp_path / "data" / "run_b"
    run_dir.mkdir()
    spec_path = run_dir / "spec.yaml"
    decision_path = run_dir / "data_quality_decision.yaml"
    spec_path.write_text(
        yaml.safe_dump(
                {
                    "run_id": "run_b",
                    "automation": {
                        "command": [
                            "python3",
                            "scripts/evaluate_cb_arb_value_gap_switch.py",
                            "--data-root",
                            ".",
                            "--fixed-source",
                            "2",
                        ],
                        "sync_paths": [],
                    },
                }
        ),
        encoding="utf-8",
    )
    decision_path.write_text(
        yaml.safe_dump(
            {
                "status": "repair_candidate",
                "fix_plan": [{"action": "derive_warehouse_columns", "path": "data/cb_warehouse/cb_daily.parquet"}],
            }
        ),
        encoding="utf-8",
    )

    report = repair_data_quality.repair(spec_path, decision_path)
    updated = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    command = updated["automation"]["command"]
    data_root = command[command.index("--data-root") + 1]
    prepared_root = tmp_path / data_root
    repaired_daily = pd.read_parquet(prepared_root / "data" / "cb_warehouse" / "cb_daily.parquet")
    repaired_call = pd.read_parquet(prepared_root / "data" / "cb_warehouse" / "cb_call.parquet")

    assert report["mode"] == "deterministic_warehouse_column_derivation"
    assert {"pct_chg", "cb_over_rate"} <= set(repaired_daily.columns)
    assert "call_type" in repaired_call.columns
    for pool_id in (0, 2, 4, 6):
        assert (prepared_root / f"pool_{pool_id}" / "best_params.json").exists()
    assert data_root.endswith("prepared_data/data_root")


def test_repaired_data_item_requeues_and_sets_spec_ready(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_c"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_c",
                "status": "ARCHIVED",
                "data_quality_repair": {"status": "prepared"},
            }
        ),
        encoding="utf-8",
    )
    state = {
        "queue": [
            {
                "id": "run_c",
                "status": "failed",
                "spec_path": "data/run_c/spec.yaml",
                "failure_reason": "data quality blocked before repair",
            }
        ],
        "history": [],
    }

    count = service.requeue_repaired_data_items(state, state["queue"])
    updated = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert count == 1
    assert state["queue"][0]["status"] == "queued"
    assert state["queue"][0]["data_quality_repair_rerun"] is True
    assert updated["status"] == "READY"


def test_repaired_data_rerun_can_reset_stale_complete_spec(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_c"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_c",
                "status": "COMPLETE",
                "data_quality_repair": {"status": "prepared"},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "vm_pipeline_stdout.log").write_text("exit_code: 1\n", encoding="utf-8")
    state = {
        "queue": [
            {
                "id": "run_c",
                "status": "failed",
                "spec_path": "data/run_c/spec.yaml",
                "data_quality_repair_rerun": True,
                "failure_reason": "remote pipeline exit_code=1",
            }
        ],
        "history": [],
    }

    count = service.requeue_repaired_data_items(state, state["queue"])
    updated = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert count == 1
    assert state["queue"][0]["status"] == "queued"
    assert updated["status"] == "READY"
    assert state["queue"][0]["previous_spec_status_before_data_requeue"] == "COMPLETE"


def test_repaired_data_rerun_stops_after_two_attempts(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_c"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_c",
                "status": "COMPLETE",
                "data_quality_repair": {"status": "prepared"},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "vm_pipeline_stdout.log").write_text("exit_code: 1\n", encoding="utf-8")
    state = {
        "queue": [
            {
                "id": "run_c",
                "status": "failed",
                "spec_path": "data/run_c/spec.yaml",
                "data_quality_repair_rerun": True,
                "failure_reason": "remote pipeline exit_code=1",
                "data_quality_repair_signature": service.data_quality_repair_signature(
                    spec_path, {"status": "prepared"}
                ),
                "data_quality_repair_requeue_attempts": 1,
            }
        ],
        "history": [
            {"id": "run_c", "message": "data quality repair prepared run-local data; AI data judge will recheck before execution"},
            {"id": "run_c", "message": "data quality repair prepared run-local data; AI data judge will recheck before execution"},
        ],
    }

    count = service.requeue_repaired_data_items(state, state["queue"])

    assert count == 1
    assert state["queue"][0]["status"] == "failed"
    assert state["queue"][0]["failure_reason"].startswith("data repair rerun failed after prepared repair")


def test_data_quality_block_records_signature(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_d"
    run_dir.mkdir(parents=True)
    script = tmp_path / "scripts" / "dummy_executor.py"
    script.parent.mkdir(parents=True)
    script.write_text("def main():\n    return 0\n", encoding="utf-8")
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_d",
                "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
            }
        ),
        encoding="utf-8",
    )
    state = {
        "queue": [
            {
                "id": "run_d",
                "status": "blocked",
                "spec_path": "data/run_d/spec.yaml",
                "block_reason": "data_quality_blocked: missing field",
            }
        ],
        "history": [],
    }

    count = service.requeue_stale_data_quality_blocks(state, state["queue"])

    assert count == 1
    assert state["queue"][0]["status"] == "blocked"
    assert state["queue"][0]["data_quality_block_signature"]


def test_data_quality_block_requeues_when_executor_inputs_change(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_d"
    run_dir.mkdir(parents=True)
    script = tmp_path / "scripts" / "dummy_executor.py"
    script.parent.mkdir(parents=True)
    script.write_text("def main():\n    return 0\n", encoding="utf-8")
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_d",
                "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
            }
        ),
        encoding="utf-8",
    )
    state = {
        "queue": [
            {
                "id": "run_d",
                "status": "blocked",
                "spec_path": "data/run_d/spec.yaml",
                "block_reason": "data_quality_blocked: missing field",
                "data_quality_block_signature": service.data_quality_block_signature(spec_path),
            }
        ],
        "history": [],
    }
    script.write_text("def main():\n    return 1\n", encoding="utf-8")

    count = service.requeue_stale_data_quality_blocks(state, state["queue"])

    assert count == 1
    assert state["queue"][0]["status"] == "queued"
    assert state["queue"][0]["requeue_reason"] == "data-quality inputs changed; re-run data-quality gate automatically"


def test_post_run_gatekeeper_processes_only_current_run(tmp_path, monkeypatch):
    calls = []
    gate = GateKeeper(repo_root=tmp_path)

    def fake_must_run(script, args, fail_msg):
        calls.append((script, args, fail_msg))

    monkeypatch.setattr(gate, "_must_run", fake_must_run)

    gate.after_run_grid(tmp_path / "data" / "run_e")

    assert ("auto_compute_l4_data.py", ["--run-dir", str(tmp_path / "data" / "run_e")], "L4 数据自动算失败, 检查 ranked.csv / trades.csv") in calls


def test_pipeline_infra_failure_requeues_after_post_run_gate_failure(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_e"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_e",
                "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "vm_pipeline_stdout.log").write_text(
        "auto_research_pipeline.py: FAIL: post-run GateKeeper check failed\n",
        encoding="utf-8",
    )
    state = {
        "queue": [
                {
                    "id": "run_e",
                    "status": "failed",
                    "spec_path": "data/run_e/spec.yaml",
                    "failed_at": "2026-05-21T00:00:00",
                    "failure_reason": "remote process exited but required artifacts are missing",
                }
        ],
        "history": [],
    }

    count = service.requeue_stale_pipeline_failures(state, state["queue"])
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert count == 1
    assert state["queue"][0]["status"] == "queued"
    assert state["queue"][0]["pipeline_failure_requeue_attempts"] == 1
    assert spec["status"] == "READY"


def test_old_pipeline_infra_failure_is_not_requeued_without_prior_signature(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_old"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump({"run_id": "run_old", "automation": {"command": ["python3", "scripts/dummy_executor.py"]}}),
        encoding="utf-8",
    )
    (run_dir / "vm_pipeline_stdout.log").write_text(
        "auto_research_pipeline.py: FAIL: post-run GateKeeper check failed\n",
        encoding="utf-8",
    )
    state = {
        "queue": [
            {
                "id": "run_old",
                "status": "failed",
                "spec_path": "data/run_old/spec.yaml",
                "failed_at": "2026-05-20T00:00:00",
                "failure_reason": "remote process exited but required artifacts are missing",
            }
        ],
        "history": [],
    }

    count = service.requeue_stale_pipeline_failures(state, state["queue"])

    assert count == 1
    assert state["queue"][0]["status"] == "failed"
    assert state["queue"][0]["pipeline_failure_signature"]


def test_requeue_skips_task_tagged_infrastructure_failure(tmp_path):
    """Mandate 2026-05-26: requeue must not pull back tasks whose
    failure_category is 'infrastructure'. The fact that some tracked
    framework file changed SHA is not evidence the underlying executor
    defect has been fixed."""
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_infra"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump({
            "run_id": "run_infra",
            "status": "ARCHIVED",
            "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
        }),
        encoding="utf-8",
    )
    state = {
        "queue": [
            {
                "id": "run_infra",
                "status": "failed",
                "spec_path": "data/run_infra/spec.yaml",
                "failed_at": "2026-05-21T00:00:00",
                "failure_reason": "remote pipeline exit_code=1",
                "failure_category": "infrastructure",
            }
        ],
        "history": [],
    }
    count = service.requeue_stale_pipeline_failures(state, state["queue"])
    assert count == 0
    assert state["queue"][0]["status"] == "failed"


def test_requeue_skips_task_with_infra_failure_type(tmp_path):
    """Mandate 2026-05-26: requeue must not pull back tasks tagged with
    an infra_failure_type (e.g. path_unreachable_module_not_found)."""
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_pathbug"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump({
            "run_id": "run_pathbug",
            "status": "ARCHIVED",
            "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
        }),
        encoding="utf-8",
    )
    state = {
        "queue": [
            {
                "id": "run_pathbug",
                "status": "failed",
                "spec_path": "data/run_pathbug/spec.yaml",
                "failed_at": "2026-05-21T00:00:00",
                "failure_reason": "remote pipeline exit_code=1",
                "infra_failure_type": "path_unreachable_module_not_found",
            }
        ],
        "history": [],
    }
    count = service.requeue_stale_pipeline_failures(state, state["queue"])
    assert count == 0
    assert state["queue"][0]["status"] == "failed"


def test_requeue_skips_task_older_than_freshness_window(tmp_path):
    """Mandate 2026-05-26: age cutoff. A failed task older than
    REQUEUE_FRESHNESS_HOURS must not be auto-resurrected even if
    framework file SHAs changed and no explicit infra tag is set."""
    service = _remote_service(tmp_path)  # mocks now_iso to 2026-05-21T00:00:00
    run_dir = tmp_path / "data" / "run_old"
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump({
            "run_id": "run_old",
            "status": "ARCHIVED",
            "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
        }),
        encoding="utf-8",
    )
    # failed 5 days before "now" — well past the 24h freshness window
    state = {
        "queue": [
            {
                "id": "run_old",
                "status": "failed",
                "spec_path": "data/run_old/spec.yaml",
                "failed_at": "2026-05-16T00:00:00",
                "failure_reason": "remote pipeline exit_code=1",
            }
        ],
        "history": [],
    }
    count = service.requeue_stale_pipeline_failures(state, state["queue"])
    assert count == 0
    assert state["queue"][0]["status"] == "failed"


def test_execution_failure_requeues_when_executor_code_changed(tmp_path):
    service = _remote_service(tmp_path)
    run_dir = tmp_path / "data" / "run_exec"
    run_dir.mkdir(parents=True)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    executor = scripts_dir / "dummy_executor.py"
    executor.write_text("print('old')\n", encoding="utf-8")
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "run_id": "run_exec",
                "status": "ARCHIVED",
                "automation": {"command": ["python3", "scripts/dummy_executor.py"]},
            }
        ),
        encoding="utf-8",
    )
    previous_sig = service.pipeline_failure_signature(spec_path)
    executor.write_text("print('fixed')\n", encoding="utf-8")
    (run_dir / "vm_pipeline_stdout.log").write_text("exit_code: 1\n", encoding="utf-8")
    state = {
        "queue": [
            {
                "id": "run_exec",
                "status": "failed",
                "spec_path": "data/run_exec/spec.yaml",
                "failed_at": "2026-05-22T00:00:00",
                "failure_reason": "remote pipeline exit_code=1",
                "pipeline_failure_signature": previous_sig,
            }
        ],
        "history": [],
    }

    count = service.requeue_stale_pipeline_failures(state, state["queue"])
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert count == 1
    assert state["queue"][0]["status"] == "queued"
    assert state["queue"][0]["pipeline_failure_requeue_attempts"] == 1
    assert spec["status"] == "READY"


def test_runner_detects_failed_pipeline_even_when_old_artifacts_exist(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "vm_pipeline_stdout.log").write_text("exit_code: 1\nmissing_artifacts: []\n", encoding="utf-8")
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")

    assert pipeline_execution_failed(run_dir) == "remote pipeline exit_code=1"


def test_runner_clears_stale_vm_avoidance(tmp_path):
    service = _remote_service(tmp_path)
    state = {
        "queue": [
            {
                "id": "run_d",
                "status": "queued",
                "avoid_vm_ids": ["singapore_sig", "guangzhou_spot"],
                "last_start_error_at": "2026-05-20T06:00:48",
            }
        ],
        "history": [],
    }

    count = service.clear_stale_vm_avoidances(state, state["queue"])

    assert count == 1
    assert "avoid_vm_ids" not in state["queue"][0]
    assert state["queue"][0]["workflow_stage"] == "queued_after_stale_vm_avoidance_reset"


def test_derive_verdict_from_run_summary(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "run_summary.json").write_text(
        json.dumps({"adoption_pass": False, "selected_passes": 3, "selected_total": 6}),
        encoding="utf-8",
    )

    verdict = pipeline.derive_verdict({}, out, 0, [])

    assert verdict["status"] == "rejected"
    assert verdict["decision"] == "failed_mechanical_thresholds"
    assert verdict["pass_value"] is False


def test_derive_verdict_from_configured_csv_table(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "summary_stop_revaluation.csv").write_text(
        "\n".join(
            [
                "name,period,excess_return,max_drawdown,score",
                "bad,test,-0.01,-0.20,0.1",
                "good,test,0.04,-0.18,0.9",
                "train_only,train,0.20,-0.10,1.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec = {
        "automation": {
            "verdict": {
                "table_path": "summary_stop_revaluation.csv",
                "filters": {"period": "test"},
                "rank_by": "score",
                "rank_desc": True,
                "thresholds": {
                    "excess_return": {"min": 0.0},
                    "max_drawdown": {"min": -0.30},
                },
            }
        }
    }

    verdict = pipeline.derive_verdict(spec, out, 0, [])

    assert verdict["status"] == "wip"
    assert verdict["pass_value"] is True
    assert verdict["summary"]["selected_table_row"]["name"] == "good"


def test_update_experiments_upserts(monkeypatch, tmp_path):
    experiments = tmp_path / "experiments.yaml"
    experiments.write_text(
        yaml.safe_dump({"schema_version": 1, "experiments": []}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "EXPERIMENTS", experiments)
    monkeypatch.setattr(pipeline, "REPO_ROOT", tmp_path)

    spec = {"run_id": "r1", "strategy_id": "cb_arb", "hypothesis": "test hypothesis"}
    out = tmp_path / "data" / "r1"
    manifest = tmp_path / "data" / "research_framework" / "run_manifests" / "r1.yaml"
    verdict = {"status": "rejected", "decision": "failed", "summary": {"adoption_pass": False}}
    compute = {"estimated_compute_cost_yuan": 1, "decision": "record-only"}

    pipeline.update_experiments(spec, out, manifest, verdict, compute, dry_run=False)
    pipeline.update_experiments(spec, out, manifest, verdict, compute, dry_run=False)

    data = yaml.safe_load(experiments.read_text(encoding="utf-8"))
    rows = [row for row in data["experiments"] if row["id"] == "r1"]
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"


# ---------------------------------------------------------------------------
# DRAFT-pending-capability suppression bug fix (2026-05-24)
# ---------------------------------------------------------------------------


def _ideation_service_with_audit(tmp_path):
    """Ideation service variant that captures audit events for assertions."""
    audits: list[tuple[str, dict | None]] = []
    statuses: list[tuple[str, dict | None]] = []
    state_holder = {"state": {"enabled": True, "queue": []}}
    service = QueueIdeationService(
        repo_root=tmp_path,
        load_state=lambda: state_holder["state"],
        save_state=lambda new_state: state_holder.update(state=new_state),
        write_status=lambda status, extra=None: statuses.append((status, extra)),
        audit=lambda action, payload=None: audits.append((action, payload)),
        log=lambda message: None,
        mark_history=lambda state, item, status, message: None,
        rel=lambda path: str(path.resolve().relative_to(tmp_path)) if path.resolve().is_relative_to(tmp_path) else str(path),
        now_iso=lambda: "2026-05-24T15:30:00",
        ideation_env=lambda: {},
        timeout_seconds=1,
    )
    return service, audits, statuses, state_holder


def _make_pending_capability_run(tmp_path, *, run_id="reverse_bond_floor_v1",
                                  package_status="awaiting_hermes_executor_code"):
    """Build a run dir mirroring what ideation_cycle produces when a DRAFT
    proposal is generated with missing_capability_request."""
    run_dir = tmp_path / "data" / run_id
    run_dir.mkdir(parents=True)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "status": "DRAFT",
                "notes": "missing registered capability",
                "proposal": {"proposal_id": run_id},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    request_path = run_dir / "executor_tool_request.yaml"
    request_path.write_text(
        yaml.safe_dump(
            {"status": package_status, "tool_request_id": f"{run_id}_executor_v1"},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return run_dir, spec_path, request_path


def test_draft_with_missing_capability_request_accepted_not_suppressed(tmp_path, monkeypatch):
    """Test 1: DRAFT + missing_capability_request → accepted_pending_capability,
    NOT suppressed. spec.yaml.automation_skip stays unset; executor_tool_request.yaml.status
    stays in awaiting_hermes_executor_code so find_pending_tool_draft can pick it up."""
    run_dir, spec_path, request_path = _make_pending_capability_run(tmp_path)
    service, audits, statuses, _ = _ideation_service_with_audit(tmp_path)
    payload = {
        "status": "DRAFT",
        "reason": "missing registered capability",
        "proposal_id": "reverse_bond_floor_v1",
        "spec_path": str(spec_path),
        "executor_tool_package": {"status": "awaiting_hermes_executor_code"},
    }
    monkeypatch.setattr(
        service,
        "generate_next_spec_if_idle",
        lambda state: (lambda: service._accept_pending_capability(payload))(),
    )
    result = service.generate_next_spec_if_idle({"queue": []})
    assert result == "ideation_accepted_pending_capability"

    # Suppression markers must NOT have been written.
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert "automation_skip" not in spec, (
        "DRAFT with valid pending executor_tool_package must not be tagged "
        "as automation_skip=skipped_non_runnable"
    )
    request = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    assert request["status"] == "awaiting_hermes_executor_code", (
        "executor_tool_request.yaml.status must stay in its pending-capability "
        "state so find_pending_tool_draft picks it up"
    )
    actions = {action for action, _ in audits}
    assert "ideation_accepted_pending_capability" in actions
    assert ("ideation_accepted_pending_capability", ) == tuple(s for s, _ in statuses)[-1:]


def test_invalid_draft_still_suppressed(tmp_path):
    """Test 2 (the critical regression guard): DRAFT WITHOUT a valid pending
    executor_tool_package is genuine non-runnable garbage and MUST still be
    suppressed. Otherwise the bug fix would also let malformed proposals through."""
    run_dir, spec_path, _ = _make_pending_capability_run(
        tmp_path,
        run_id="garbage_draft",
        package_status="invalid_tool_code_response",  # terminal failure
    )
    service, audits, statuses, _ = _ideation_service_with_audit(tmp_path)
    payload_no_package = {
        "status": "DRAFT",
        "reason": "proposal schema invalid",
        "proposal_id": "garbage_draft",
        "spec_path": str(spec_path),
        # no executor_tool_package at all
    }
    service.suppress_non_runnable_draft(payload_no_package, "DRAFT")
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert spec.get("automation_skip", {}).get("status") == "skipped_non_runnable"
    suppressed = [a for a, _ in audits if a == "ideation_non_runnable_suppressed"]
    assert suppressed, "non-runnable DRAFT without pending package must still be suppressed"


def test_runnable_ready_proposal_path_unchanged(tmp_path):
    """Test 3: READY proposal still goes through enqueue_ready_spec unchanged.
    The bug fix only touches the DRAFT branch."""
    from framework.autonomous.queue_ideation import _is_draft_pending_capability

    ready_payload = {"status": "READY", "spec_path": "data/r1/spec.yaml"}
    # READY proposals never trigger the pending-capability branch.
    assert _is_draft_pending_capability(ready_payload) is False


def test_repeated_draft_dedupes_to_noop(tmp_path):
    """Test 4: same proposal_id offered twice → second call emits
    ideation_pending_capability_noop, not a second fresh accept event."""
    run_dir, spec_path, _ = _make_pending_capability_run(
        tmp_path, run_id="duplicate_draft"
    )
    service, audits, statuses, _ = _ideation_service_with_audit(tmp_path)
    payload = {
        "status": "DRAFT",
        "reason": "missing registered capability",
        "proposal_id": "duplicate_draft",
        "spec_path": str(spec_path),
        "executor_tool_package": {"status": "awaiting_hermes_executor_code"},
    }
    # First call: fresh accept.
    first = service._accept_pending_capability(payload)
    # Second call: should be detected as already accepted and noop.
    second = service._accept_pending_capability(payload)
    assert first == "ideation_accepted_pending_capability"
    assert second == "ideation_accepted_pending_capability"
    actions = [a for a, _ in audits]
    assert actions.count("ideation_accepted_pending_capability") == 1
    assert actions.count("ideation_pending_capability_noop") == 1


def test_draft_with_terminal_package_status_is_suppressed(tmp_path):
    """Test 5: DRAFT + package with terminal failure status (e.g.
    invalid_tool_code_response) is NOT pending-capability — it is a failed
    attempt and should still be suppressed."""
    from framework.autonomous.queue_ideation import _is_draft_pending_capability

    payload = {
        "status": "DRAFT",
        "executor_tool_package": {"status": "invalid_tool_code_response"},
    }
    assert _is_draft_pending_capability(payload) is False
