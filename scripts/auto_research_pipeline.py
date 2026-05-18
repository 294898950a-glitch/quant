#!/usr/bin/env python3
"""Mechanical research pipeline runner.

This script automates the parts that should not require human or LLM judgment:

1. load a filled spec.yaml
2. make a mechanical budget decision
3. run GateKeeper preflight
4. execute the spec-declared command
5. verify required artifacts
6. derive a pass/fail verdict from machine-readable outputs
7. write run_manifest + experiments.yaml records

It does not choose research directions, promote live strategies, change protocol
rules, or revive rejected directions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.gatekeeper import GateKeeper, GateKeeperError


BUDGET_CONFIG = REPO_ROOT / "data" / "research_framework" / "compute_budget_config.json"
EXPERIMENTS = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
MANIFEST_DIR = REPO_ROOT / "data" / "research_framework" / "run_manifests"
SPEC_STATUSES_RUNNABLE = {"READY", "RUNNING"}


class PipelineError(RuntimeError):
    pass


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
        raise PipelineError(f"{rel(path)} YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError(f"{rel(path)} root must be mapping")
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{rel(path)} JSON parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError(f"{rel(path)} root must be object")
    return data


def load_budget_config() -> dict[str, Any]:
    data = json.loads(BUDGET_CONFIG.read_text(encoding="utf-8"))
    return {
        "auto_approve_limit_yuan": float(data["auto_approve_limit_yuan"]),
        "spot_yuan_per_hour": float(data["spot_yuan_per_hour"]),
        "sig_yuan_per_hour": float(data["sig_yuan_per_hour"]),
        "local_yuan_per_hour": float(data["local_yuan_per_hour"]),
        "safety_multiplier": float(data["safety_multiplier"]),
        "spot_allowed_hosts": list(data.get("spot_allowed_hosts") or []),
    }


def estimate_budget(spec: dict[str, Any]) -> dict[str, Any]:
    cfg = load_budget_config()
    estimate = spec.get("compute_estimate") or {}
    if not isinstance(estimate, dict):
        raise PipelineError("spec.compute_estimate must be mapping")
    sig_minutes = float(estimate.get("sig_minutes", 0) or 0)
    spot_minutes = float(estimate.get("spot_minutes", 0) or 0)
    local_minutes = float(estimate.get("local_minutes", 0) or 0)
    fixed_yuan = float(estimate.get("estimated_cost_yuan", 0) or 0)
    raw = (
        sig_minutes / 60 * cfg["sig_yuan_per_hour"]
        + spot_minutes / 60 * cfg["spot_yuan_per_hour"]
        + local_minutes / 60 * cfg["local_yuan_per_hour"]
        + fixed_yuan
    )
    budget_cap = float(spec.get("budget_cap_yuan", 0) or 0)
    estimated = round(raw * cfg["safety_multiplier"], 2)
    limit = min(cfg["auto_approve_limit_yuan"], budget_cap if budget_cap > 0 else cfg["auto_approve_limit_yuan"])
    return {
        "estimated_budget_yuan": estimated,
        "auto_limit_yuan": limit,
        "decision": "auto-approve" if estimated <= limit else "wait-user",
        "inputs": {
            "sig_minutes": sig_minutes,
            "spot_minutes": spot_minutes,
            "local_minutes": local_minutes,
            "fixed_yuan": fixed_yuan,
            "safety_multiplier": cfg["safety_multiplier"],
        },
    }


def enforce_compute_placement(spec: dict[str, Any], budget: dict[str, Any], dry_run: bool, no_execute: bool) -> None:
    if dry_run or no_execute:
        return
    estimate = spec.get("compute_estimate") or {}
    if not isinstance(estimate, dict):
        return
    spot_minutes = float(estimate.get("spot_minutes", 0) or 0)
    if spot_minutes <= 0 and not requires_spot_compute(spec):
        return
    allowed = set(load_budget_config().get("spot_allowed_hosts") or [])
    host = socket.gethostname()
    if host not in allowed:
        reason = f"spot_minutes={spot_minutes:g}" if spot_minutes > 0 else "cb_arb backtest command"
        raise PipelineError(
            f"compute placement violation: {reason} requires VM/spot host "
            f"in {sorted(allowed)}, current host is {host!r}"
        )


def automation_command_tokens(spec: dict[str, Any]) -> list[str]:
    automation = spec.get("automation") or spec.get("execution") or {}
    if not isinstance(automation, dict):
        return []
    command = automation.get("command") or []
    if isinstance(command, str):
        return [command]
    if isinstance(command, list):
        return [str(item) for item in command]
    return []


def requires_spot_compute(spec: dict[str, Any]) -> bool:
    command_text = " ".join(automation_command_tokens(spec))
    if any(
        marker in command_text
        for marker in (
            "scripts/evaluate_cb_arb",
            "scripts/search_cb_arb",
            "scripts/run_cb_arb",
        )
    ):
        return True
    strategy_id = str(spec.get("strategy_id") or "")
    grid = spec.get("grid") or {}
    candidate_count = grid.get("candidates_count") if isinstance(grid, dict) else 0
    return strategy_id.startswith("cb_arb") and isinstance(candidate_count, int) and candidate_count > 0


def ensure_runnable_status(spec: dict[str, Any], allow_archived: bool) -> None:
    status = str(spec.get("status") or "")
    if status in SPEC_STATUSES_RUNNABLE:
        return
    if allow_archived and status in {"COMPLETE", "ARCHIVED"}:
        return
    raise PipelineError(f"spec.status={status!r} is not runnable; expected READY/RUNNING")


def command_from_spec(spec: dict[str, Any], spec_path: Path, output_dir: Path) -> list[str]:
    automation = spec.get("automation") or spec.get("execution") or {}
    if not isinstance(automation, dict):
        raise PipelineError("spec.automation must be mapping when present")
    command = automation.get("command")
    if command is None:
        raise PipelineError("spec missing automation.command; code cannot infer how to run this research")
    if isinstance(command, str):
        parts = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(x, str) for x in command):
        parts = list(command)
    else:
        raise PipelineError("automation.command must be string or list of strings")
    replacements = {
        "{run_id}": str(spec.get("run_id")),
        "{spec_path}": rel(spec_path),
        "{output_dir}": rel(output_dir),
    }
    resolved = [replace_placeholders(part, replacements) for part in parts]
    if resolved and resolved[0] == ".venv/bin/python" and not (REPO_ROOT / resolved[0]).exists():
        resolved[0] = sys.executable or "python3"
    return resolved


def replace_placeholders(value: str, replacements: dict[str, str]) -> str:
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    return value


def output_dir_from_spec(spec: dict[str, Any], spec_path: Path) -> Path:
    automation = spec.get("automation") or spec.get("execution") or {}
    output = automation.get("output_dir") if isinstance(automation, dict) else None
    if isinstance(output, str) and output.strip():
        path = Path(output)
        return path if path.is_absolute() else REPO_ROOT / path
    return spec_path.parent


def run_command(command: list[str], log_path: Path, dry_run: bool) -> int | None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return None
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(shlex.quote(x) for x in command) + "\n\n")
        log.flush()
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return result.returncode


def required_artifact_paths(spec: dict[str, Any], output_dir: Path) -> list[Path]:
    artifacts = spec.get("artifacts_required") or []
    if not isinstance(artifacts, list):
        raise PipelineError("artifacts_required must be list")
    out: list[Path] = []
    for item in artifacts:
        if not isinstance(item, str) or not item.strip():
            continue
        path = Path(item)
        out.append(path if path.is_absolute() else output_dir / path)
    return out


def verify_artifacts(spec: dict[str, Any], output_dir: Path) -> list[str]:
    missing = []
    for path in required_artifact_paths(spec, output_dir):
        if not path.exists():
            missing.append(rel(path))
    return missing


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
        return {
            "adoption_pass": False,
            "summary_path": rel(path),
            "table_verdict_error": "table_missing",
        }
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
        return {
            "falsifier_flags": flags,
            "falsifier_warnings": warnings,
            "falsifier_yearly_path": rel(yearly_path),
        }
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


def set_spec_status(spec_path: Path, status: str, dry_run: bool) -> None:
    if dry_run:
        return
    spec = read_yaml(spec_path)
    original_status = str(spec.get("status") or "")
    spec["status"] = status
    spec["updated_at"] = now_iso()
    write_yaml(spec_path, spec)


def write_manifest(
    spec: dict[str, Any],
    spec_path: Path,
    output_dir: Path,
    command: list[str] | None,
    start_at: str | None,
    end_at: str,
    exit_code: int | None,
    budget: dict[str, Any],
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
        "compute_cost_yuan": budget["estimated_budget_yuan"],
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
            "budget": budget,
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


def update_experiments(
    spec: dict[str, Any],
    output_dir: Path,
    manifest_path: Path,
    verdict: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    data = read_yaml(EXPERIMENTS) if EXPERIMENTS.exists() else {"schema_version": 1, "experiments": []}
    experiments = data.setdefault("experiments", [])
    if not isinstance(experiments, list):
        raise PipelineError("experiments.yaml experiments must be list")
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
            "budget": budget,
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


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    spec_path = args.spec
    if not spec_path.is_absolute():
        spec_path = REPO_ROOT / spec_path
    spec = read_yaml(spec_path)
    original_status = str(spec.get("status") or "")
    ensure_runnable_status(spec, args.allow_archived)
    output_dir = output_dir_from_spec(spec, spec_path)
    budget = estimate_budget(spec)
    enforce_compute_placement(spec, budget, args.dry_run, args.no_execute)
    if budget["decision"] != "auto-approve" and not args.force_budget:
        raise PipelineError(f"budget decision is {budget['decision']}; user approval required")

    command: list[str] | None = None
    if not args.no_execute:
        command = command_from_spec(spec, spec_path, output_dir)

    if args.dry_run:
        return {
            "dry_run": True,
            "spec": rel(spec_path),
            "output_dir": rel(output_dir),
            "budget": budget,
            "command": command,
        }

    if not args.no_execute:
        gate = GateKeeper(quiet=args.quiet)
        gate.before_run_grid(spec_path)
        set_spec_status(spec_path, "RUNNING", dry_run=False)
        start_at = now_iso()
        log_path = output_dir / "auto_pipeline.log"
        exit_code = run_command(command or [], log_path, dry_run=False)
    else:
        start_at = None
        exit_code = 0

    end_at = now_iso()
    missing = verify_artifacts(spec, output_dir)
    verdict = derive_verdict(spec, output_dir, exit_code, missing)
    verdict["missing_artifacts"] = missing
    manifest_path = write_manifest(
        spec=spec,
        spec_path=spec_path,
        output_dir=output_dir,
        command=command,
        start_at=start_at,
        end_at=end_at,
        exit_code=exit_code,
        budget=budget,
        verdict=verdict,
        dry_run=False,
    )
    update_experiments(spec, output_dir, manifest_path, verdict, budget, dry_run=False)
    if not args.no_execute or original_status in SPEC_STATUSES_RUNNABLE:
        set_spec_status(spec_path, "COMPLETE", dry_run=False)

    run_after_gatekeeper = bool((spec.get("automation") or {}).get("gatekeeper_after_run", True))
    if not args.no_execute and run_after_gatekeeper:
        try:
            gate.after_run_grid(output_dir)
        except GateKeeperError:
            raise PipelineError("post-run GateKeeper check failed") from None

    return {
        "dry_run": False,
        "spec": rel(spec_path),
        "output_dir": rel(output_dir),
        "budget": budget,
        "exit_code": exit_code,
        "missing_artifacts": missing,
        "verdict": {k: v for k, v in verdict.items() if k != "summary"},
        "manifest": rel(manifest_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="data/<run-id>/spec.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print plan only; no writes and no execution")
    parser.add_argument("--no-execute", action="store_true", help="classify existing artifacts without running command")
    parser.add_argument("--allow-archived", action="store_true", help="allow COMPLETE/ARCHIVED spec for artifact backfill")
    parser.add_argument("--force-budget", action="store_true", help="requires explicit user approval outside this script")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_pipeline(args)
    except PipelineError as exc:
        print(f"auto_research_pipeline.py: FAIL: {exc}", file=sys.stderr)
        return 1
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
