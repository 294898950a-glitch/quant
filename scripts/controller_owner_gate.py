#!/usr/bin/env python3
"""CLI ownership fence shared by shell controller entrypoints."""

from __future__ import annotations

import argparse
from pathlib import Path

from framework.autonomous.controller_owner import audit_noop, owner_allows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--action", default="controller_owner_noop")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    allowed, reason = owner_allows(current_path=root / "data/research_framework/current.yaml")
    if allowed:
        print(reason)
        return 0
    audit_noop(
        audit_path=root / "data/research_framework/orchestrator_log.jsonl",
        reason=reason,
        action=args.action,
    )
    print(reason)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
