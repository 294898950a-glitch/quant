#!/usr/bin/env python3
"""Deprecated.

Markdown indexes are no longer part of the framework. Runtime entrypoints are
defined in data/research_framework/runtime_entrypoints.yaml.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--docs", action="store_true")
    parser.add_argument("--reports", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.parse_args()
    print("generate_indexes.py: deprecated; machine YAML entrypoints are authoritative")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
