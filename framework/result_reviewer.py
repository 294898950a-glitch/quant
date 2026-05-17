from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


FACTS_EXTRACTOR_VERSION = "result_reviewer_v0.1"
CORE_ARTIFACTS = ("spec.yaml", "summary.json", "report.yaml", "diagnostic.yaml")
METRIC_KEYS = (
    "total_return",
    "excess_return",
    "max_drawdown",
    "win_rate",
    "total_trades",
    "n_days",
    "score",
)
SUMMARY_BLOCKS = (
    "best_train",
    "best_test",
    "selected_test",
    "baseline_train",
    "baseline_test",
    "baseline_2020",
    "selected_2020",
)


class _NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def review_run(run_dir: str | Path) -> dict[str, Any]:
    run_path = Path(run_dir)
    existing_artifacts = _find_artifacts(run_path)
    source_artifacts = [_artifact_record(path, run_path) for path in existing_artifacts]
    missing_core = [name for name in CORE_ARTIFACTS if not (run_path / name).is_file()]

    loaded: dict[str, Any] = {}
    parse_errors: list[dict[str, str]] = []
    for name in CORE_ARTIFACTS:
        path = run_path / name
        if not path.is_file():
            continue
        try:
            loaded[name] = _load_artifact(path)
        except Exception as exc:  # noqa: BLE001 - reviewer must not crash on bad artifacts.
            parse_errors.append({"path": _rel_path(path, run_path), "error": str(exc)})

    csv_facts = _read_csv_facts(run_path)
    spec = _as_dict(loaded.get("spec.yaml"))
    summary = _as_dict(loaded.get("summary.json"))
    report = _as_dict(loaded.get("report.yaml"))
    diagnostic = _as_dict(loaded.get("diagnostic.yaml"))

    decision = _extract_decision(report, diagnostic, summary)
    review_status = _review_status(missing_core, parse_errors, decision)
    experiment = _extract_experiment(spec, summary, report, diagnostic, run_path)
    key_metrics = _extract_key_metrics(summary)
    falsifiers = _extract_falsifiers(spec, summary, report, diagnostic)
    closed_directions = _extract_closed_directions(spec, report, diagnostic, experiment)
    one_line_summary = _one_line_summary(experiment, decision, key_metrics, review_status)

    review = {
        "schema_version": 1,
        "run_id": experiment.get("run_id") or run_path.name,
        "strategy_id": experiment.get("strategy_id"),
        "reviewed_at": _utc_now_iso(),
        "reviewer": "code",
        "reviewer_version": FACTS_EXTRACTOR_VERSION,
        "review_status": review_status,
        "facts": {
            "source_artifacts": source_artifacts,
            "missing_artifacts": missing_core,
            "parse_errors": parse_errors,
            "experiment": experiment,
            "key_metrics": key_metrics,
            "csv_artifacts": csv_facts,
            "falsifiers": falsifiers,
            "decision": decision,
        },
        "interpretation": {
            "pending_ai_review": True,
            "machine_summary": one_line_summary,
        },
        "verification": {
            "used": False,
            "rounds_used": 0,
        },
        "closed_directions": closed_directions,
        "next_directions": [],
        "materials_for_ideator": {
            "one_line_summary": one_line_summary,
            "useful_facts": _useful_facts(experiment, decision, key_metrics, falsifiers),
            "open_questions": _open_questions(review_status, missing_core, parse_errors, decision),
            "suggested_mechanics": [],
            "do_not_repeat_tags": _do_not_repeat_tags(closed_directions),
            "related_runs": _related_runs(spec, summary, report, diagnostic, run_path.name),
        },
        "provenance": {
            "facts_extractor_version": FACTS_EXTRACTOR_VERSION,
            "artifact_bundle_hash": _artifact_bundle_hash(source_artifacts),
        },
    }
    return review


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_review(run_dir: str | Path) -> Path:
    run_path = Path(run_dir)
    review = review_run(run_path)
    output_path = run_path / "review.yaml"
    output_path.write_text(
        yaml.dump(review, Dumper=_NoAliasSafeDumper, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


def _find_artifacts(run_path: Path) -> list[Path]:
    if not run_path.is_dir():
        return []
    names = set(CORE_ARTIFACTS)
    for pattern in ("summary_*.csv", "yearly_*.csv"):
        names.update(path.name for path in run_path.glob(pattern) if path.is_file())
    return [run_path / name for name in sorted(names) if (run_path / name).is_file()]


def _load_artifact(path: Path) -> Any:
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _artifact_record(path: Path, run_path: Path) -> dict[str, str]:
    return {
        "path": _rel_path(path, run_path),
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_bundle_hash(source_artifacts: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for artifact in sorted(source_artifacts, key=lambda item: item["path"]):
        digest.update(artifact["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifact["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _read_csv_facts(run_path: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if not run_path.is_dir():
        return facts
    for pattern in ("summary_*.csv", "yearly_*.csv"):
        for path in sorted(run_path.glob(pattern)):
            if not path.is_file():
                continue
            facts[path.name] = _csv_profile(path)
    return facts


def _csv_profile(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        sample_rows = []
        row_count = 0
        for row in reader:
            row_count += 1
            if len(sample_rows) < 3:
                sample_rows.append(_coerce_row(row))
    profile: dict[str, Any] = {
        "rows": row_count,
        "columns": reader.fieldnames or [],
    }
    if sample_rows:
        profile["sample_rows"] = sample_rows
    return profile


def _coerce_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _coerce_scalar(value) for key, value in row.items()}


def _coerce_scalar(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped == "":
        return ""
    try:
        if "." not in stripped and "e" not in stripped.lower():
            return int(stripped)
        return float(stripped)
    except ValueError:
        return value


def _extract_experiment(
    spec: dict[str, Any],
    summary: dict[str, Any],
    report: dict[str, Any],
    diagnostic: dict[str, Any],
    run_path: Path,
) -> dict[str, Any]:
    return _drop_empty(
        {
            "run_id": _first_value("run_id", spec, summary, report, diagnostic) or run_path.name,
            "date": _first_value("date", spec, report) or diagnostic.get("diagnostic_date"),
            "strategy_id": spec.get("strategy_id"),
            "hypothesis": spec.get("hypothesis"),
            "status": _first_value("status", summary, report, spec),
            "candidate_count": summary.get("candidate_count"),
            "adoption_pass": summary.get("adoption_pass"),
            "cv_design": spec.get("cv_design"),
            "cv_holdout_years": spec.get("cv_holdout_years"),
        }
    )


def _extract_key_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for block in SUMMARY_BLOCKS:
        if isinstance(summary.get(block), dict):
            metrics[block] = _metric_row(summary[block])
    for scalar_key in ("candidate_count", "adoption_pass", "status"):
        if scalar_key in summary:
            metrics[scalar_key] = summary[scalar_key]
    return metrics


def _metric_row(row: dict[str, Any]) -> dict[str, Any]:
    selected = {
        "name": row.get("name"),
        "description": row.get("description"),
        "period": row.get("period"),
        "start": row.get("start"),
        "end": row.get("end"),
    }
    for key in METRIC_KEYS:
        if key in row:
            selected[key] = row[key]
    return _drop_empty(selected)


def _extract_falsifiers(
    spec: dict[str, Any],
    summary: dict[str, Any],
    report: dict[str, Any],
    diagnostic: dict[str, Any],
) -> dict[str, Any]:
    return _drop_empty(
        {
            "hard_floors": spec.get("hard_floors"),
            "hard_floors_baseline_source": spec.get("hard_floors_baseline_source"),
            "stop_conditions": spec.get("stop_conditions"),
            "adoption_pass": summary.get("adoption_pass"),
            "report_summary": report.get("summary"),
            "diagnostic_verdict_rationale": diagnostic.get("verdict_rationale"),
        }
    )


def _extract_decision(
    report: dict[str, Any],
    diagnostic: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return _drop_empty(
        {
            "l6_exit_decision": report.get("l6_exit_decision"),
            "report_status": report.get("status"),
            "report_summary": report.get("summary"),
            "diagnostic_verdict": diagnostic.get("verdict_referenced"),
            "diagnostic_summary": diagnostic.get("summary"),
            "adoption_pass": summary.get("adoption_pass"),
        }
    )


def _extract_closed_directions(
    spec: dict[str, Any],
    report: dict[str, Any],
    diagnostic: dict[str, Any],
    experiment: dict[str, Any],
) -> list[dict[str, Any]]:
    verdict = report.get("l6_exit_decision") or diagnostic.get("verdict_referenced")
    if str(verdict).lower() != "reject":
        return []
    direction_tags = _direction_tags_from_spec(spec)
    return [
        _drop_empty(
            {
                "run_id": experiment.get("run_id"),
                "strategy_id": experiment.get("strategy_id"),
                "decision": "reject",
                "direction_tags": direction_tags,
                "reason": report.get("summary") or diagnostic.get("verdict_rationale"),
                "source": "report.yaml",
            }
        )
    ]


def _direction_tags_from_spec(spec: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    ideation = _as_dict(spec.get("ideation"))
    family = ideation.get("family")
    if family:
        tags.append(str(family))

    parameter_names = {
        str(item.get("name"))
        for item in spec.get("parameter_space", [])
        if isinstance(item, dict) and item.get("name")
    }
    if "position_scale_variant" in parameter_names:
        tags.append("static_candidate_position_scale")
    if "close_to_bond_floor_threshold" in parameter_names:
        tags.append("close_to_bond_floor_threshold")
    if "moneyness_stock_to_conv_threshold" in parameter_names:
        tags.append("moneyness_stock_to_conv_threshold")

    seen = set()
    unique_tags = []
    for tag in tags:
        if tag not in seen:
            unique_tags.append(tag)
            seen.add(tag)
    return unique_tags


def _one_line_summary(
    experiment: dict[str, Any],
    decision: dict[str, Any],
    key_metrics: dict[str, Any],
    review_status: str,
) -> str:
    run_id = experiment.get("run_id", "unknown_run")
    verdict = decision.get("l6_exit_decision") or decision.get("diagnostic_verdict")
    report_summary = decision.get("report_summary") or decision.get("diagnostic_summary")
    best_train = _named_metric(key_metrics.get("best_train"))
    best_test = _named_metric(key_metrics.get("best_test"))
    parts = [f"{run_id}: review_status={review_status}"]
    if verdict:
        parts.append(f"decision={verdict}")
    if report_summary:
        parts.append(str(report_summary))
    elif best_train or best_test:
        parts.append(", ".join(part for part in (best_train, best_test) if part))
    return "; ".join(parts)


def _named_metric(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    name = value.get("name")
    period = value.get("period")
    excess = value.get("excess_return")
    max_dd = value.get("max_drawdown")
    return ", ".join(
        str(part)
        for part in (
            f"{period}:{name}" if period and name else name,
            f"excess={excess}" if excess is not None else None,
            f"max_dd={max_dd}" if max_dd is not None else None,
        )
        if part
    )


def _useful_facts(
    experiment: dict[str, Any],
    decision: dict[str, Any],
    key_metrics: dict[str, Any],
    falsifiers: dict[str, Any],
) -> list[Any]:
    facts: list[Any] = []
    for key in ("strategy_id", "hypothesis", "candidate_count", "adoption_pass"):
        value = experiment.get(key) if key in experiment else key_metrics.get(key)
        if value is not None:
            facts.append({key: value})
    for block in ("best_train", "best_test", "selected_test", "baseline_test", "selected_2020"):
        if block in key_metrics:
            facts.append({block: key_metrics[block]})
    if decision:
        facts.append({"decision": decision})
    if falsifiers.get("hard_floors"):
        facts.append({"hard_floors": falsifiers["hard_floors"]})
    return facts


def _open_questions(
    review_status: str,
    missing_core: list[str],
    parse_errors: list[dict[str, str]],
    decision: dict[str, Any],
) -> list[str]:
    questions: list[str] = []
    if missing_core:
        questions.append("Core artifacts are missing; rerun or recover the run output before relying on this review.")
    if parse_errors:
        questions.append("Some core artifacts failed to parse; inspect parse_errors before using the facts.")
    if review_status == "inconclusive" or not decision:
        questions.append("No machine-readable decision was found in report.yaml or diagnostic.yaml.")
    return questions


def _do_not_repeat_tags(closed_directions: list[dict[str, Any]]) -> list[str]:
    tags: list[str] = []
    for item in closed_directions:
        direction_tags = item.get("direction_tags")
        if isinstance(direction_tags, list):
            tags.extend(str(tag) for tag in direction_tags if tag)
        elif direction_tags:
            tags.append(str(direction_tags))
        elif item.get("run_id"):
            tags.append(str(item["run_id"]))
    return tags


def _related_runs(
    spec: dict[str, Any],
    summary: dict[str, Any],
    report: dict[str, Any],
    diagnostic: dict[str, Any],
    fallback_run_id: str,
) -> list[str]:
    run_ids = []
    for data in (spec, summary, report, diagnostic):
        run_id = data.get("run_id")
        if run_id and run_id not in run_ids:
            run_ids.append(str(run_id))
    if not run_ids:
        run_ids.append(fallback_run_id)
    return run_ids


def _review_status(
    missing_core: list[str],
    parse_errors: list[dict[str, str]],
    decision: dict[str, Any],
) -> str:
    if missing_core or parse_errors:
        return "invalid_artifacts"
    if not decision:
        return "inconclusive"
    return "complete"


def _first_value(key: str, *dicts: dict[str, Any]) -> Any:
    for data in dicts:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in (None, [], {})}


def _rel_path(path: Path, run_path: Path) -> str:
    return str(path)
