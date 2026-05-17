from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DIGEST_SCHEMA_VERSION = 1
DEFAULT_DIGEST_PATH = Path("data/research_framework/recent_results_digest.yaml")
DEFAULT_CURRENT_PATH = Path("data/research_framework/current.yaml")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            payload,
            handle,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def discover_review_paths(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return sorted(path for path in data_dir.glob("*/review.yaml") if path.is_file())


def build_recent_results_digest(
    data_dir: Path | str = Path("data"),
    limit: int = 10,
    updated_at: str | None = None,
) -> dict[str, Any]:
    data_path = Path(data_dir)
    review_paths = discover_review_paths(data_path)
    review_paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    rows = [_digest_run_from_review(path, data_path) for path in review_paths]

    if limit >= 0:
        rows = rows[:limit]

    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "updated_at": updated_at or utc_now_iso(),
        "source_reviews_count": len(review_paths),
        "runs": rows,
    }


def write_recent_results_digest(
    output_path: Path | str = DEFAULT_DIGEST_PATH,
    data_dir: Path | str = Path("data"),
    limit: int = 10,
    updated_at: str | None = None,
) -> dict[str, Any]:
    digest = build_recent_results_digest(data_dir=data_dir, limit=limit, updated_at=updated_at)
    write_yaml(Path(output_path), digest)
    return digest


def update_current_with_recent_results_digest(
    current_path: Path | str,
    digest_path: Path | str,
    digest: dict[str, Any],
) -> dict[str, Any]:
    current_file = Path(current_path)
    current = load_yaml(current_file)
    latest = []
    for run in digest.get("runs", [])[:5]:
        if isinstance(run, dict):
            latest.append(
                {
                    "run_id": run.get("run_id"),
                    "strategy_id": run.get("strategy_id"),
                    "verdict": run.get("verdict"),
                    "one_line_summary": run.get("one_line_summary"),
                    "closed_direction_tags": run.get("closed_direction_tags", []),
                }
            )

    current["recent_results_digest"] = {
        "path": str(Path(digest_path)),
        "updated_at": digest.get("updated_at"),
        "latest": latest,
    }
    write_yaml(current_file, current)
    return current


def _digest_run_from_review(path: Path, data_dir: Path) -> dict[str, Any]:
    review = load_yaml(path)
    facts = _mapping_value(review, "facts")
    experiment = _mapping_value(facts, "experiment")
    review_root = path.parent
    run_id = (
        _first_present(review, "run_id", "id", "experiment_id")
        or _first_present(experiment, "run_id", "id", "experiment_id")
        or review_root.name
    )

    return {
        "run_id": run_id,
        "strategy_id": _first_present(review, "strategy_id", "strategy")
        or _first_present(experiment, "strategy_id", "strategy"),
        "review_status": _first_present(review, "review_status", "status"),
        "decision": _extract_decision(review),
        "verdict": _extract_verdict(review),
        "one_line_summary": _extract_one_line_summary(review),
        "key_metrics": _extract_key_metrics(review),
        "closed_direction_tags": _extract_closed_direction_tags(review),
        "next_direction_ids": _extract_next_direction_ids(review),
        "review_path": _relative_path(path, data_dir.parent),
    }


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _extract_decision(review: dict[str, Any]) -> Any:
    facts = _mapping_value(review, "facts")
    decision = review.get("decision", facts.get("decision"))
    if isinstance(decision, dict):
        return (
            _first_present(decision, "decision", "l6_exit_decision", "status", "name", "id")
            or decision
        )
    return decision


def _extract_verdict(review: dict[str, Any]) -> Any:
    verdict = review.get("verdict")
    if verdict is not None:
        return verdict
    facts = _mapping_value(review, "facts")
    decision = review.get("decision", facts.get("decision"))
    if isinstance(decision, dict):
        return _first_present(decision, "verdict", "diagnostic_verdict", "l6_exit_decision")
    return None


def _extract_one_line_summary(review: dict[str, Any]) -> Any:
    summary = _first_present(review, "one_line_summary", "summary", "one_line")
    if summary is None:
        materials = _mapping_value(review, "materials_for_ideator")
        interpretation = _mapping_value(review, "interpretation")
        summary = _first_present(
            materials,
            "one_line_summary",
            "summary",
            "one_line",
        ) or _first_present(interpretation, "machine_summary", "summary")
    if isinstance(summary, dict):
        return _first_present(summary, "one_line", "text", "summary")
    return summary


def _extract_mapping(review: dict[str, Any], *keys: str) -> dict[str, Any]:
    value = _first_present(review, *keys)
    if value is None:
        facts = _mapping_value(review, "facts")
        value = _first_present(facts, *keys)
    return value if isinstance(value, dict) else {}


def _extract_key_metrics(review: dict[str, Any]) -> dict[str, Any]:
    metrics = _extract_mapping(review, "key_metrics", "metrics", "summary_metrics")
    return {
        key: value
        for key, value in metrics.items()
        if key not in {"csv_artifacts", "source_artifacts", "sample_rows"}
    }


def _extract_closed_direction_tags(review: dict[str, Any]) -> list[Any]:
    value = _first_present(review, "closed_direction_tags", "closed_tags")
    if value is None:
        materials = _mapping_value(review, "materials_for_ideator")
        value = materials.get("do_not_repeat_tags")
    if value is None:
        value = review.get("closed_directions")
    if isinstance(value, list):
        tags = []
        for item in value:
            if isinstance(item, dict):
                direction_tags = item.get("direction_tags")
                if isinstance(direction_tags, list):
                    tags.extend(direction_tags)
                elif direction_tags:
                    tags.append(direction_tags)
                else:
                    tags.append(_first_present(item, "tag", "id", "direction_id", "run_id") or item)
            else:
                tags.append(item)
        return _dedupe(tags)
    return _ids_from_list_value(value)


def _extract_next_direction_ids(review: dict[str, Any]) -> list[Any]:
    value = _first_present(review, "next_direction_ids", "next_ids")
    if value is None:
        value = review.get("next_directions")
    return _ids_from_list_value(value)


def _ids_from_list_value(value: Any) -> list[Any]:
    items = value if isinstance(value, list) else _list_or_empty(value)
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(_first_present(item, "tag", "id", "direction_id", "run_id") or item)
        else:
            result.append(item)
    return result


def _dedupe(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        key = str(value)
        if key in seen:
            continue
        result.append(value)
        seen.add(key)
    return result


def _list_or_empty(value: Any) -> list[Any]:
    if value is None:
        return []
    return [value]


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _mapping_value(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}
