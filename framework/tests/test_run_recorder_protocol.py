from __future__ import annotations

import json

from framework.autonomous import run_recorder
from scripts import auto_research_pipeline


def test_run_recorder_enforces_declared_protocol(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "summary.json").write_text(json.dumps({
        "best_test_excess": 0.1,
        "best_test_sharpe": None,
        "best_test_total_trades": 200,
        "bottom_momentum_trade_count": 12,
        "costs": {"friction_total": 1.0},
    }), encoding="utf-8")
    spec = {
        "evaluation_protocol": {
            "schema_version": 1, "mode": "cb_momentum_protocol_v2",
            "primary_metric": {"path": "best_test_excess", "direction": "maximize"},
            "splits": {"holdout_periods": [2025]},
            "cost_model": {"required": True, "metric_path": "costs.friction_total"},
            "constraints": [
                {"metric": "best_test_sharpe", "op": "gt", "value": 0.0},
                {"metric": "bottom_momentum_trade_count", "op": "gte", "value": 30},
            ], "fail_closed": True,
        }
    }
    verdict = run_recorder.derive_verdict(spec, out, 0, [])
    pipeline_verdict = auto_research_pipeline.derive_verdict(spec, out, 0, [])
    assert verdict["decision"] == "evaluation_evidence_incomplete"
    assert pipeline_verdict["decision"] == verdict["decision"]
    assert verdict["pass_field"] == "best_test_excess"
    assert verdict["protocol_result"]["protocol_checks"]["constraint_0"]["reason"] == "missing"
