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
    from framework.autonomous.result_classification import closes_direction, evidence_usable
    from framework.autonomous.status_codes import prompt_code_menu, status_label
    from framework.autonomous.verification_tool import EvidenceToolkit
except ModuleNotFoundError:  # importlib-based acceptance tests load files directly
    from artifacts import ArtifactStore  # type: ignore
    from result_classification import closes_direction, evidence_usable  # type: ignore
    from status_codes import prompt_code_menu, status_label  # type: ignore
    from verification_tool import EvidenceToolkit  # type: ignore


SOURCE_NAMES = ("spec.yaml", "summary.json", "summary.csv", "summary_test.csv", "report.yaml", "diagnostic.yaml")
AI_REVIEW_REQUIRED_KEYS = {
    "review_status_code",
    "result_summary",
    "main_reason",
    "failure_causes",
    "next_research_directions",
    "evidence_gaps",
}
AI_REVIEW_MAX_ATTEMPTS = 3


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


def _compact_summary_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "adoption_pass",
        "selected_passes",
        "selected_total",
        "candidate_count",
        "task_count",
    )
    out = {key: summary[key] for key in keys if key in summary}
    if isinstance(summary.get("best_candidate"), dict):
        best = summary["best_candidate"]
        out["selected_name"] = "best_candidate"
        params = best.get("params")
        if isinstance(params, dict):
            out["selected_params"] = params
        for key in (
            "train_total_return",
            "test_total_return",
            "yr2020_total_return",
            "train_win_rate",
            "test_win_rate",
            "yr2020_win_rate",
            "train_score",
            "test_score",
            "yr2020_score",
        ):
            if key in best:
                out[key] = best[key]
    return out


def _manifest_verdict(run_dir: Path) -> dict[str, Any]:
    run_id = run_dir.name
    path = Path("data/research_framework/run_manifests") / f"{run_id}.yaml"
    if not path.exists():
        return {}
    manifest = _read_yaml(path)
    verdict = manifest.get("automation", {}).get("verdict") if isinstance(manifest.get("automation"), dict) else None
    return verdict if isinstance(verdict, dict) else {}


