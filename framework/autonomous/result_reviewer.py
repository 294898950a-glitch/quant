"""Code-first completed-run reviewer."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.artifacts import ArtifactStore
    from framework.autonomous.verification_tool import EvidenceToolkit
except ModuleNotFoundError:  # importlib-based acceptance tests load files directly
    from artifacts import ArtifactStore  # type: ignore
    from verification_tool import EvidenceToolkit  # type: ignore


SOURCE_NAMES = ("spec.yaml", "summary.json", "summary.csv", "summary_test.csv", "report.yaml", "diagnostic.yaml")


def _hash_sources(run_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(run_dir).iterdir()):
        if path.name in SOURCE_NAMES or path.suffix in {".csv", ".json", ".yaml"} and path.name != "review.yaml":
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _read_yaml(path: Path) -> dict[str, Any]:
    return ArtifactStore().read_yaml(path)


def _read_json(path: Path) -> dict[str, Any]:
    return ArtifactStore().read_json(path)


def _read_csv_metrics(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    rows = list(csv.DictReader(path.open("r", encoding="utf-8")))
    facts: dict[str, Any] = {"row_count": len(rows)}
    for row in rows:
        name = row.get("name") or row.get("variant") or ""
        period = row.get("period") or ""
        prefix = f"{name}_{period}".strip("_")
        for field in ("excess_return", "max_drawdown", "total_return"):
            if field in row and row[field] not in ("", None):
                try:
                    facts[f"{prefix}_{field}"] = float(row[field])
                except ValueError:
                    facts[f"{prefix}_{field}"] = row[field]
    return facts


def extract_facts(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    spec = _read_yaml(run_dir / "spec.yaml")
    summary = _read_json(run_dir / "summary.json")
    report = _read_yaml(run_dir / "report.yaml")
    facts: dict[str, Any] = {
        "run_id": spec.get("run_id") or run_dir.name,
        "strategy_id": spec.get("strategy_id"),
        "decision": (((spec.get("automation") or {}).get("verdict") or {}).get("decision") or report.get("decision")),
        "exit_code": summary.get("exit_code"),
        "cost_yuan": summary.get("compute_cost_yuan") or summary.get("cost_yuan"),
        "best_train_variant": summary.get("best_train_variant"),
        "best_test_variant": summary.get("best_test_variant"),
    }
    for csv_name in ("summary.csv", "summary_test.csv"):
        facts.update(_read_csv_metrics(run_dir / csv_name))
    facts = {k: v for k, v in facts.items() if v is not None}
    facts["artifacts_hash"] = _hash_sources(run_dir)
    return facts


def verify_facts_hash(facts: dict[str, Any], run_dir: Path) -> bool:
    return facts.get("artifacts_hash") == _hash_sources(Path(run_dir))


def review(run_dir: Path, verification_callback=None, ai_adapter=None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    facts = extract_facts(run_dir)
    review_path = run_dir / "review.yaml"
    existing = _read_yaml(review_path)
    if existing.get("facts") and not verify_facts_hash(existing["facts"], run_dir):
        existing["warning"] = "existing facts hash mismatch; facts restored from source artifacts"

    verification_rounds = 0
    inconclusive = False
    if verification_callback is not None:
        for round_num in (1, 2):
            verification_rounds = round_num
            result = verification_callback("artifact_consistency_check", {"run_id": facts.get("run_id")})
            if isinstance(result, dict) and result.get("status") != "insufficient":
                break
        else:
            inconclusive = True

    interpretation: dict[str, Any] = {"narrative": ""}
    ai_provider_used = None
    response_hash = None
    if ai_adapter is not None:
        response = ai_adapter.call_active_provider("Review run artifacts without altering facts.", schema={})
        interpretation = {"narrative": getattr(response, "content", "")}
        ai_provider_used = getattr(response, "provider_id", None)
        response_hash = getattr(response, "response_hash", None)

    spec = _read_yaml(run_dir / "spec.yaml")
    decision = str(facts.get("decision") or "")
    closed_directions = []
    if "reject" in decision or "failed" in decision:
        for mechanic in spec.get("mechanics", []) or []:
            closed_directions.append({"mechanic_tag": mechanic, "reason": f"run decision={decision}"})

    data = {
        "schema_version": 1,
        "run_id": facts.get("run_id"),
        "strategy_id": facts.get("strategy_id"),
        "facts": facts,
        "interpretation": interpretation,
        "closed_directions": closed_directions,
        "next_directions": [],
        "ai_provider_used": ai_provider_used,
        "prompt_path": None,
        "response_hash": response_hash,
        "inconclusive": inconclusive,
        "verification_rounds_used": verification_rounds,
    }
    if existing.get("warning"):
        data["warning"] = existing["warning"]
    ArtifactStore().write_yaml(review_path, data)
    if verification_callback is None:
        reviewer_evidence = EvidenceToolkit(review_dir=run_dir).collect_for_role(
            role="reviewer",
            review_id=str(facts.get("run_id") or run_dir.name),
            purpose="reviewer_evidence",
        )
        data["verification_evidence"] = reviewer_evidence
        data["verification_rounds_used"] = max(verification_rounds, 1 if reviewer_evidence else 0)
        ArtifactStore().write_yaml(review_path, data)
    return data
