from __future__ import annotations

from framework.autonomous.evaluation_protocol import (
    CB_ARB_COMPAT_PROTOCOL,
    evaluate_constraints,
    protocol_for_proposal,
    validate_protocol,
)
import yaml


def explicit_protocol() -> dict:
    return {
        "schema_version": 1,
        "primary_metric": {"path": "metrics.test_excess", "direction": "maximize"},
        "splits": {"holdout_periods": [2024]},
        "cost_model": {"required": True, "metric_path": "metrics.cost_on"},
        "constraints": [{"metric": "metrics.max_drawdown", "op": "gte", "value": -0.2}],
        "robustness_checks": [{"metric": "metrics.sharpe", "op": "gte", "value": 0.5}],
        "comparator": {"metric_path": "metrics.test_excess", "baseline_metric_path": "metrics.baseline_excess", "op": "gt"},
        "fail_closed": True,
    }


def test_protocol_validation_accepts_strategy_neutral_contract():
    assert validate_protocol(explicit_protocol()) == []


def test_protocol_validation_rejects_missing_cost_metric_and_bad_constraint():
    protocol = explicit_protocol()
    protocol["cost_model"] = {"required": True}
    protocol["constraints"] = [{"metric": "x", "op": "bogus", "value": "0"}]
    errors = validate_protocol(protocol)
    assert "cost_model.metric_path is required when cost_model.required is true" in errors
    assert "constraints[0].op is invalid" in errors
    assert "constraints[0].value must be numeric" in errors


def test_protocol_evaluation_fails_closed_for_missing_metrics():
    result = evaluate_constraints(explicit_protocol(), {"metrics": {"test_excess": 0.2}})
    assert result["protocol_pass"] is False
    assert result["protocol_checks"]["cost_model"]["reason"] == "missing"


def test_protocol_enforces_robustness_and_baseline_comparator():
    metrics = {"test_excess": 0.2, "baseline_excess": 0.1, "cost_on": 1.0, "max_drawdown": -0.1, "sharpe": 0.7}
    result = evaluate_constraints(explicit_protocol(), {"metrics": metrics})
    assert result["protocol_pass"] is True
    assert result["protocol_checks"]["robustness_0"]["status"] == "passed"
    assert result["protocol_checks"]["comparator"]["status"] == "passed"


def test_protocol_distinguishes_missing_and_non_numeric_metrics():
    protocol = explicit_protocol()
    result = evaluate_constraints(protocol, {"metrics": {"test_excess": "bad", "cost_on": "bad", "max_drawdown": -0.1}})
    assert result["protocol_checks"]["constraint_0"]["reason"] is None
    assert result["protocol_checks"]["cost_model"]["reason"] == "non_numeric"
    assert result["protocol_checks"]["primary_metric"]["reason"] == "non_numeric"


def test_cb_arb_invalid_explicit_protocol_does_not_fallback():
    protocol, errors = protocol_for_proposal({"strategy_id": "cb_arb_new", "evaluation_protocol": {"schema_version": 99}})
    assert protocol is None
    assert errors


def test_declared_protocol_decisions_are_classified():
    mapping = yaml.safe_load(open("data/research_framework/result_classification_map.yaml", encoding="utf-8"))["result_decisions"]
    assert {"passed_declared_protocol", "failed_declared_protocol", "evaluation_protocol_invalid"} <= set(mapping)


def test_non_cb_arb_proposal_requires_explicit_protocol():
    protocol, errors = protocol_for_proposal({"strategy_id": "new_family"})
    assert protocol is None
    assert errors == ["evaluation_protocol is required for non-cb_arb strategies"]


def test_cb_arb_uses_explicit_compatibility_adapter():
    protocol, errors = protocol_for_proposal({"strategy_id": "cb_arb_new_variant"})
    assert errors == []
    assert protocol == CB_ARB_COMPAT_PROTOCOL


def test_cb_arb_compatibility_adapter_is_idempotent_when_compiler_reuses_it():
    proposal = {"strategy_id": "cb_arb_new_variant"}
    protocol, errors = protocol_for_proposal(proposal)
    assert errors == []
    proposal["evaluation_protocol"] = protocol
    reused, reused_errors = protocol_for_proposal(proposal)
    assert reused_errors == []
    assert reused == CB_ARB_COMPAT_PROTOCOL


def test_legacy_compatibility_mode_is_not_validated_as_strict_numeric_contract():
    assert protocol_for_proposal({"strategy_id": "cb_arb_legacy", "evaluation_protocol": dict(CB_ARB_COMPAT_PROTOCOL)}) == (CB_ARB_COMPAT_PROTOCOL, [])


def test_non_cb_arb_cannot_bypass_strict_validation_with_legacy_mode():
    protocol, errors = protocol_for_proposal({"strategy_id": "other_strategy", "evaluation_protocol": dict(CB_ARB_COMPAT_PROTOCOL)})
    assert protocol is None
    assert "cost_model.metric_path is required when cost_model.required is true" in errors