def extract_facts(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    spec = _read_yaml(run_dir / "spec.yaml")
    summary = _read_json(run_dir / "summary.json")
    report = _read_yaml(run_dir / "report.yaml")
    manifest_verdict = _manifest_verdict(run_dir)
    decision = (
        manifest_verdict.get("decision")
        or (((spec.get("automation") or {}).get("verdict") or {}).get("decision"))
        or report.get("decision")
    )
    key_metrics = _compact_summary_metrics(summary)
    if not key_metrics.get("adoption_pass") and not manifest_verdict.get("pass_value") and "adoption_pass" not in summary:
        decision = decision or "no_adoption_decision_unusable"
    facts: dict[str, Any] = {
        "run_id": spec.get("run_id") or run_dir.name,
        "strategy_id": spec.get("strategy_id"),
        "decision": decision,
        "verdict": manifest_verdict,
        "key_metrics": key_metrics,
        "exit_code": summary.get("exit_code"),
        "cost_yuan": summary.get("compute_cost_yuan") or summary.get("cost_yuan"),
        "best_train_variant": summary.get("best_train_variant"),
        "best_test_variant": summary.get("best_test_variant"),
    }
    if decision:
        facts["evidence_usable"] = evidence_usable(str(decision))
    for csv_name in ("summary.csv", "summary_test.csv"):
        facts.update(_read_csv_metrics(run_dir / csv_name))
    facts = {k: v for k, v in facts.items() if v is not None}
    facts["artifacts_hash"] = _hash_sources(run_dir)
    return facts


def verify_facts_hash(facts: dict[str, Any], run_dir: Path) -> bool:
    return facts.get("artifacts_hash") == _hash_sources(Path(run_dir))


def _compact_for_prompt(value: Any, *, max_items: int = 20) -> Any:
    if isinstance(value, dict):
        return {str(k): _compact_for_prompt(v, max_items=max_items) for k, v in list(value.items())[:max_items]}
    if isinstance(value, list):
        return [_compact_for_prompt(item, max_items=max_items) for item in value[:max_items]]
    return value


def build_ai_review_prompt(run_dir: Path, facts: dict[str, Any]) -> str:
    spec = _compact_for_prompt(_read_yaml(run_dir / "spec.yaml"))
    report = _compact_for_prompt(_read_yaml(run_dir / "report.yaml"))
    diagnostic = _compact_for_prompt(_read_yaml(run_dir / "diagnostic.yaml"))
    context = {
        "facts_locked_by_code": facts,
        "spec_context": spec,
        "report_context": report,
        "diagnostic_context": diagnostic,
    }
    required_yaml = {
        "review_status_code": prompt_code_menu("review_status"),
        "result_summary": "one short sentence based only on facts_locked_by_code",
        "main_reason": "one short sentence explaining the decision",
        "failure_causes": ["short cause 1", "short cause 2"],
        "next_research_directions": [
            {
                "direction": "short direction name",
                "why": "why this follows from the run result",
                "priority_code": prompt_code_menu("review_priority"),
            }
        ],
        "evidence_gaps": ["missing or weak evidence, empty list if none"],
    }
    return (
        "You are the review_memory node for a quant research run.\n"
        "The facts below are locked by code. Do not rewrite, invent, or correct them.\n"
        "Return YAML only. No Markdown fences. No prose outside YAML.\n"
        "Use exactly the top-level keys shown in required_output.\n"
        "Status-like fields must use numeric codes only; do not output text status names.\n"
        "If the facts are insufficient, set review_status_code to the code for inconclusive or needs_manual_review.\n\n"
        "required_output:\n"
        f"{yaml.safe_dump(required_yaml, allow_unicode=True, sort_keys=False)}\n"
        "run_context:\n"
        f"{yaml.safe_dump(context, allow_unicode=True, sort_keys=False)}"
    )


def parse_ai_review_yaml(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        raise ValueError("AI review returned empty content")
    if "```" in text:
        raise ValueError("AI review must return raw YAML without Markdown fences")
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"AI review YAML parse error: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("AI review YAML root must be a mapping")
    extra = set(loaded) - AI_REVIEW_REQUIRED_KEYS
    missing = AI_REVIEW_REQUIRED_KEYS - set(loaded)
    if extra:
        raise ValueError(f"AI review YAML has unexpected keys: {sorted(extra)}")
    if missing:
        raise ValueError(f"AI review YAML missing keys: {sorted(missing)}")
    if "review_status" in loaded:
        raise ValueError("AI review must use numeric review_status_code, not text review_status")
    loaded["review_status"] = status_label("review_status", loaded.get("review_status_code"))
    for key in ("result_summary", "main_reason"):
        if not isinstance(loaded.get(key), str) or not loaded[key].strip():
            raise ValueError(f"AI review {key} must be a non-empty string")
    for key in ("failure_causes", "next_research_directions", "evidence_gaps"):
        if not isinstance(loaded.get(key), list):
            raise ValueError(f"AI review {key} must be a list")
    for item in loaded["next_research_directions"]:
        if not isinstance(item, dict):
            raise ValueError("AI review next_research_directions items must be mappings")
        allowed = {"direction", "why", "priority_code"}
        extra_item = set(item) - allowed
        if extra_item:
            raise ValueError(f"AI review next_research_directions has unexpected keys: {sorted(extra_item)}")
        item["priority"] = status_label("review_priority", item.get("priority_code"))
    return loaded


def request_ai_review_yaml(ai_adapter: Any, prompt: str) -> tuple[dict[str, Any], Any, int]:
    last_error: Exception | None = None
    current_prompt = prompt
    response = None
    for attempt in range(1, AI_REVIEW_MAX_ATTEMPTS + 1):
        response = ai_adapter.call_active_provider(
            current_prompt,
            schema={
                "type": "review_memory_yaml",
                "format": "yaml",
                "required_keys": sorted(AI_REVIEW_REQUIRED_KEYS),
            },
        )
        try:
            return parse_ai_review_yaml(getattr(response, "content", "")), response, attempt
        except ValueError as exc:
            last_error = exc
            current_prompt = (
                f"{prompt}\n\n"
                "Your previous response was rejected by the YAML parser.\n"
                f"validation_error: {exc}\n"
                "Rewrite the full answer as raw YAML only. Quote any string containing ':' or '|'."
            )
    raise ValueError(f"AI review did not return valid fixed YAML after {AI_REVIEW_MAX_ATTEMPTS} attempts: {last_error}")


def review(run_dir: Path, verification_callback=None, ai_adapter=None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    facts = extract_facts(run_dir)
    review_path = run_dir / "review.yaml"
    existing = _read_yaml(review_path)
    warning = None
    if existing.get("facts") and not verify_facts_hash(existing["facts"], run_dir):
        warning = "existing facts hash mismatch; facts restored from source artifacts"

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
        prompt = build_ai_review_prompt(run_dir, facts)
        interpretation, response, ai_attempts = request_ai_review_yaml(ai_adapter, prompt)
        ai_provider_used = getattr(response, "provider_id", None)
        response_hash = getattr(response, "response_hash", None)
    else:
        ai_attempts = 0

    spec = _read_yaml(run_dir / "spec.yaml")
    decision = str(facts.get("decision") or "")
    closed_directions = []
    if decision and closes_direction(decision):
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
        "ai_review_attempts": ai_attempts,
        "inconclusive": inconclusive,
        "verification_rounds_used": verification_rounds,
    }
    if warning:
        data["warning"] = warning
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


def write_review(run_dir: str | Path) -> Path:
    run_path = Path(run_dir)
    review(run_path)
    return run_path / "review.yaml"


def write_ai_review(run_dir: str | Path, ai_adapter: Any) -> Path:
    run_path = Path(run_dir)
    review(run_path, ai_adapter=ai_adapter)
    return run_path / "review.yaml"
