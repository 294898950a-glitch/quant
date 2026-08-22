from __future__ import annotations

import unittest

import pandas as pd

from framework.evaluation.benchmark import load_benchmark
from framework.evaluation.contract import GateStatus, plan_from_mapping, validate_plan_complete
from framework.evaluation.evidence import measurement_evidence
from framework.evaluation.validation import evaluate_plan


def plan() -> dict:
    gates = {
        name: {"required": True, "required_evidence_keys": [name], "allowed_sources": ["holdout"], "required_sources": ["holdout"]}
        for name in ("integrity", "signal", "benchmark", "friction", "generalization", "constraints")
    }
    gates["friction"]["scenario"] = "cost_on"
    gates["signal"].update({"metric": "signal", "op": "gte", "value": 0.0})
    return {"schema_version": 1, "plan_version": "p1", "mode": "declared", "required_gates": list(gates), "gates": gates}


class TestEvaluationContract(unittest.TestCase):
    def test_complete_plan_requires_integrity_and_source_subset(self) -> None:
        raw = plan()
        raw["gates"]["signal"]["required_sources"] = ["train"]
        self.assertEqual(validate_plan_complete(plan()), [])
        errors = validate_plan_complete(raw)
        self.assertTrue(any("required_sources" in error for error in errors))

    def test_integrity_failure_blocks_other_gates(self) -> None:
        raw = plan()
        evidence = [measurement_evidence(id="i", key="integrity", value=True, source="train", plan_version="p1")]
        report = evaluate_plan(raw, evidence)
        self.assertEqual(report.decision, "evaluation_protocol_invalid")
        self.assertEqual(report.gates["signal"].status, GateStatus.BLOCKED)

    def test_signal_rejects_train_source(self) -> None:
        raw = plan()
        evidence = [measurement_evidence(id=name, key=name, value=True, source="holdout", plan_version="p1") for name in raw["required_gates"]]
        evidence = [item for item in evidence if item.key != "signal"]
        evidence.append(measurement_evidence(id="signal", key="signal", value=1.0, source="train", plan_version="p1"))
        report = evaluate_plan(raw, evidence)
        self.assertEqual(report.gates["signal"].status, GateStatus.FAILED)

    def test_benchmark_requires_injected_provider(self) -> None:
        with self.assertRaises(ValueError):
            load_benchmark("20240101", "20240102")

        class Provider:
            def get_returns(self, benchmark_id: str, start: str | None, end: str | None) -> pd.Series:
                return pd.Series([0.01], index=["20240101"])

            def get_total_return(self, benchmark_id: str, start: str, end: str) -> float:
                return 0.01

        result = load_benchmark("20240101", "20240102", provider=Provider())
        self.assertAlmostEqual(float(result.iloc[0]), 0.01)


if __name__ == "__main__":
    unittest.main()
