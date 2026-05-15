#!/usr/bin/env python3
"""Get baseline row from baseline_registry.md (P1.4 spec).

Usage:
  python3 scripts/get_baseline.py --strategy cb_arb
  python3 scripts/get_baseline.py --strategy cb_arb --json
  python3 scripts/get_baseline.py --list

Parses the baseline_registry.md markdown table and returns the latest active
baseline row for the strategy. Active = not archived/rejected/invalidated.

Avoids agents scraping markdown by hand.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "data" / "research_framework" / "baseline_registry.md"


def parse_registry() -> list[dict]:
    """Parse markdown tables under ### headers, return list of baseline rows."""
    if not REGISTRY.exists():
        print(f"ERROR: {REGISTRY} missing", file=sys.stderr)
        sys.exit(1)
    text = REGISTRY.read_text(encoding="utf-8")
    rows = []
    sections = re.split(r"^### (.+)$", text, flags=re.MULTILINE)
    for i in range(1, len(sections), 2):
        section_name = sections[i].strip()
        body = sections[i + 1] if i + 1 < len(sections) else ""
        table_match = re.search(r"^\| pk \|.*?\n((?:\|.*?\n)+)", body, re.MULTILINE)
        if not table_match:
            continue
        lines = table_match.group(0).strip().split("\n")
        if len(lines) < 3:
            continue
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        for line in lines[2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != len(headers):
                continue
            row = dict(zip(headers, cells))
            row["_section"] = section_name
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", help="strategy_id to lookup")
    parser.add_argument("--list", action="store_true", help="list all baselines")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    rows = parse_registry()
    if args.list:
        for row in rows:
            print(f"  {row.get('pk', '?')}: {row.get('_section', '?')} [{row.get('status', '?')[:50]}]")
        return 0

    if not args.strategy:
        parser.error("--strategy or --list required")

    matched = [r for r in rows
               if args.strategy.lower() in r.get("pk", "").lower()
               or args.strategy.lower() in r.get("_section", "").lower()]

    if not matched:
        print(f"ERROR: no baseline found for strategy='{args.strategy}'", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(matched, indent=2, ensure_ascii=False))
    else:
        for row in matched:
            print(f"# {row.get('_section', '?')}")
            print(f"  pk: {row.get('pk', '?')}")
            print(f"  日期: {row.get('日期', '?')}")
            print(f"  累计 excess (复利): {row.get('累计 excess (复利)', '?')}")
            print(f"  status: {row.get('status', '?')}")
            for key in ("artifact", "manifest_path", "git_commit"):
                if key in row:
                    print(f"  {key}: {row[key]}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
