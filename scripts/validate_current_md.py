#!/usr/bin/env python3
"""Validate data/research_framework/current.yaml.

The filename is kept for compatibility with existing preflight wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT = REPO_ROOT / "data" / "research_framework" / "current.yaml"

REQUIRED_STRATEGY_FIELDS = {
    "strategy_id",
    "name",
    "status",
    "baseline_row",
    "deployment_contract_status",
    "research_direction",
}
ALLOWED_STATUS = {"experiment", "wip", "adopted", "rejected", "archived", "stale", "invalidated", "n/a"}
ALLOWED_RESEARCH_DIRECTION = {"open", "closed"}
ALLOWED_DEPLOYMENT_STATUS = {"passing", "failing", "unknown", "n/a"}


def main() -> int:
    if not CURRENT.exists():
        print(f"ERROR: missing {CURRENT.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    try:
        data = yaml.safe_load(CURRENT.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"ERROR: current.yaml parse failure: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("ERROR: current.yaml root must be mapping", file=sys.stderr)
        return 1

    issues: list[str] = []
    if data.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    strategies = data.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        issues.append("strategies must be a non-empty list")
    else:
        seen: set[str] = set()
        for idx, item in enumerate(strategies):
            if not isinstance(item, dict):
                issues.append(f"strategies[{idx}] must be mapping")
                continue
            sid = str(item.get("strategy_id") or "")
            if sid in seen:
                issues.append(f"duplicate strategy_id: {sid}")
            seen.add(sid)
            missing = REQUIRED_STRATEGY_FIELDS - set(item)
            if missing:
                issues.append(f"{sid or idx}: missing fields {sorted(missing)}")
            if item.get("status") not in ALLOWED_STATUS:
                issues.append(f"{sid}: invalid status {item.get('status')}")
            if item.get("research_direction") not in ALLOWED_RESEARCH_DIRECTION:
                issues.append(f"{sid}: invalid research_direction {item.get('research_direction')}")
            if item.get("deployment_contract_status") not in ALLOWED_DEPLOYMENT_STATUS:
                issues.append(f"{sid}: invalid deployment_contract_status {item.get('deployment_contract_status')}")
            if item.get("status") not in {"archived", "rejected", "invalidated"}:
                if not isinstance(item.get("decision_contract"), dict):
                    issues.append(f"{sid}: active strategy missing decision_contract")
                if not isinstance(item.get("metrics"), dict):
                    issues.append(f"{sid}: active strategy missing metrics")

    no_reply = data.get("no_reply_default")
    if not isinstance(no_reply, dict):
        issues.append("missing no_reply_default")
    else:
        if no_reply.get("timeout_minutes") != 30:
            issues.append("no_reply_default.timeout_minutes must be 30")
        if no_reply.get("auto_continue_limit_cny") != 100:
            issues.append("no_reply_default.auto_continue_limit_cny must be 100")

    if issues:
        print(f"validate_current_md.py: {len(issues)} failure(s)")
        for issue in issues:
            print(f"  FAIL {issue}")
        return 1

    print("validate_current_md.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
