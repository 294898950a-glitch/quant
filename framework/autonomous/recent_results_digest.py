"""Recent-results digest built only from review.yaml files."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.artifacts import ArtifactStore
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from artifacts import ArtifactStore  # type: ignore


def _review_files(review_dir: Path) -> list[Path]:
    return sorted(Path(review_dir).glob("*review.yaml"))


def build_digest(review_dir: Path, last_n: int = 5) -> dict[str, Any]:
    reviews = []
    closed_counter: Counter[str] = Counter()
    for path in _review_files(Path(review_dir))[-last_n:]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        facts = data.get("facts") or {}
        run = {
            "run_id": data.get("run_id") or facts.get("run_id") or path.stem.replace("_review", ""),
            "strategy_id": data.get("strategy_id") or facts.get("strategy_id"),
            "decision": facts.get("decision"),
            "facts_hash": facts.get("artifacts_hash") or facts.get("facts_hash"),
        }
        reviews.append(run)
        for item in data.get("closed_directions", []) or []:
            tag = item.get("mechanic_tag")
            if tag:
                closed_counter[tag] += 1
    return {
        "schema_version": 1,
        "source": "review_yaml_only",
        "recent_runs": reviews,
        "suggested_closed_families": [
            {"tag": tag, "reject_count": count}
            for tag, count in sorted(closed_counter.items())
            if count >= 3
        ],
    }


def update_current_pointer(digest: dict[str, Any], current_yaml_path: Path) -> None:
    path = Path(current_yaml_path)
    store = ArtifactStore()
    current = store.read_yaml(path)
    summary = current.setdefault("summary", {})
    if isinstance(summary, dict):
        summary["latest_digest"] = "data/research_framework/recent_results_digest.yaml"
        summary["latest_runs"] = [run.get("run_id") for run in digest.get("recent_runs", [])]
    current["recent_results_digest"] = {
        "source": "framework.autonomous.recent_results_digest",
        "recent_run_count": len(digest.get("recent_runs", [])),
        "suggested_closed_families": digest.get("suggested_closed_families", []),
    }
    store.write_yaml(path, current)
