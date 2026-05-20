"""Record completed research runs from source artifacts.

This module owns verdict derivation, run manifests, and experiment-registry
updates. The execution pipeline may call it, but should not decide final run
records itself.
"""

from __future__ import annotations

import csv
import hashlib
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
MANIFEST_DIR = REPO_ROOT / "data" / "research_framework" / "run_manifests"


class RunRecordError(RuntimeError):
    pass


NORMAL_RECORD_TYPE = "executed_run"
BACKFILL_RECORD_TYPE = "backfill"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RunRecordError(f"{rel(path)} YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise RunRecordError(f"{rel(path)} root must be mapping")
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunRecordError(f"{rel(path)} JSON parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise RunRecordError(f"{rel(path)} root must be object")
    return data


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_tree_hash(output_dir: Path) -> str | None:
    if not output_dir.exists():
        return None
    h = hashlib.md5()
    for path in sorted(p for p in output_dir.rglob("*") if p.is_file()):
        h.update(rel(path).encode("utf-8"))
        h.update(b"\0")
        h.update(file_md5(path).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def git_commit() -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def git_dirty() -> list[str]:
    result = subprocess.run(["git", "status", "--short"], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return ["unknown"]
    return [line for line in result.stdout.splitlines() if line.strip()]


def load_result_summary(output_dir: Path) -> tuple[dict[str, Any], str | None]:
    for name in ("run_summary.json", "summary.json"):
        path = output_dir / name
        if path.exists():
            return read_json(path), rel(path)
    csv_path = output_dir / "summary.csv"
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        selected = next((r for r in rows if str(r.get("adoption_pass", "")).lower() in {"true", "false"}), None)
        if selected is None and rows:
            selected = rows[-1]
        return (selected or {}), rel(csv_path)
    return {}, None


def boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "pass", "passed"}:
            return True
        if lowered in {"false", "0", "no", "n", "fail", "failed"}:
            return False
    return None


def derive_verdict(spec: dict[str, Any], output_dir: Path, exit_code: int | None, missing_artifacts: list[str]) -> dict[str, Any]:
    summary, summary_path = load_result_summary(output_dir)
    automation = spec.get("automation") or {}
    verdict_cfg = automation.get("verdict") if isinstance(automation, dict) else None
    if not isinstance(verdict_cfg, dict):
        verdict_cfg = {}
    table_summary = derive_table_verdict(output_dir, verdict_cfg)
    if table_summary:
        summary = {**summary, **table_summary}
        summary_path = table_summary.get("summary_path") or summary_path
    pass_field = str(verdict_cfg.get("pass_field") or "adoption_pass")
    pass_value = boolish(summary.get(pass_field))
    falsifier_result = derive_train_falsifier_flags(spec, output_dir, verdict_cfg, summary)
    summary = {**summary, **falsifier_result}
    falsifier_failed = any(
        isinstance(flag, dict) and flag.get("status") == "failed"
        for flag in falsifier_result.get("falsifier_flags", {}).values()
    )
    if exit_code not in (None, 0):
        status = "abandoned"
        decision = "execution_failed"
    elif missing_artifacts:
        status = "abandoned"
        decision = "missing_artifacts"
    elif pass_value is True and falsifier_failed:
        status = "rejected"
        decision = "passed_mechanical_but_falsifier_failed"
    elif pass_value is True:
        status = "wip"
        decision = "passed_mechanical_thresholds_not_promoted"
    elif pass_value is False:
        status = "rejected"
        decision = "failed_mechanical_thresholds"
    else:
        status = "wip"
        decision = "no_pass_field_found_needs_review"
    return {
        "status": status,
        "decision": decision,
        "pass_field": pass_field,
        "pass_value": pass_value,
        "summary_path": summary_path,
        "summary": summary,
        "falsifier_flags": falsifier_result.get("falsifier_flags", {}),
    }


def derive_table_verdict(output_dir: Path, verdict_cfg: dict[str, Any]) -> dict[str, Any]:
    table_path = verdict_cfg.get("table_path") or verdict_cfg.get("source_csv")
    if not isinstance(table_path, str) or not table_path.strip():
        return {}
    path = Path(table_path)
    if not path.is_absolute():
        path = output_dir / path
    if not path.exists():
        return {"adoption_pass": False, "summary_path": rel(path), "table_verdict_error": "table_missing"}
    with path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    filters = verdict_cfg.get("filters") or {}
    if isinstance(filters, dict):
        for key, expected in filters.items():
            rows = [r for r in rows if str(r.get(str(key), "")) == str(expected)]
    rank_by = verdict_cfg.get("rank_by")
    if isinstance(rank_by, str) and rank_by:
        reverse = bool(verdict_cfg.get("rank_desc", True))
        rows.sort(key=lambda r: numeric_or_default(r.get(rank_by), float("-inf")), reverse=reverse)
    selected = rows[0] if rows else {}
    thresholds = verdict_cfg.get("thresholds") or {}
    checks: dict[str, bool] = {}
    if isinstance(thresholds, dict):
        for field, rule in thresholds.items():
            value = numeric_or_default(selected.get(str(field)), None)
            if value is None:
                checks[str(field)] = False
                continue
            if isinstance(rule, dict):
                passed = True
                if "min" in rule:
                    passed = passed and value >= float(rule["min"])
                if "max" in rule:
                    passed = passed and value <= float(rule["max"])
                checks[str(field)] = passed
            else:
                checks[str(field)] = value >= float(rule)
    adoption_pass = all(checks.values()) if checks else None
    return {
        "adoption_pass": adoption_pass,
        "summary_path": rel(path),
        "selected_table_row": selected,
        "table_rows_considered": len(rows),
        "table_threshold_checks": checks,
    }


def derive_train_falsifier_flags(
    spec: dict[str, Any],
    output_dir: Path,
    verdict_cfg: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    flags: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    yearly_path = find_yearly_csv_path(spec, output_dir, verdict_cfg)
    selected_name = selected_candidate_name(summary)
    if yearly_path is None:
        warnings.append("yearly_csv_missing_skip_train_falsifiers")
        return {"falsifier_flags": flags, "falsifier_warnings": warnings}
    if selected_name is None:
        warnings.append("selected_candidate_missing_skip_train_falsifiers")
        return {"falsifier_flags": flags, "falsifier_warnings": warnings, "falsifier_yearly_path": rel(yearly_path)}
    with yearly_path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    selected_rows = [row for row in rows if candidate_name(row) == selected_name]
    train_rows = train_year_rows(spec, selected_rows)
    if not train_rows:
        warnings.append("train_year_rows_missing_skip_train_falsifiers")
        return {
            "falsifier_flags": flags,
            "falsifier_warnings": warnings,
            "falsifier_yearly_path": rel(yearly_path),
            "falsifier_selected_name": selected_name,
        }

    excess_values = [
        value
        for value in (numeric_or_default(row.get("excess_return"), None) for row in train_rows)
        if value is not None
    ]
    if excess_values:
        compound_excess = 1.0
        for value in excess_values:
            compound_excess *= 1.0 + value
        compound_excess -= 1.0
        flags["falsifier_train_excess"] = {
            "status": "failed" if compound_excess < 0 else "passed",
            "compound_excess_return": compound_excess,
            "years": [str(row.get("period")) for row in train_rows],
            "threshold": 0.0,
        }
    else:
        flags["falsifier_train_excess"] = {"status": "skipped", "reason": "missing_excess_return"}

    dd_ceiling = train_drawdown_ceiling(spec, verdict_cfg)
    dd_rows = [
        (row, value)
        for row in train_rows
        for value in [numeric_or_default(row.get("max_drawdown"), None)]
        if value is not None
    ]
    if dd_rows:
        worst_row, worst_dd = min(dd_rows, key=lambda item: item[1])
        flags["falsifier_single_year_dd"] = {
            "status": "failed" if worst_dd < dd_ceiling else "passed",
            "worst_year": str(worst_row.get("period")),
            "worst_max_drawdown": worst_dd,
            "ceiling": dd_ceiling,
        }
    else:
        flags["falsifier_single_year_dd"] = {"status": "skipped", "reason": "missing_max_drawdown"}

    yearly_threshold = train_yearly_excess_threshold(spec, verdict_cfg)
    if yearly_threshold is not None:
        year_values = [
            (row, value)
            for row in train_rows
            for value in [numeric_or_default(row.get("excess_return"), None)]
            if value is not None
        ]
        if year_values:
            worst_row, worst_excess = min(year_values, key=lambda item: item[1])
            flags["falsifier_single_year_excess"] = {
                "status": "failed" if worst_excess < yearly_threshold else "passed",
                "worst_year": str(worst_row.get("period")),
                "worst_excess_return": worst_excess,
                "threshold": yearly_threshold,
            }
        else:
            flags["falsifier_single_year_excess"] = {"status": "skipped", "reason": "missing_excess_return"}

    return {
        "falsifier_flags": flags,
        "falsifier_warnings": warnings,
        "falsifier_yearly_path": rel(yearly_path),
        "falsifier_selected_name": selected_name,
    }


def find_yearly_csv_path(spec: dict[str, Any], output_dir: Path, verdict_cfg: dict[str, Any]) -> Path | None:
    candidates: list[str] = []
    for key in ("yearly_path", "yearly_csv", "yearly_table_path", "train_falsifier_yearly_path"):
        value = verdict_cfg.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    artifacts = spec.get("artifacts_required") or []
    if isinstance(artifacts, list):
        candidates.extend(
            item
            for item in artifacts
            if isinstance(item, str) and Path(item).name.startswith("yearly") and item.endswith(".csv")
        )
    candidates.extend(path.name for path in sorted(output_dir.glob("yearly*.csv")))
    for item in candidates:
        path = Path(item)
        if not path.is_absolute():
            path = output_dir / path
        if path.exists():
            return path
    return None


def selected_candidate_name(summary: dict[str, Any]) -> str | None:
    selected = summary.get("selected_table_row")
    if isinstance(selected, dict):
        return candidate_name(selected)
    for key in ("selected_name", "name", "candidate"):
        value = summary.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def candidate_name(row: dict[str, Any]) -> str | None:
    for key in ("name", "candidate", "variant"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def train_year_rows(spec: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    yearly_rows = [row for row in rows if str(row.get("period", "")).isdigit()]
    if not yearly_rows:
        return []
    cv_years = {str(year) for year in spec.get("cv_holdout_years") or []}
    non_holdout = [row for row in yearly_rows if str(row.get("period")) not in cv_years]
    return non_holdout or yearly_rows


def train_drawdown_ceiling(spec: dict[str, Any], verdict_cfg: dict[str, Any]) -> float:
    for source in (verdict_cfg, spec):
        for key in ("train_single_year_dd_ceiling", "single_year_dd_ceiling", "falsifier_single_year_dd_ceiling"):
            value = source.get(key) if isinstance(source, dict) else None
            parsed = numeric_or_default(value, None)
            if parsed is not None:
                return parsed
    return -0.15


def train_yearly_excess_threshold(spec: dict[str, Any], verdict_cfg: dict[str, Any]) -> float | None:
    for source in (verdict_cfg, spec):
        for key in ("train_single_year_excess_min", "single_year_excess_min", "falsifier_single_year_excess_min"):
            value = source.get(key) if isinstance(source, dict) else None
            parsed = numeric_or_default(value, None)
            if parsed is not None:
                return parsed
    return None


def numeric_or_default(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def write_manifest(
    spec: dict[str, Any],
    spec_path: Path,
    output_dir: Path,
    command: list[str] | None,
    start_at: str | None,
    end_at: str,
    exit_code: int | None,
    compute_metadata: dict[str, Any],
    verdict: dict[str, Any],
    dry_run: bool,
) -> Path:
    run_id = str(spec["run_id"])
    path = MANIFEST_DIR / f"{run_id}.yaml"
    promotion_status = {
        "wip": "experiment",
        "rejected": "rejected",
        "abandoned": "invalidated",
        "promoted": "adopted",
        "active": "experiment",
    }.get(str(verdict["status"]), "experiment")
    manifest = {
        "schema_version": 1,
        "record_type": NORMAL_RECORD_TYPE,
        "batch_id": run_id,
        "strategy_id": spec.get("strategy_id"),
        "hypothesis_id": spec.get("hypothesis_id") or run_id,
        "data_window": spec.get("data_window") or {"start": "unknown", "end": "unknown"},
        "config_path": rel(spec_path),
        "config_hash": file_md5(spec_path) if spec_path.exists() else "unknown",
        "entrypoint": command or [],
        "git_commit": git_commit(),
        "git_dirty": git_dirty(),
        "dirty_policy": "allowed_with_list",
        "data_snapshot": {},
        "compute_host": socket.gethostname(),
        "compute_cost_yuan": compute_metadata["estimated_compute_cost_yuan"],
        "start_at": start_at,
        "end_at": end_at,
        "exit_code": exit_code,
        "result_artifact": rel(output_dir),
        "artifact_hash": artifact_tree_hash(output_dir),
        "artifact_hash_manifest": None,
        "result_summary": verdict["decision"],
        "promotion_status": promotion_status,
        "reviewer": "auto",
        "verdict_at": end_at,
        "automation": {
            "compute": compute_metadata,
            "verdict": {
                "decision": verdict["decision"],
                "pass_field": verdict["pass_field"],
                "pass_value": verdict["pass_value"],
                "summary_path": verdict["summary_path"],
                "falsifier_flags": verdict.get("falsifier_flags", {}),
            },
        },
    }
    if not dry_run:
        write_yaml(path, manifest)
    return path


def _required_artifact_paths(spec: dict[str, Any], output_dir: Path) -> list[Path]:
    artifacts = spec.get("artifacts_required") or []
    if not isinstance(artifacts, list):
        raise RunRecordError("artifacts_required must be list")
    paths: list[Path] = []
    for item in artifacts:
        if not isinstance(item, str) or not item.strip():
            continue
        path = Path(item)
        paths.append(path if path.is_absolute() else output_dir / path)
    return paths


def _missing_artifacts(spec: dict[str, Any], output_dir: Path) -> list[str]:
    return [rel(path) for path in _required_artifact_paths(spec, output_dir) if not path.exists()]


def _require_executed_run_inputs(
    *,
    spec: dict[str, Any],
    spec_path: Path,
    output_dir: Path,
    command: list[str] | None,
    start_at: str | None,
    end_at: str | None,
    exit_code: int | None,
    compute_metadata: dict[str, Any],
    data_quality_decision: dict[str, Any] | None,
) -> None:
    if not spec_path.exists():
        raise RunRecordError("executed run record requires spec_path to exist")
    if not command:
        raise RunRecordError("executed run record requires executed command")
    if not start_at or not end_at:
        raise RunRecordError("executed run record requires start_at and end_at")
    if exit_code is None:
        raise RunRecordError("executed run record requires exit_code")
    if not output_dir.exists():
        raise RunRecordError("executed run record requires output_dir to exist")
    if not isinstance(compute_metadata, dict) or "estimated_compute_cost_yuan" not in compute_metadata:
        raise RunRecordError("executed run record requires compute metadata")
    if not isinstance(data_quality_decision, dict) or str(data_quality_decision.get("status") or "").lower() != "pass":
        raise RunRecordError("executed run record requires pass data quality decision")


def record_executed_run(
    *,
    spec: dict[str, Any],
    spec_path: Path,
    output_dir: Path,
    command: list[str] | None,
    start_at: str | None,
    end_at: str | None,
    exit_code: int | None,
    compute_metadata: dict[str, Any],
    data_quality_decision: dict[str, Any] | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Normal record entry. Only call after an execution attempt."""
    _require_executed_run_inputs(
        spec=spec,
        spec_path=spec_path,
        output_dir=output_dir,
        command=command,
        start_at=start_at,
        end_at=end_at,
        exit_code=exit_code,
        compute_metadata=compute_metadata,
        data_quality_decision=data_quality_decision,
    )
    missing = _missing_artifacts(spec, output_dir)
    verdict = derive_verdict(spec, output_dir, exit_code, missing)
    verdict["missing_artifacts"] = missing
    manifest_path = write_manifest(
        spec=spec,
        spec_path=spec_path,
        output_dir=output_dir,
        command=command,
        start_at=start_at,
        end_at=end_at or now_iso(),
        exit_code=exit_code,
        compute_metadata=compute_metadata,
        verdict=verdict,
        dry_run=dry_run,
    )
    if not dry_run:
        manifest = read_yaml(manifest_path)
        manifest["record_type"] = NORMAL_RECORD_TYPE
        manifest["data_quality_decision"] = data_quality_decision
        write_yaml(manifest_path, manifest)
    update_experiments(spec, output_dir, manifest_path, verdict, compute_metadata, dry_run=dry_run)
    return {"record_type": NORMAL_RECORD_TYPE, "verdict": verdict, "manifest_path": manifest_path}


def backfill_run_record(
    *,
    spec: dict[str, Any],
    spec_path: Path,
    output_dir: Path,
    reason: str,
    actor: str,
    evidence_paths: list[str],
    compute_metadata: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Historical record entry. It never represents a newly executed run."""
    if not reason.strip():
        raise RunRecordError("backfill record requires reason")
    if not actor.strip():
        raise RunRecordError("backfill record requires actor")
    if not evidence_paths:
        raise RunRecordError("backfill record requires evidence_paths")
    missing = _missing_artifacts(spec, output_dir)
    verdict = derive_verdict(spec, output_dir, None, missing)
    verdict["missing_artifacts"] = missing
    compute = compute_metadata or {"estimated_compute_cost_yuan": 0.0, "decision": "backfill_record_only"}
    manifest_path = write_manifest(
        spec=spec,
        spec_path=spec_path,
        output_dir=output_dir,
        command=[],
        start_at=None,
        end_at=now_iso(),
        exit_code=None,
        compute_metadata=compute,
        verdict=verdict,
        dry_run=dry_run,
    )
    if not dry_run:
        manifest = read_yaml(manifest_path)
        manifest["record_type"] = BACKFILL_RECORD_TYPE
        manifest["backfill"] = {
            "reason": reason,
            "actor": actor,
            "evidence_paths": evidence_paths,
            "new_execution": False,
            "may_trigger_next_research": False,
            "may_update_current_strategy": False,
        }
        write_yaml(manifest_path, manifest)
    update_experiments(spec, output_dir, manifest_path, verdict, compute, dry_run=dry_run)
    return {"record_type": BACKFILL_RECORD_TYPE, "verdict": verdict, "manifest_path": manifest_path}


def update_experiments(
    spec: dict[str, Any],
    output_dir: Path,
    manifest_path: Path,
    verdict: dict[str, Any],
    compute_metadata: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    data = read_yaml(EXPERIMENTS) if EXPERIMENTS.exists() else {"schema_version": 1, "experiments": []}
    experiments = data.setdefault("experiments", [])
    if not isinstance(experiments, list):
        raise RunRecordError("experiments.yaml experiments must be list")
    run_id = str(spec["run_id"])
    row = {
        "id": run_id,
        "strategy_id": spec.get("strategy_id"),
        "hypothesis_id": spec.get("hypothesis_id") or run_id,
        "branch": spec.get("branch") or "auto",
        "status": verdict["status"],
        "summary": str(spec.get("hypothesis") or "")[:300],
        "key_metrics": compact_metrics(verdict["summary"]),
        "artifacts": [rel(output_dir), rel(manifest_path)],
        "affects_current_strategy": False,
        "current_strategy_effect": "auto-run record only; does not promote current strategy",
        "automation": {
            "decision": verdict["decision"],
            "compute": compute_metadata,
            "updated_at": now_iso(),
        },
    }
    for idx, item in enumerate(experiments):
        if isinstance(item, dict) and item.get("id") == run_id:
            experiments[idx] = {**item, **row}
            break
    else:
        experiments.append(row)
    data["updated_at"] = now_iso()
    write_yaml(EXPERIMENTS, data)


def compact_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "adoption_pass",
        "selected_passes",
        "selected_total",
        "candidate_count",
        "task_count",
        "compounded_yearly_excess_return",
        "simple_sum_yearly_excess_return",
        "hdrf_full_oos_minus_loop_saved_excess",
        "falsifier_flags",
        "falsifier_warnings",
        "falsifier_yearly_path",
        "falsifier_selected_name",
    )
    out = {key: summary[key] for key in keys if key in summary}
    if isinstance(summary.get("selected_table_row"), dict):
        selected = summary["selected_table_row"]
        out["selected_name"] = selected.get("name") or selected.get("candidate")
        out["selected_period"] = selected.get("period")
        for key in ("excess_return", "total_return", "max_drawdown", "score", "total_trades", "win_rate"):
            if key in selected:
                out[key] = selected[key]
    return out
