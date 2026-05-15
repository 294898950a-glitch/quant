#!/usr/bin/env python3
"""Validate data warehouse schema against expectations (phase 1.5 #2 spec).

Reads data/research_framework/data_schema_expectations.yaml.
For each warehouse file: check exists, parseable, required_columns present, row count >= min.
Recommended columns missing → warn (not fail). Required missing → fail.

Usage:
  python3 scripts/validate_data_schema.py            # check all
  python3 scripts/validate_data_schema.py --strict   # promote recommended-missing to fail

Exit codes:
  0 OK
  1 strict failure (file missing / required column missing / row count too low / parse error)
  2 warnings only (recommended column missing)
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

try:
    import pyarrow.parquet as pq
except ImportError:
    print("ERROR: pyarrow required (for parquet schema inspection)", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATIONS = REPO_ROOT / "data" / "research_framework" / "data_schema_expectations.yaml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="promote recommended-missing to fail")
    args = parser.parse_args()

    if not EXPECTATIONS.exists():
        print(f"ERROR: {EXPECTATIONS} missing", file=sys.stderr)
        return 1
    expected = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))

    fail_count = 0
    warn_count = 0

    for entry in expected.get("warehouse_files", []):
        path = REPO_ROOT / entry["path"]
        name = path.name
        print(f"\n=== {name} ===")
        if not path.exists():
            print(f"  FAIL: file missing at {path}")
            fail_count += 1
            continue
        try:
            table = pq.read_metadata(path)
            schema = pq.read_schema(path)
            cols = set(schema.names)
            rows = table.num_rows
        except Exception as e:
            print(f"  FAIL: parquet read error: {e}")
            fail_count += 1
            continue

        required = set(entry.get("required_columns", []))
        recommended = set(entry.get("recommended_columns", []))
        min_rows = entry.get("expected_min_rows", 0)

        missing_required = required - cols
        missing_recommended = recommended - cols

        if missing_required:
            print(f"  FAIL: missing required columns: {sorted(missing_required)}")
            fail_count += 1
        if rows < min_rows:
            print(f"  FAIL: rows={rows} < expected_min_rows={min_rows}")
            fail_count += 1
        if missing_recommended:
            severity = "FAIL" if args.strict else "WARN"
            print(f"  {severity}: missing recommended columns: {sorted(missing_recommended)}")
            if args.strict:
                fail_count += 1
            else:
                warn_count += 1
        if not missing_required and not missing_recommended and rows >= min_rows:
            print(f"  OK ({rows} rows, {len(cols)} cols)")

    print("\n=== summary ===")
    if fail_count:
        print(f"FAIL: {fail_count} schema failure(s)")
    if warn_count:
        print(f"WARN: {warn_count} recommended-column warning(s)")
    if not fail_count and not warn_count:
        print("OK: all warehouse files match schema expectations")

    if fail_count:
        return 1
    if warn_count:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
