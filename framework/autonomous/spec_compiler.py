"""Compile validated proposals into guarded READY/DRAFT/REJECT results."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from framework.autonomous.artifacts import ArtifactStore
    from framework.autonomous.executor_registry import capability_catalog, capability_ids_to_mechanics
    from framework.autonomous.executor_registry import match_executor
    from framework.autonomous.executor_registry import validate_registry_schema
    from framework.autonomous.proposal_schema import validate_proposal
except ModuleNotFoundError:  # importlib-based acceptance tests load files directly
    from artifacts import ArtifactStore  # type: ignore
    from executor_registry import capability_catalog, capability_ids_to_mechanics  # type: ignore
    from executor_registry import match_executor  # type: ignore
    from executor_registry import validate_registry_schema  # type: ignore
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


def _portable_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _registry_capability_ids(registry: dict[str, Any]) -> set[str]:
    return set(capability_catalog(registry))


def _proposal_fingerprint(proposal: dict[str, Any]) -> str:
    payload = {
        "hypothesis": proposal.get("hypothesis"),
        "capability_ids": sorted(proposal.get("capability_ids", [])),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _proposal_data(proposal: dict[str, Any]) -> set[str]:
    return {str(item["path"] if isinstance(item, dict) else item) for item in proposal.get("required_data", [])}


def _missing_proposal_data(proposal: dict[str, Any], repo_root: Path | None = None) -> list[str]:
    root = repo_root or REPO_ROOT
    missing: list[str] = []
    for raw_path in _proposal_data(proposal):
        if not raw_path:
            continue
        path = Path(raw_path)
        resolved = path if path.is_absolute() else root / path
        if not resolved.exists():
            missing.append(raw_path)
    return sorted(missing)


def _normalized_proposal(proposal: dict[str, Any], mechanics: set[str], capability_ids: set[str]) -> dict[str, Any]:
    normalized = dict(proposal)
    normalized["mechanics"] = sorted(mechanics)
    normalized["capability_ids"] = sorted(capability_ids)
    return normalized


def _closed_intersections(proposal: dict[str, Any], mechanics: set[str], closed_tags: dict[str, Any]) -> set[str]:
    candidates = set(mechanics)
    for key in ("family", "required_executor"):
        value = proposal.get(key)
        if value:
            candidates.add(str(value))
    for tag in proposal.get("closed_direction_tags", []) or []:
        candidates.add(str(tag))
    return candidates & set(closed_tags)


def _missing_executor_data(executor: Any, repo_root: Path | None = None) -> list[str]:
    root = repo_root or REPO_ROOT
    data = _executor_value(executor, "required_data", []) or []
    missing: list[str] = []
    for item in data:
        path_value = item.get("path") if isinstance(item, dict) else item
        if not path_value:
            continue
        path = Path(str(path_value))
        resolved = path if path.is_absolute() else root / path
        if not resolved.exists():
            missing.append(str(path_value))
    return missing


def _write_yaml(path: Path, data: dict[str, Any]) -> str:
    return str(ArtifactStore().write_yaml(path, data, no_aliases=True))


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _executor_value(executor: Any, key: str, default: Any = None) -> Any:
    if isinstance(executor, dict):
        return executor.get(key, default)
    return getattr(executor, key, default)


def _format_command_template(executor: Any, proposal: dict[str, Any], output_dir: Path) -> list[str]:
    template = _executor_value(executor, "command_template", []) or []
    if not isinstance(template, list):
        return []
    config = dict(_executor_value(executor, "default_config", {}) or {})
    config.update(proposal.get("executor_config") or {})
    portable_output_dir = _portable_path(output_dir)
    config.setdefault("output_dir", portable_output_dir)
    formatted: list[str] = []
    for item in template:
        text = str(item)
        for key, value in config.items():
            text = text.replace("{" + str(key) + "}", str(value))
        text = text.replace("{output_dir}", portable_output_dir)
        formatted.append(text)
    return formatted


def _executor_sync_paths(executor: Any) -> list[str]:
    paths: list[str] = []
    script = _executor_value(executor, "script_path")
    if script:
        paths.append(str(script))
    paths.extend(str(path) for path in _executor_value(executor, "extra_sync_paths", []) or [])
    for item in _executor_value(executor, "required_data", []) or []:
        path = item.get("path") if isinstance(item, dict) else item
        if path:
            paths.append(str(path))
    paths.extend(
        [
            "scripts/auto_research_pipeline.py",
            "scripts/gatekeeper.py",
            "scripts/validate_spec.py",
            "scripts/research_sanity_checker.py",
            "data/research_framework/runtime_entrypoints.yaml",
            "data/research_framework/protocol_rules.yaml",
            "data/research_framework/experiments.yaml",
            "data/research_framework/result_classification_map.yaml",
        ]
    )
    return list(dict.fromkeys(paths))


def _run_id(proposal: dict[str, Any]) -> str:
    proposal_id = str(proposal.get("proposal_id") or "auto_strategy")
    return proposal_id.replace(" ", "_")


def _base_spec(
    proposal: dict[str, Any],
    status: str,
    reason: str,
    mechanics: set[str] | None = None,
) -> dict[str, Any]:
    resolved_mechanics = sorted(mechanics if mechanics is not None else set(proposal.get("mechanics", [])))
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
                "range": resolved_mechanics,
                "type": "categorical",
                "description": "Mechanics resolved from capability_ids requested by the generated proposal.",
            }
        ],
        "capability_ids": sorted(proposal.get("capability_ids", [])),
        "mechanics": resolved_mechanics,
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
        "stop_conditions": [
            "executor match fails",
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
    recent_proposals: list[dict[str, Any]],
    output_dir: Path | None = None,
) -> CompileResult:
    registry_errors = validate_registry_schema(registry)
    if registry_errors:
        return CompileResult("DRAFT", "executor registry invalid", errors=registry_errors)

    vocab = _registry_mechanics(registry) | set(closed_tags)
    capability_vocab = _registry_capability_ids(registry)
    errors = validate_proposal(proposal, vocab, capability_vocab=capability_vocab or None)
    if errors:
        return CompileResult("REJECT", "proposal schema invalid", errors=errors)

    capability_ids = set(str(item) for item in proposal.get("capability_ids", []))
    if not capability_ids and proposal.get("missing_capability_request"):
        spec_path = None
        plan_path = None
        if output_dir is not None:
            out = Path(output_dir)
            plan_path = _write_yaml(out / "implementation_plan.yaml", {
                "proposal_id": proposal.get("proposal_id"),
                "missing_capability_request": proposal.get("missing_capability_request"),
                "reason": "no registered capability id fits this idea",
            })
            spec_path = _write_yaml(
                out / "spec.yaml",
                _base_spec(proposal, "DRAFT", "missing registered capability", mechanics=set()),
            )
        return CompileResult(
            "DRAFT",
            "missing registered capability",
            spec_path=spec_path,
            implementation_plan_path=plan_path or "implementation_plan.yaml",
        )
    mechanics = capability_ids_to_mechanics(capability_ids, registry) if capability_ids else set(proposal.get("mechanics", []))
    proposal = _normalized_proposal(proposal, mechanics, capability_ids)
    closed = _closed_intersections(proposal, mechanics, closed_tags)
    if closed:
        return CompileResult("REJECT", f"proposal intersects closed directions: {sorted(closed)}")

    current_fp = _proposal_fingerprint(proposal)
    for old in recent_proposals[-5:]:
        if _proposal_fingerprint(old) == current_fp:
            return CompileResult("REJECT", "proposal cycle/repeat detected")

    match = match_executor(
        mechanics,
        _proposal_data(proposal),
        registry,
        proposal_capability_ids=capability_ids,
        required_executor=str(proposal.get("required_executor") or ""),
    )
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
                _base_spec(proposal, "DRAFT", "no strict executor match", mechanics=mechanics),
            )
        else:
            plan_path = "implementation_plan.yaml"
        return CompileResult(
            "DRAFT",
            "no strict executor match",
            spec_path=spec_path,
            implementation_plan_path=plan_path,
        )

    matching_rules = registry.get("matching_rules") or {}
    if isinstance(matching_rules, dict) and matching_rules.get("require_required_data_exists") is True:
        missing_proposal_data = _missing_proposal_data(proposal)
        if missing_proposal_data:
            spec_path = None
            plan_path = None
            if output_dir is not None:
                out = Path(output_dir)
                plan_path = _write_yaml(out / "implementation_plan.yaml", {
                    "proposal_id": proposal.get("proposal_id"),
                    "missing_data": missing_proposal_data,
                    "reason": "proposal required data missing on disk",
                })
                spec_path = _write_yaml(
                    out / "spec.yaml",
                    _base_spec(proposal, "DRAFT", "proposal required data missing", mechanics=mechanics),
                )
            return CompileResult(
                "DRAFT",
                "proposal required data missing",
                spec_path=spec_path,
                implementation_plan_path=plan_path or "implementation_plan.yaml",
            )
        missing_data = _missing_executor_data(match)
        if missing_data:
            spec_path = None
            plan_path = None
            if output_dir is not None:
                out = Path(output_dir)
                plan_path = _write_yaml(out / "implementation_plan.yaml", {
                    "proposal_id": proposal.get("proposal_id"),
                    "executor_id": _executor_value(match, "executor_id") or _executor_value(match, "id"),
                    "missing_data": missing_data,
                    "reason": "executor required data missing on disk",
                })
                spec_path = _write_yaml(
                    out / "spec.yaml",
                    _base_spec(proposal, "DRAFT", "executor required data missing", mechanics=mechanics),
                )
            return CompileResult(
                "DRAFT",
                "executor required data missing",
                spec_path=spec_path,
                implementation_plan_path=plan_path or "implementation_plan.yaml",
            )

    spec_path = None
    if output_dir is not None:
        ready_spec = _base_spec(proposal, "READY", "all guarded checks passed", mechanics=mechanics)
        command = _format_command_template(match, proposal, Path(output_dir))
        ready_spec.update({
            "executor_id": _executor_value(match, "executor_id") or _executor_value(match, "id"),
            "command_template": _executor_value(match, "command_template"),
            "budget_estimate": _executor_value(match, "budget_estimate"),
            "automation": {
                "output_dir": _portable_path(Path(output_dir)),
                "command": command,
                "sync_paths": _executor_sync_paths(match),
                "verdict": {"pass_field": "adoption_pass"},
            },
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
