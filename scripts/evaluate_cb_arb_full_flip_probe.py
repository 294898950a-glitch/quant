#!/usr/bin/env python3
"""Full-flip probe for the cb_arb_value_gap_switch strategy.

Follow-up to the 2026-05-24 reverse-rank probe. The first probe flipped
only the sort direction (ascending=True), but kept the candidate filter
``value_gap_amount > 0`` intact — so it picked the smallest-positive
gaps, not the negative-gap (over-priced) bonds.

This probe flips the **other** axis: the sign of value_gap_amount itself.
After negation:

* Originally-negative (over-priced) bonds have positive value_gap_amount.
* The base evaluator's ``> 0`` filter now selects exactly those bonds.
* The base evaluator's descending sort puts most-negative-original
  (= most-over-priced) bonds at the top.

So **without** monkey-patching sort, the run buys the bonds the formula
considers MOST OVER-PRICED. That is the strong-form anti-alpha test.

If forward loses, sort-reversed wins (=> first probe), AND
sign-flipped wins (=> this probe), the value_gap formula is fully
inverted relative to the exploitable structure.

Per CLAUDE.md hard boundaries:
- Does not touch verifier / cost_model / baseline_registry.
- Does not modify the underlying evaluator's source file.
- Results are evidence only; user must approve any truth promotion.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def declare_data_requirements(command: list[str], spec: dict[str, Any]) -> dict[str, Any]:
    """Mirror the underlying value_gap_switch evaluator's data needs."""
    return {
        "required_files": [
            {"path": "data/cb_warehouse/cb_basic.parquet"},
            {"path": "data/cb_warehouse/cb_daily.parquet"},
            {"path": "data/cb_warehouse/cb_call.parquet"},
            {"path": "data/cb_warehouse/stk_daily_qfq.parquet"},
        ],
    }


def _load_base_module() -> Any:
    base_path = REPO_ROOT / "scripts" / "evaluate_cb_arb_value_gap_switch.py"
    if not base_path.exists():
        raise FileNotFoundError(f"underlying evaluator missing: {base_path}")
    spec = importlib.util.spec_from_file_location("_cb_arb_value_gap_switch_base", base_path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load value_gap_switch base module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    base = _load_base_module()

    # Patch 1: wrap _add_value_gap_amounts so the fresh computation
    # returns negated value_gap_amount.
    original_add = base._add_value_gap_amounts

    def patched_add(ranks, root, fixed_source):
        result = original_add(ranks, root, fixed_source)
        if "value_gap_amount" in result.columns:
            result = result.copy()
            result["value_gap_amount"] = -result["value_gap_amount"]
        return result

    base._add_value_gap_amounts = patched_add

    # Patch 2: pd.read_parquet — when reusing a cached ranks file, also
    # negate. Match by column presence to avoid touching unrelated reads.
    original_read = pd.read_parquet

    def patched_read(path, *args, **kwargs):
        df = original_read(path, *args, **kwargs)
        path_str = str(path)
        if "value_gap_amount" in df.columns and (
            "daily_value_gap_amounts" in path_str or path_str.endswith("ranks.parquet")
        ):
            df = df.copy()
            df["value_gap_amount"] = -df["value_gap_amount"]
        return df

    pd.read_parquet = patched_read

    print(
        "[full_flip_probe] value_gap_amount negated; > 0 filter now selects originally-negative (over-priced) gaps",
        flush=True,
    )
    print(
        "[full_flip_probe] sort_values('value_gap_amount') NOT reversed; descending on negated col → most-over-priced first",
        flush=True,
    )

    rc = base.main()

    # Write a marker so review_memory can identify this probe.
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-dir", type=Path, default=None)
    args, _ = parser.parse_known_args()
    if args.output_dir and args.output_dir.exists():
        marker = args.output_dir / "full_flip_probe_meta.yaml"
        marker.write_text(
            yaml.safe_dump(
                {
                    "probe_type": "full_flip_value_gap",
                    "purpose": "Strong-form anti-alpha test — buy the most over-priced bonds (originally value_gap_amount < 0).",
                    "patched_behavior": "_add_value_gap_amounts() and read_parquet() of daily ranks return value_gap_amount * -1; > 0 filter now picks originally-negative gaps; descending sort picks most-negative-original first.",
                    "predecessor": "data/cb_arb_value_gap_switch_reverse-probe_20260524",
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
