from __future__ import annotations

import yaml

from scripts import auto_research_pipeline as pipeline


def _spec(summary_name: str = "summary.csv", yearly_name: str = "yearly.csv") -> dict:
    return {
        "run_id": "train_falsifier_test",
        "strategy_id": "cb_arb_value_gap_switch",
        "hypothesis": "train falsifier test",
        "cv_holdout_years": [2025, 2026],
        "artifacts_required": [summary_name, yearly_name],
        "automation": {
            "verdict": {
                "table_path": summary_name,
                "yearly_path": yearly_name,
                "filters": {"period": "test"},
                "rank_by": "score",
                "rank_desc": True,
                "thresholds": {
                    "excess_return": {"min": 0.0},
                    "max_drawdown": {"min": -0.30},
                },
            }
        },
    }


def _write_summary(out, *, excess: float = 0.20, dd: float = -0.10) -> None:
    (out / "summary.csv").write_text(
        "\n".join(
            [
                "name,period,excess_return,max_drawdown,score",
                f"selected,test,{excess},{dd},1.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_yearly(out, rows: list[tuple[int, float, float]]) -> None:
    lines = ["name,period,excess_return,max_drawdown,score"]
    for year, excess, dd in rows:
        lines.append(f"selected,{year},{excess},{dd},0.0")
    (out / "yearly.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_mechanical_pass_cross_year_falsifier_fails(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    _write_summary(out)
    _write_yearly(out, [(2019, -0.10, -0.05), (2020, -0.10, -0.06)])

    verdict = pipeline.derive_verdict(_spec(), out, 0, [])

    assert verdict["status"] == "rejected"
    assert verdict["decision"] == "passed_mechanical_but_falsifier_failed"
    assert verdict["falsifier_flags"]["falsifier_train_excess"]["status"] == "failed"


def test_mechanical_pass_single_year_drawdown_falsifier_fails(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    _write_summary(out)
    _write_yearly(out, [(2019, 0.20, -0.10), (2020, 0.10, -0.20)])

    verdict = pipeline.derive_verdict(_spec(), out, 0, [])

    assert verdict["status"] == "rejected"
    assert verdict["decision"] == "passed_mechanical_but_falsifier_failed"
    assert verdict["falsifier_flags"]["falsifier_single_year_dd"]["status"] == "failed"
    assert verdict["falsifier_flags"]["falsifier_single_year_dd"]["worst_year"] == "2020"


def test_mechanical_pass_all_train_falsifiers_pass(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    _write_summary(out)
    _write_yearly(out, [(2019, 0.20, -0.10), (2020, 0.10, -0.12)])

    verdict = pipeline.derive_verdict(_spec(), out, 0, [])

    assert verdict["status"] == "wip"
    assert verdict["decision"] == "passed_mechanical_thresholds_not_promoted"
    assert verdict["pass_value"] is True
    assert verdict["falsifier_flags"]["falsifier_train_excess"]["status"] == "passed"
    assert verdict["falsifier_flags"]["falsifier_single_year_dd"]["status"] == "passed"


def test_mechanical_fail_keeps_existing_decision(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    _write_summary(out, excess=-0.01, dd=-0.10)
    _write_yearly(out, [(2019, -0.20, -0.25)])

    verdict = pipeline.derive_verdict(_spec(), out, 0, [])

    assert verdict["status"] == "rejected"
    assert verdict["decision"] == "failed_mechanical_thresholds"
    assert verdict["pass_value"] is False


def test_missing_yearly_csv_skips_falsifiers_without_blocking(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    _write_summary(out)

    verdict = pipeline.derive_verdict(_spec(), out, 0, [])

    assert verdict["status"] == "wip"
    assert verdict["decision"] == "passed_mechanical_thresholds_not_promoted"
    assert verdict["falsifier_flags"] == {}
    assert "yearly_csv_missing_skip_train_falsifiers" in verdict["summary"]["falsifier_warnings"]


def test_update_experiments_carries_falsifier_flags_in_key_metrics(monkeypatch, tmp_path):
    experiments = tmp_path / "experiments.yaml"
    experiments.write_text(
        yaml.safe_dump({"schema_version": 1, "experiments": []}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "EXPERIMENTS", experiments)
    monkeypatch.setattr(pipeline, "REPO_ROOT", tmp_path)

    flags = {"falsifier_train_excess": {"status": "failed"}}
    verdict = {
        "status": "rejected",
        "decision": "passed_mechanical_but_falsifier_failed",
        "summary": {"adoption_pass": True, "falsifier_flags": flags},
    }

    pipeline.update_experiments(
        {"run_id": "train_falsifier_test", "strategy_id": "cb_arb", "hypothesis": "x"},
        tmp_path / "out",
        tmp_path / "manifest.yaml",
        verdict,
        {"decision": "auto-approve", "estimated_budget_yuan": 0},
        dry_run=False,
    )

    data = yaml.safe_load(experiments.read_text(encoding="utf-8"))
    row = data["experiments"][0]
    assert row["automation"]["decision"] == "passed_mechanical_but_falsifier_failed"
    assert row["key_metrics"]["falsifier_flags"] == flags
