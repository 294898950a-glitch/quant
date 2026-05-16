#!/usr/bin/env python3
"""Deprecated.

Current state is stored in data/research_framework/current.yaml. No Markdown
snapshot is generated.
"""

from __future__ import annotations


def main() -> int:
    print("snapshot_current_state.py: deprecated; use data/research_framework/current.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
