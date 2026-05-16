#!/usr/bin/env python3
"""Validate data/<run-id>/report.yaml for completed HDRF runs.

Only runs with spec.yaml status=COMPLETE require report.yaml. DRAFT, READY,
RUNNING, and ARCHIVED runs are skipped so historical artifacts do not need a
mechanical migration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

REQUIRED_FIELDS = {
    "schema_version",
    "run_id",
    "date",
    "strategy_id",
    "l6_exit_decision",
    "three_exits_section",
    "compute_cost_yuan",
    "confirmed_invalid_directions",
    "learnings",
    "follow_up_actions",
    "status",
}

RECOMMENDED_FIELDS = {"notes", "references", "related_reports"}

ALLOWED_L6_DECISION = {"adopt", "reject", "mini-spec-retry", "archive-direction"}
ALLOWED_STATUS = {"DRAFT", "READY", "COMPLETE", "ARCHIVED"}
PLACEHOLDER_TOKENS = {"TBD", "tbd", "...", "skip", "SKIP", "TODO", "todo", "pending", "PENDING"}


def load_yaml(path: Path) -> tuple[dict | None, list[str]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [f"YAML parse error: {exc}"]
    if not isinstance(data, dict):
        return None, ["root must be a dict"]
    return data, []


def is_placeholder(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or stripped in PLACEHOLDER_TOKENS
    if isinstance(value, list):
        return not value or any(is_placeholder(item) for item in value)
    if isinstance(value, dict):
        return not value or any(is_placeholder(item) for item in value.values())
    return False


def validate(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    data, load_errors = load_yaml(path)
    if load_errors:
        return load_errors, []
    assert data is not None

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")

    if data.get("schema_version") != 1:
        errors.append(f"schema_version must be 1, got {data.get('schema_version')}")
    if data.get("l6_exit_decision") not in ALLOWED_L6_DECISION:
        errors.append(
            f"l6_exit_decision invalid: {data.get('l6_exit_decision')} "
            f"(allowed: {sorted(ALLOWED_L6_DECISION)})"
        )
    if data.get("status") not in ALLOWED_STATUS:
        errors.append(f"status invalid: {data.get('status')} (allowed: {sorted(ALLOWED_STATUS)})")
    if not isinstance(data.get("compute_cost_yuan"), (int, float)):
        errors.append("compute_cost_yuan must be number")

    for field in sorted(REQUIRED_FIELDS):
        if field in data and field not in {"schema_version", "compute_cost_yuan"} and is_placeholder(data[field]):
            errors.append(f"{field} is placeholder/empty")

    rec_missing = RECOMMENDED_FIELDS - set(data.keys())
    if rec_missing:
        warnings.append(f"missing recommended fields: {sorted(rec_missing)}")

    return errors, warnings


def spec_status(run_dir: Path) -> str | None:
    spec_path = run_dir / "spec.yaml"
    if not spec_path.exists():
        return None
    data, errors = load_yaml(spec_path)
    if errors or data is None:
        return None
    status = data.get("status")
    return status if isinstance(status, str) else None


def report_required(run_dir: Path) -> bool:
    return spec_status(run_dir) == "COMPLETE"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="single report.yaml path (default: scan COMPLETE runs)")
    args = parser.parse_args()

    if args.path:
        path = Path(args.path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        paths = [path]
        skipped = 0
    else:
        run_dirs = sorted([d for d in DATA_DIR.iterdir() if d.is_dir() and (d / "spec.yaml").exists()])
        paths = []
        skipped = 0
        for run_dir in run_dirs:
            if not report_required(run_dir):
                skipped += 1
                continue
            report_path = run_dir / "report.yaml"
            if report_path.exists():
                paths.append(report_path)
            else:
                print(f"FAIL: {run_dir.name} — spec.yaml status COMPLETE but report.yaml missing")
                return 1

    total_err = 0
    total_warn = 0
    for path in paths:
        errors, warnings = validate(path)
        if errors:
            print(f"\nFAIL: {path.relative_to(REPO_ROOT)}")
            for error in errors:
                print(f"  ERROR {error}")
            total_err += len(errors)
        if warnings:
            print(f"\nWARN: {path.relative_to(REPO_ROOT)}")
            for warning in warnings:
                print(f"  WARN  {warning}")
            total_warn += len(warnings)
        if not errors and not warnings:
            print(f"OK: {path.relative_to(REPO_ROOT)}")

    print(f"\nvalidate_report.py: {len(paths)} checked, {skipped} skipped, {total_err} error(s), {total_warn} warning(s)")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
