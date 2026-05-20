"""Recent-results digest built only from review.yaml files."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.artifacts import ArtifactStore
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from artifacts import ArtifactStore  # type: ignore


DEFAULT_DIGEST_PATH = Path("data/research_framework/recent_results_digest.yaml")
DEFAULT_CURRENT_PATH = Path("data/research_framework/current.yaml")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _review_files(review_dir: Path) -> list[Path]:
    root = Path(review_dir)
    direct = list(root.glob("*review.yaml"))
    nested = list(root.glob("*/review.yaml"))
    return sorted({*direct, *nested}, key=lambda path: path.stat().st_mtime)


def build_digest(review_dir: Path, last_n: int = 5) -> dict[str, Any]:
    reviews = []
    closed_counter: Counter[str] = Counter()
    review_paths = _review_files(Path(review_dir))
    selected_paths = review_paths[-last_n:] if last_n >= 0 else review_paths
    for path in selected_paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        facts = data.get("facts") or {}
        experiment = facts.get("experiment") if isinstance(facts.get("experiment"), dict) else {}
        materials = data.get("materials_for_ideator") if isinstance(data.get("materials_for_ideator"), dict) else {}
        interpretation = data.get("interpretation") if isinstance(data.get("interpretation"), dict) else {}
        run = {
            "run_id": data.get("run_id") or facts.get("run_id") or experiment.get("run_id") or path.parent.name,
            "strategy_id": data.get("strategy_id") or facts.get("strategy_id") or experiment.get("strategy_id"),
            "review_status": data.get("review_status"),
            "decision": facts.get("decision"),
            "verdict": _extract_verdict(data, facts),
            "one_line_summary": (
                materials.get("one_line_summary")
                or interpretation.get("machine_summary")
                or data.get("summary")
            ),
            "key_metrics": facts.get("key_metrics") or {},
            "facts_hash": facts.get("artifacts_hash") or facts.get("facts_hash"),
            "review_path": str(path),
        }
        reviews.append(run)
        for item in data.get("closed_directions", []) or []:
            for tag in _closed_tags(item):
                closed_counter[tag] += 1
    return {
        "schema_version": 1,
        "source": "review_yaml_only",
        "updated_at": utc_now_iso(),
        "source_reviews_count": len(review_paths),
        "runs": reviews,
        "recent_runs": reviews,
        "suggested_closed_families": [
            {"tag": tag, "reject_count": count}
            for tag, count in sorted(closed_counter.items())
            if count >= 3
        ],
    }


def write_recent_results_digest(
    output_path: Path | str = DEFAULT_DIGEST_PATH,
    data_dir: Path | str = Path("data"),
    limit: int = 10,
    updated_at: str | None = None,
) -> dict[str, Any]:
    digest = build_digest(Path(data_dir), last_n=limit)
    if updated_at:
        digest["updated_at"] = updated_at
    ArtifactStore().write_yaml(Path(output_path), digest)
    return digest


def update_current_pointer(digest: dict[str, Any], current_yaml_path: Path) -> None:
    path = Path(current_yaml_path)
    store = ArtifactStore()
    current = store.read_yaml(path)
    summary = current.setdefault("summary", {})
    if isinstance(summary, dict):
        summary["latest_digest"] = "data/research_framework/recent_results_digest.yaml"
        summary["latest_runs"] = [run.get("run_id") for run in _digest_runs(digest)]
    current["recent_results_digest"] = {
        "source": "framework.autonomous.recent_results_digest",
        "path": str(DEFAULT_DIGEST_PATH),
        "updated_at": digest.get("updated_at"),
        "recent_run_count": len(_digest_runs(digest)),
        "suggested_closed_families": digest.get("suggested_closed_families", []),
    }
    store.write_yaml(path, current)


def update_current_with_recent_results_digest(
    current_path: Path | str,
    digest_path: Path | str,
    digest: dict[str, Any],
) -> dict[str, Any]:
    path = Path(current_path)
    store = ArtifactStore()
    current = store.read_yaml(path)
    latest = []
    for run in _digest_runs(digest)[:5]:
        if isinstance(run, dict):
            latest.append(
                {
                    "run_id": run.get("run_id"),
                    "strategy_id": run.get("strategy_id"),
                    "verdict": run.get("verdict"),
                    "one_line_summary": run.get("one_line_summary"),
                    "closed_direction_tags": _closed_tags_from_review_row(run),
                }
            )
    current["recent_results_digest"] = {
        "path": str(Path(digest_path)),
        "updated_at": digest.get("updated_at"),
        "latest": latest,
    }
    store.write_yaml(path, current)
    return current


def _digest_runs(digest: dict[str, Any]) -> list[dict[str, Any]]:
    runs = digest.get("runs") or digest.get("recent_runs") or []
    return [run for run in runs if isinstance(run, dict)]


def _extract_verdict(review: dict[str, Any], facts: dict[str, Any]) -> Any:
    if review.get("verdict") is not None:
        return review.get("verdict")
    decision = facts.get("decision") or review.get("decision")
    if isinstance(decision, dict):
        return decision.get("verdict") or decision.get("diagnostic_verdict") or decision.get("l6_exit_decision")
    return decision


def _closed_tags(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return [str(item)] if item else []
    tags = item.get("direction_tags")
    if isinstance(tags, list):
        return [str(tag) for tag in tags if tag]
    if tags:
        return [str(tags)]
    tag = item.get("mechanic_tag") or item.get("tag") or item.get("id")
    return [str(tag)] if tag else []


def _closed_tags_from_review_row(row: dict[str, Any]) -> list[str]:
    tags = row.get("closed_direction_tags")
    if isinstance(tags, list):
        return [str(tag) for tag in tags if tag]
    return []
