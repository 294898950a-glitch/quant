#!/usr/bin/env python3
"""Validate the autonomous research architecture stays pruned to 5 nodes."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
ARCH_PATH = REPO_ROOT / "data" / "research_framework" / "autonomous_research_acceptance_criteria.yaml"
EXPECTED = {"state_and_rules", "ideation", "proposal_gate", "runner", "review_memory"}


def main() -> int:
    errors: list[str] = []
    data = yaml.safe_load(ARCH_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        errors.append("architecture file root must be a mapping")
        data = {}

    components = data.get("active_components")
    if not isinstance(components, list):
        errors.append("active_components must be a list")
        components = []
    ids = [str(item.get("id")) for item in components if isinstance(item, dict)]
    if len(ids) > 5:
        errors.append(f"active architecture has {len(ids)} nodes; max is 5")
    missing = sorted(EXPECTED - set(ids))
    extra = sorted(set(ids) - EXPECTED)
    if missing:
        errors.append(f"missing active architecture nodes: {missing}")
    if extra:
        errors.append(f"unexpected active architecture nodes: {extra}")
    if "components" in data:
        errors.append("legacy components list is not allowed; use active_components")

    acceptance = data.get("acceptance") if isinstance(data, dict) else {}
    if not isinstance(acceptance, dict):
        errors.append("acceptance must be a mapping")
    elif int(acceptance.get("max_active_components") or 0) != 5:
        errors.append("acceptance.max_active_components must be 5")

    for component in components:
        if not isinstance(component, dict):
            continue
        for field in ("owns", "files", "must", "must_not"):
            value = component.get(field)
            if not isinstance(value, list) or not value:
                errors.append(f"component {component.get('id')} missing non-empty {field}")

    if errors:
        print(f"validate_architecture_pruned.py: {len(errors)} failure(s)")
        for error in errors:
            print(f"  FAIL {error}")
        return 1
    print("validate_architecture_pruned.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
