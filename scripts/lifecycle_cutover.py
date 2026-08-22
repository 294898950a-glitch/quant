#!/usr/bin/env python3
"""Refuse lifecycle read cutover until the immutable compare window is green."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework.evaluation.lifecycle_cutover import compare_window  # noqa: E402
from scripts.quant_access_guard import require_ticket  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true", help="write mode=authoritative after the gate passes; check-only is intentionally ticket-free")
    args = parser.parse_args()
    config_path = ROOT / "data/research_framework/strategy_ideator.yaml"
    diffs = ROOT / "data/research_framework/lifecycle_projection_diffs.jsonl"
    ok, reason, count = compare_window(diffs, 30)
    print(f"lifecycle_cutover: {'READY' if ok else 'BLOCKED'} ({reason})")
    if not args.activate:
        return 0 if ok else 1
    if not ok:
        return 1
    require_ticket("lifecycle_cutover_authoritative")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("strategy ideator config must be mapping")
    lifecycle = raw.setdefault("research_lifecycle", {})
    if not isinstance(lifecycle, dict) or lifecycle.get("mode") != "compare":
        raise ValueError("only compare mode may transition to authoritative")
    lifecycle["mode"] = "authoritative"
    lifecycle["cutover_zero_diff_cycles"] = count
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
