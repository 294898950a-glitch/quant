"""Compile validated proposals into guarded READY/DRAFT/REJECT results."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.artifacts import ArtifactStore
    from framework.autonomous.executor_registry import match_executor
    from framework.autonomous.proposal_schema import validate_proposal
except ModuleNotFoundError:  # importlib-based acceptance tests load files directly
    from artifacts import ArtifactStore  # type: ignore
    from executor_registry import match_executor  # type: ignore
    from proposal_schema import validate_proposal  # type: ignore


class CompileResult:
    def __init__(
        self,
        status: str,
        reason: str,
        spec_path: str | None = None,
        implementation_plan_path: str | None = None,
        errors: list[str] | None = None,
    ):
        self.status = status
        self.reason = reason
        self.spec_path = spec_path
        self.implementation_plan_path = implementation_plan_path
        self.errors = errors or []


def _registry_mechanics(registry: dict[str, Any]) -> set[str]:
    raw = registry.get("executors", [])
    executors = raw.values() if isinstance(raw, dict) else raw
    vocab: set[str] = set()
    for executor in executors or []:
        vocab.update(executor.get("can_test", []))
        vocab.update(executor.get("cannot_test", []))
    return vocab


def _proposal_fingerprint(proposal: dict[str, Any]) -> str:
    payload = {
        "hypothesis": proposal.get("hypothesis"),
        "mechanics": sorted(proposal.get("mechanics", [])),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _estimate_cost_yuan(executor: dict[str, Any]) -> float:
    budget = executor.get("budget_estimate", {}) if isinstance(executor, dict) else {}
    if budget.get("estimated_cost_yuan") is not None:
        return float(budget["estimated_cost_yuan"])
    # Current observed spot economics are roughly 6.25 CNY/hour.
    return float(budget.get("spot_minutes", 0)) * 6.25 / 60.0 + float(budget.get("sig_minutes", 0)) * 0.0


def _proposal_data(proposal: dict[str, Any]) -> set[str]:
    return {str(item["path"] if isinstance(item, dict) else item) for item in proposal.get("required_data", [])}


def _write_yaml(path: Path, data: dict[str, Any]) -> str:
    return str(ArtifactStore().write_yaml(path, data, no_aliases=True))


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _executor_value(executor: Any, key: str, default: Any = None) -> Any:
    if isinstance(executor, dict):
        return executor.get(key, default)
    return getattr(executor, key, default)


def _run_id(proposal: dict[str, Any]) -> str:
    proposal_id = str(proposal.get("proposal_id") or "auto_strategy")
    return proposal_id.replace(" ", "_")


def _base_spec(
    proposal: dict[str, Any],
    status: str,
    budget_cap: float,
    reason: str,
) -> dict[str, Any]:
    mechanics = proposal.get("mechanics", [])
    success = proposal.get("success_criteria", {})
    return {
        "schema_version": 1,
        "run_id": _run_id(proposal),
        "date": _today(),
        "strategy_id": proposal.get("strategy_id"),
        "l0_entry_id": 1,
        "l0_source": "autonomous strategy ideator from recent_results_digest",
        "hypothesis": proposal.get("hypothesis"),
        "source_insight": proposal.get("source_insight"),
        "parameter_space": [
            {
                "name": "mechanics",
                "range": mechanics,
                "type": "categorical",
                "description": "Mechanics requested by the generated proposal.",
            }
        ],
        "new_data_sources": [
            {"path": str(item["path"] if isinstance(item, dict) else item)}
            for item in proposal.get("required_data", [])
        ],
        "hard_floors": success if isinstance(success, dict) and success else {"test_excess_min": 0.0},
        "cv_design": "single-window",
        "cv_holdout_years": [2025, 2026],
        "compute_estimate": {
            "sig_minutes": 0,
            "spot_minutes": 0 if status == "DRAFT" else 60,
            "local_minutes": 0,
            "estimated_cost_yuan": 0.0,
        },
        "budget_cap_yuan": float(budget_cap),
        "stop_conditions": [
            "executor match fails",
            "budget exceeds cap",
            "required artifacts are missing",
        ],
        "artifacts_required": ["implementation_plan.yaml", "spec.yaml"]
        if status == "DRAFT"
        else ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
        "status": status,
        "auxiliary_metrics": ["train_excess_return", "test_excess_return", "max_drawdown"],
        "escalation": ["DRAFT requires executor implementation before run"],
        "notes": reason,
        "proposal": proposal,
    }


def compile(
    proposal: dict[str, Any],
    registry: dict[str, Any],
    closed_tags: dict[str, Any],
    budget_cap: float,
    recent_proposals: list[dict[str, Any]],
    output_dir: Path | None = None,
) -> CompileResult:
    vocab = _registry_mechanics(registry) | set(closed_tags) | set(proposal.get("mechanics", []))
    errors = validate_proposal(proposal, vocab)
    if errors:
        return CompileResult("REJECT", "proposal schema invalid", errors=errors)

    mechanics = set(proposal.get("mechanics", []))
    closed = mechanics & set(closed_tags)
    if closed:
        return CompileResult("REJECT", f"mechanics intersect closed directions: {sorted(closed)}")

    current_fp = _proposal_fingerprint(proposal)
    for old in recent_proposals[-5:]:
        if _proposal_fingerprint(old) == current_fp:
            return CompileResult("REJECT", "proposal cycle/repeat detected")

    match = match_executor(mechanics, _proposal_data(proposal), registry)
    if match is None:
        plan_path = None
        spec_path = None
        if output_dir is not None:
            out = Path(output_dir)
            plan_path = _write_yaml(out / "implementation_plan.yaml", {
                "proposal_id": proposal.get("proposal_id"),
                "missing_executor": proposal.get("required_executor"),
                "mechanics": sorted(mechanics),
                "reason": "no strict executor match",
            })
            spec_path = _write_yaml(
                out / "spec.yaml",
                _base_spec(proposal, "DRAFT", budget_cap, "no strict executor match"),
            )
        else:
            plan_path = "implementation_plan.yaml"
        return CompileResult(
            "DRAFT",
            "no strict executor match",
            spec_path=spec_path,
            implementation_plan_path=plan_path,
        )

    cost = _estimate_cost_yuan(match)
    if cost > float(budget_cap):
        return CompileResult("DRAFT", f"budget {cost:.2f} exceeds cap {budget_cap:.2f}", implementation_plan_path="implementation_plan.yaml")

    spec_path = None
    if output_dir is not None:
        ready_spec = _base_spec(proposal, "READY", budget_cap, "all guarded checks passed")
        ready_spec.update({
            "executor_id": _executor_value(match, "executor_id") or _executor_value(match, "id"),
            "command_template": _executor_value(match, "command_template"),
            "budget_estimate": _executor_value(match, "budget_estimate"),
            "ideation_provenance": {
                "ai_provider": proposal.get("ai_provider"),
                "prompt_path": proposal.get("prompt_path"),
                "proposal_path": proposal.get("proposal_path"),
                "response_hash": proposal.get("response_hash"),
                "compiler_decision": "READY",
                "compiler_reason": "all guarded checks passed",
            },
        })
        spec_path = _write_yaml(Path(output_dir) / "spec.yaml", ready_spec)
    return CompileResult("READY", "all guarded checks passed", spec_path=spec_path)
