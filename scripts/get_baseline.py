#!/usr/bin/env python3
"""Get baseline rows from data/research_framework/baseline_registry.yaml."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REGISTRY = Path(__file__).resolve().parent.parent / "data" / "research_framework" / "baseline_registry.yaml"


def parse_registry() -> list[dict]:
    if not REGISTRY.exists():
        print(f"ERROR: {REGISTRY} missing", file=sys.stderr)
        sys.exit(1)
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("baselines"), list):
        print("ERROR: baseline_registry.yaml missing baselines list", file=sys.stderr)
        sys.exit(1)
    return [row for row in data["baselines"] if isinstance(row, dict)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", help="strategy_id to lookup")
    parser.add_argument("--list", action="store_true", help="list all baselines")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    rows = parse_registry()
    if args.list:
        for row in rows:
            print(f"  {row.get('pk', '?')}: {row.get('strategy_id', '?')} [{row.get('status', '?')}]")
        return 0

    if not args.strategy:
        parser.error("--strategy or --list required")

    matched = [
        row for row in rows
        if args.strategy.lower() in str(row.get("strategy_id", "")).lower()
        or args.strategy.lower() in str(row.get("pk", "")).lower()
    ]

    if not matched:
        print(f"ERROR: no baseline found for strategy='{args.strategy}'", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(matched, indent=2, ensure_ascii=False))
    else:
        for row in matched:
            metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
            print(f"pk: {row.get('pk', '?')}")
            print(f"strategy_id: {row.get('strategy_id', '?')}")
            print(f"date: {row.get('date', '?')}")
            print(f"status: {row.get('status', '?')}")
            if metrics:
                print(f"metrics: {json.dumps(metrics, ensure_ascii=False, sort_keys=True)}")
            for key in ("artifact", "manifest_path", "git_commit"):
                if key in row:
                    print(f"{key}: {row[key]}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
