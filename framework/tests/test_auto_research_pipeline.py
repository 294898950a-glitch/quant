from __future__ import annotations

import json
import yaml
import pytest
import pandas as pd

from scripts import auto_research_pipeline as pipeline
from scripts import repair_data_quality
from scripts import validate_data_quality
from framework.autonomous import run_recorder


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
    assert "repair_candidate" in source
    assert "status: pass 或 repair_candidate 或 fail" in source


def test_data_repairer_is_separate_and_revalidated():
    runner = pipeline.Path("scripts/research_queue_runner.py").read_text(encoding="utf-8")
    repairer = pipeline.Path("scripts/repair_data_quality.py").read_text(encoding="utf-8")
    assert "scripts/repair_data_quality.py" in runner
    assert 'issue_ticket("data_quality_repair")' in runner
    assert "repaired_summary = remote_data_quality_summary" in runner
    assert 'require_ticket("data_quality_repair")' in repairer
    assert "status=repair_candidate" in repairer or "repair_candidate" in repairer
    assert "RegisteredProviderAdapter" in repairer
    assert "generated_repair.py" in repairer
    assert "original data was not overwritten" in repairer


def test_deterministic_data_gate_repairs_old_warehouse_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(validate_data_quality, "REPO_ROOT", tmp_path)
    for rel_path in validate_data_quality.WAREHOUSE_REL_PATHS:
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder", encoding="utf-8")
    summary = {
        "schema_version": 1,
        "data_files": [
            {
                "path": f"data/old_run/{rel_path}",
                "exists": False,
                "readable": False,
            }
            for rel_path in validate_data_quality.WAREHOUSE_REL_PATHS
        ],
    }

    decision = validate_data_quality.deterministic_decision(summary)

    assert decision["status"] == "repair_candidate"
    assert decision["decision_source"] == "deterministic_data_gate"
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


def test_deterministic_data_gate_repairs_derivable_recommended_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(validate_data_quality, "REPO_ROOT", tmp_path)
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

    decision = validate_data_quality.deterministic_decision(summary)

    assert decision["status"] == "repair_candidate"
    assert {item["action"] for item in decision["fix_plan"]} == {"derive_warehouse_columns"}


def test_warehouse_column_repair_writes_run_local_data(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_data_quality, "REPO_ROOT", tmp_path)
    warehouse = tmp_path / "data" / "cb_warehouse"
    warehouse.mkdir(parents=True)
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
                    "command": ["python3", "scripts/evaluate.py", "--data-root", "."],
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
    prepared_root = tmp_path / updated["automation"]["command"][-1]
    repaired_daily = pd.read_parquet(prepared_root / "data" / "cb_warehouse" / "cb_daily.parquet")
    repaired_call = pd.read_parquet(prepared_root / "data" / "cb_warehouse" / "cb_call.parquet")

    assert report["mode"] == "deterministic_warehouse_column_derivation"
    assert {"pct_chg", "cb_over_rate"} <= set(repaired_daily.columns)
    assert "call_type" in repaired_call.columns
    assert updated["automation"]["command"][-1].endswith("prepared_data/data_root")


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
