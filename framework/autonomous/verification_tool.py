"""Guarded reviewer verification request interface."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.evidence_tool_registry import EvidenceToolRegistry
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from evidence_tool_registry import EvidenceToolRegistry  # type: ignore

DEFAULT_TOOL_REGISTRY_PATH = Path("data/research_framework/evidence_tool_registry.yaml")

ALLOWED_REQUEST_TYPES = {
    "metric_slice",
    "year_breakdown",
    "source_attribution",
    "trade_group_attribution",
    "baseline_vs_candidate_diff",
    "artifact_consistency_check",
}
PROHIBITED_REQUEST_TYPES = {"new_strategy", "param_optimization", "promotion", "full_backtest_grid"}
MAX_ROUNDS = 2
MAX_BUDGET_YUAN = 50.0
ROLE_REQUESTS = {
    "ideator": [
        "artifact_consistency_check",
        "baseline_vs_candidate_diff",
        "year_breakdown",
    ],
    "reviewer": [
        "artifact_consistency_check",
        "source_attribution",
        "metric_slice",
    ],
}


def load_tool_registry(path: Path | str = DEFAULT_TOOL_REGISTRY_PATH) -> dict[str, Any]:
    return EvidenceToolRegistry(path).load()


def registered_tool_ids(registry: dict[str, Any] | None = None) -> set[str]:
    return EvidenceToolRegistry().implemented_ids(registry)


def tool_manifest_for_prompt(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    return EvidenceToolRegistry().manifest(registry)


def tool_manifest_sha256(registry: dict[str, Any] | None = None) -> str:
    return EvidenceToolRegistry().manifest_sha256(registry)


def ensure_tool_registered(request_type: str, registry: dict[str, Any] | None = None) -> None:
    EvidenceToolRegistry().ensure_implemented(request_type, registry)


class VerificationResult(dict):
    def __init__(self, review_id: str, request_type: str, status: str = "queued", data: dict[str, Any] | None = None):
        super().__init__(review_id=review_id, request_type=request_type, status=status, data=data or {})
        self.review_id = review_id
        self.request_type = request_type
        self.status = status
        self.data = data or {}


class EvidenceToolkit:
    def __init__(
        self,
        review_dir: Path | None = None,
        max_budget_yuan: float = MAX_BUDGET_YUAN,
        tool_registry_path: Path | str = DEFAULT_TOOL_REGISTRY_PATH,
    ) -> None:
        self.review_dir = Path(review_dir) if review_dir is not None else None
        self.max_budget_yuan = float(max_budget_yuan)
        self.tool_registry_path = Path(tool_registry_path)
        self.tool_registry = load_tool_registry(self.tool_registry_path)

    def available_tools_for_prompt(self) -> dict[str, Any]:
        return tool_manifest_for_prompt(self.tool_registry)

    def request(
        self,
        review_id: str,
        request_type: str,
        params: dict[str, Any] | None = None,
        round_num: int = 1,
    ) -> VerificationResult:
        ensure_tool_registered(request_type, self.tool_registry)
        return request_verification(
            review_id=review_id,
            request_type=request_type,
            params=params or {},
            round_num=round_num,
            review_dir=self.review_dir,
            max_budget_yuan=self.max_budget_yuan,
            tool_registry=self.tool_registry,
        )

    def collect_for_role(
        self,
        role: str,
        review_id: str,
        request_types: list[str] | None = None,
        round_num: int = 1,
        purpose: str | None = None,
    ) -> list[dict[str, Any]]:
        selected = request_types or ROLE_REQUESTS.get(role, [])
        request_ids = [f"{review_id}:{role}:{request_type}" for request_type in selected]
        evidence = []
        for request_type, request_id in zip(selected, request_ids):
            result = self.request(
                review_id=review_id,
                request_type=request_type,
                params={
                    "request_id": request_id,
                    "preregistered_request_ids": request_ids,
                    "purpose": purpose or f"{role}_evidence",
                },
                round_num=round_num,
            )
            evidence.append(
                {
                    "request_type": request_type,
                    "status": result.status,
                    "data": result.data,
                }
            )
        return evidence


def _append_log(review_dir: Path, row: dict[str, Any]) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    with (review_dir / "verification_log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def _load_review(review_dir: Path) -> dict[str, Any]:
    path = review_dir / "review.yaml"
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metric_diff(facts: dict[str, Any]) -> dict[str, Any]:
    metrics = facts.get("key_metrics") if isinstance(facts.get("key_metrics"), dict) else {}
    baseline = metrics.get("baseline_test") if isinstance(metrics.get("baseline_test"), dict) else {}
    selected = metrics.get("selected_test") if isinstance(metrics.get("selected_test"), dict) else {}
    result = {
        "baseline_test": baseline,
        "selected_test": selected,
    }
    for key in ("total_return", "excess_return", "max_drawdown", "score"):
        if key in baseline and key in selected:
            try:
                result[f"selected_minus_baseline_{key}"] = float(selected[key]) - float(baseline[key])
            except (TypeError, ValueError):
                pass
    return result


def _execute_request(request_type: str, params: dict[str, Any], review_dir: Path | None) -> tuple[str, dict[str, Any]]:
    if review_dir is None:
        return "queued_for_vm_only", {}
    review = _load_review(review_dir)
    if not review:
        return "no_data", {"reason": "review.yaml not found"}
    facts = review.get("facts") if isinstance(review.get("facts"), dict) else {}
    metrics = facts.get("key_metrics") if isinstance(facts.get("key_metrics"), dict) else {}

    if request_type == "artifact_consistency_check":
        checks = []
        for artifact in facts.get("source_artifacts", []) or []:
            if not isinstance(artifact, dict) or not artifact.get("path"):
                continue
            path = Path(artifact["path"])
            checks.append(
                {
                    "path": str(path),
                    "exists": path.exists(),
                    "expected_sha256": artifact.get("sha256"),
                    "actual_sha256": _sha256(path) if path.exists() else None,
                }
            )
        return "completed", {"checks": checks}

    if request_type == "baseline_vs_candidate_diff":
        return "completed", _metric_diff(facts)

    if request_type == "metric_slice":
        block = params.get("block")
        if block:
            return "completed", {str(block): metrics.get(str(block))}
        return "completed", metrics

    if request_type == "year_breakdown":
        return "completed", {
            "baseline_2020": metrics.get("baseline_2020"),
            "selected_2020": metrics.get("selected_2020"),
        }

    if request_type == "source_attribution":
        return "completed", {
            "decision": facts.get("decision"),
            "experiment": facts.get("experiment"),
            "source_artifacts": facts.get("source_artifacts"),
        }

    if request_type == "trade_group_attribution":
        return "no_data", {"reason": "trade group attribution is not present in review.yaml"}

    return "queued_for_vm_only", {}


def request_verification(
    review_id: str,
    request_type: str,
    params: dict[str, Any],
    round_num: int,
    review_dir: Path | None = None,
    max_budget_yuan: float = MAX_BUDGET_YUAN,
    tool_registry: dict[str, Any] | None = None,
) -> VerificationResult:
    if request_type in PROHIBITED_REQUEST_TYPES or request_type not in ALLOWED_REQUEST_TYPES:
        raise ValueError(f"verification request type is not allowed: {request_type}")
    ensure_tool_registered(request_type, tool_registry)
    if round_num > MAX_ROUNDS:
        raise ValueError("verification is capped at 2 rounds")
    request_id = params.get("request_id", f"{review_id}:{round_num}:{request_type}")
    if round_num == 1:
        preregistered = params.get("preregistered_request_ids") or [request_id]
    else:
        preregistered = params.get("preregistered_request_ids", [])
        followup = params.get("followup_request_id")
        if request_id not in preregistered and request_id != followup:
            raise ValueError("round 2 verification must use preregistered request_id or one follow-up")
    status, data = _execute_request(request_type, params, Path(review_dir) if review_dir is not None else None)
    result = VerificationResult(review_id, request_type, status=status, data=data)
    if review_dir is not None:
        _append_log(Path(review_dir), {
            "review_id": review_id,
            "request_id": request_id,
            "request_type": request_type,
            "round_num": round_num,
            "params": params,
            "max_budget_yuan": max_budget_yuan,
            "placement": "vm_only",
            "status": result.status,
            "data": result.data,
        })
    return result


def collect_evidence_for_role(
    role: str,
    review_id: str,
    review_dir: Path | None = None,
    request_types: list[str] | None = None,
    max_budget_yuan: float = MAX_BUDGET_YUAN,
) -> list[dict[str, Any]]:
    return EvidenceToolkit(review_dir=review_dir, max_budget_yuan=max_budget_yuan).collect_for_role(
        role=role,
        review_id=review_id,
        request_types=request_types,
    )
