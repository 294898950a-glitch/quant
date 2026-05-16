#!/usr/bin/env python3
"""Validate spec.yaml files in data/<run-id>/ against HDRF L1 schema.

Replaces soft "grep section title" check on old spec.md with strict YAML schema.

Required fields (缺 → exit 1):
- schema_version, run_id, date, strategy_id, l0_entry_id, hypothesis
- parameter_space (≥1 dimension)
- hard_floors (≥1 floor)
- cv_design (string), cv_holdout_years (list)
- compute_estimate (sig_minutes + spot_minutes + estimated_cost_yuan)
- budget_cap_yuan
- stop_conditions (≥1)
- artifacts_required (≥1)
- status (DRAFT/READY/RUNNING/COMPLETE/ARCHIVED)

Recommended (缺 → warn):
- source_insight, new_data_sources, auxiliary_metrics, escalation, notes

Usage:
  python3 scripts/validate_spec.py                # check all data/<run-id>/spec.yaml
  python3 scripts/validate_spec.py path/to/spec.yaml   # single file
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
    "schema_version", "run_id", "date", "strategy_id", "l0_entry_id",
    "hypothesis", "parameter_space", "hard_floors", "cv_design",
    "cv_holdout_years", "compute_estimate", "budget_cap_yuan",
    "stop_conditions", "artifacts_required", "status",
}

RECOMMENDED_FIELDS = {
    "source_insight", "new_data_sources", "auxiliary_metrics",
    "escalation", "notes", "l0_source",
}

ALLOWED_STATUS = {"DRAFT", "READY", "RUNNING", "COMPLETE", "ARCHIVED"}
ALLOWED_L0_ENTRIES = {1, 2, 3}
ALLOWED_CV = {"leave-one-year-out", "sealed-pool-8", "walk-forward",
              "leave-one-pool-out", "k-fold", "single-window"}


def validate(path: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a single spec.yaml."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"], []
    if not isinstance(data, dict):
        return ["root must be a dict"], []

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")

    # Type / enum checks
    if data.get("schema_version") != 1:
        errors.append(f"schema_version must be 1, got {data.get('schema_version')}")
    if data.get("l0_entry_id") not in ALLOWED_L0_ENTRIES:
        errors.append(f"l0_entry_id must be 1/2/3, got {data.get('l0_entry_id')}")
    if data.get("status") not in ALLOWED_STATUS:
        errors.append(f"status invalid: {data.get('status')} (allowed: {ALLOWED_STATUS})")

    # Sub-structure
    pspace = data.get("parameter_space") or []
    if not isinstance(pspace, list) or not pspace:
        errors.append("parameter_space must be non-empty list")
    else:
        for i, p in enumerate(pspace):
            if not isinstance(p, dict) or "name" not in p or "range" not in p:
                errors.append(f"parameter_space[{i}]: must have name + range")

    floors = data.get("hard_floors") or {}
    if not isinstance(floors, dict) or not floors:
        errors.append("hard_floors must be non-empty dict")

    cv = data.get("cv_design", "")
    # Allow any value but warn if not in known list
    if cv and cv not in ALLOWED_CV:
        warnings.append(f"cv_design '{cv}' not in standard set {ALLOWED_CV}")

    years = data.get("cv_holdout_years") or []
    if not isinstance(years, list) or not years:
        errors.append("cv_holdout_years must be non-empty list of years")

    cost = data.get("compute_estimate") or {}
    if not isinstance(cost, dict):
        errors.append("compute_estimate must be dict")
    else:
        for f in ("sig_minutes", "spot_minutes", "estimated_cost_yuan"):
            if f not in cost:
                errors.append(f"compute_estimate missing field: {f}")

    if not isinstance(data.get("budget_cap_yuan"), (int, float)):
        errors.append("budget_cap_yuan must be number")
    else:
        if data["budget_cap_yuan"] > 100 and data.get("status") in ("DRAFT", "READY", "RUNNING"):
            warnings.append(f"budget_cap_yuan {data['budget_cap_yuan']} > 100, requires user approval per U17")

    stops = data.get("stop_conditions") or []
    if not isinstance(stops, list) or not stops:
        errors.append("stop_conditions must be non-empty list")

    artifacts = data.get("artifacts_required") or []
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts_required must be non-empty list")

    # Recommended warnings
    rec_missing = RECOMMENDED_FIELDS - set(data.keys())
    if rec_missing:
        warnings.append(f"missing recommended fields: {sorted(rec_missing)}")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="single spec.yaml path (default: scan all data/<run-id>/spec.yaml)")
    args = parser.parse_args()

    if args.path:
        p = Path(args.path)
        if not p.is_absolute():
            p = REPO_ROOT / p
        paths = [p]
    else:
        paths = sorted(DATA_DIR.glob("*/spec.yaml"))

    if not paths:
        print("validate_spec.py: no spec.yaml found (OK for fresh repos)")
        return 0

    total_err = 0
    total_warn = 0
    for path in paths:
        errors, warnings = validate(path)
        if errors:
            print(f"\nFAIL: {path.relative_to(REPO_ROOT)}")
            for e in errors:
                print(f"  ERROR {e}")
            total_err += len(errors)
        if warnings:
            print(f"\nWARN: {path.relative_to(REPO_ROOT)}")
            for w in warnings:
                print(f"  WARN  {w}")
            total_warn += len(warnings)
        if not errors and not warnings:
            print(f"OK: {path.relative_to(REPO_ROOT)}")

    print(f"\nvalidate_spec.py: {len(paths)} spec(s), {total_err} error(s), {total_warn} warning(s)")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
