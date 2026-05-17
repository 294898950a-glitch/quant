from __future__ import annotations

import json
import yaml

from scripts import auto_research_pipeline as pipeline


def test_estimate_budget_uses_config_limit():
    spec = {
        "compute_estimate": {
            "sig_minutes": 0,
            "spot_minutes": 60,
            "estimated_cost_yuan": 10,
        },
        "budget_cap_yuan": 100,
    }

    result = pipeline.estimate_budget(spec)

    assert result["estimated_budget_yuan"] == 18.75
    assert result["decision"] == "auto-approve"


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
    budget = {"estimated_budget_yuan": 1, "decision": "auto-approve"}

    pipeline.update_experiments(spec, out, manifest, verdict, budget, dry_run=False)
    pipeline.update_experiments(spec, out, manifest, verdict, budget, dry_run=False)

    data = yaml.safe_load(experiments.read_text(encoding="utf-8"))
    rows = [row for row in data["experiments"] if row["id"] == "r1"]
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
