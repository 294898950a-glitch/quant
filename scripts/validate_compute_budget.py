#!/usr/bin/env python3
"""Validate autonomous compute budget configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "data" / "research_framework" / "compute_budget_config.json"


def main() -> int:
    issues: list[str] = []
    if not CONFIG.exists():
        issues.append(f"missing budget config: {CONFIG.relative_to(REPO_ROOT)}")
    else:
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(f"budget config is not valid JSON: {exc}")
            data = {}

        required = {
            "auto_approve_limit_yuan": 100.0,
            "spot_yuan_per_hour": None,
            "sig_yuan_per_hour": None,
            "local_yuan_per_hour": None,
            "safety_multiplier": None,
        }
        for key, expected in required.items():
            if key not in data:
                issues.append(f"budget config missing {key}")
                continue
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                issues.append(f"budget config {key} is not numeric")
                continue
            if value < 0:
                issues.append(f"budget config {key} must be >= 0")
            if expected is not None and value != expected:
                issues.append(f"budget config {key} must be {expected:g}")
        try:
            if float(data.get("safety_multiplier", 0)) < 1:
                issues.append("budget config safety_multiplier must be >= 1")
        except (TypeError, ValueError):
            pass

    if issues:
        for issue in issues:
            print(issue)
        return 1

    print("validate_compute_budget.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
