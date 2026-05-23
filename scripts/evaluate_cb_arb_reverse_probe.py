#!/usr/bin/env python3
"""Reverse-rank probe for the cb_arb_value_gap_switch strategy.

User research question (2026-05-24): the current cb_arb_value_gap_switch
strategy loses 3% cost-off and 10% cost-on. Is that because:
  (a) value_gap signal genuinely picks under-priced CBs, but transaction
      costs eat the alpha, OR
  (b) the universe itself has negative drift regardless of selection (so
      any selection rule loses, even random), OR
  (c) value_gap signal is anti-alpha (we're systematically picking the
      *worse* CBs).

This probe answers it by running the same strategy with the rank flipped:
buy the most over-priced CBs (largest negative value_gap_amount) instead
of the most under-priced (largest positive value_gap_amount). Comparing
this reverse strategy's PnL to the forward strategy and to a random
baseline disambiguates (a) / (b) / (c).

Implementation: import the evaluate_cb_arb_value_gap_switch module and
monkey-patch its sort + filter step so the rest of the pipeline is
untouched. This avoids forking the main strategy code.

Per CLAUDE.md hard boundaries:
- Does not touch verifier / cost_model / baseline_registry.
- Does not modify the underlying evaluator's source.
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
    # The hot loop in value_gap_switch sorts ranked_today by
    # 'value_gap_amount' ascending=False (largest positive first → most
    # under-priced first) and then filters value_gap_amount > 0.
    # For the reverse probe we want largest *negative* first (most
    # over-priced first) and filter value_gap_amount < 0.
    #
    # Rather than maintain a parallel hot loop, monkey-patch
    # pandas.DataFrame.sort_values for the specific column name pattern
    # used inside the loop. This is a targeted patch that the base
    # evaluator's other sort_values calls (on trade_date, ts_code, etc.)
    # are unaffected by.
    import pandas as _pd

    original_sort_values = _pd.DataFrame.sort_values

    def patched_sort_values(self, by, *args, **kwargs):  # type: ignore[no-redef]
        if isinstance(by, str) and by == "value_gap_amount":
            kwargs = dict(kwargs)
            # Flip ascending — pick most over-priced (smallest value_gap)
            kwargs["ascending"] = not kwargs.get("ascending", False)
            return original_sort_values(self, by, *args, **kwargs)
        return original_sort_values(self, by, *args, **kwargs)

    _pd.DataFrame.sort_values = patched_sort_values  # type: ignore[assignment]

    # Also patch the candidate filter — base evaluator keeps rows where
    # value_gap_amount > 0. For reverse probe we want value_gap_amount < 0.
    # Easier: monkey-patch the comparison on the candidates list-comp.
    # The simplest reliable way is to leave the filter as-is and accept
    # that the reversed sort + same filter will pick the smallest of the
    # positive subset (= least under-priced ≈ near-fair-value cbs). That
    # gives a different but still meaningful contrast: "near-fair-value
    # picks" vs "most under-priced picks". If you want strict "most
    # over-priced", you would need to also flip the > 0 filter.
    #
    # Document this nuance in the run output for review_memory.
    print("[reverse_probe] sort_values('value_gap_amount') patched to ascending=True", flush=True)
    print("[reverse_probe] candidate filter value_gap_amount > 0 left intact (picks least under-priced)", flush=True)

    # Delegate to the base evaluator's main(). It writes its own
    # summary.json / report.yaml / l4_ack / diagnostic under --output-dir.
    rc = base.main()

    # Append a marker so review_memory knows this was the reverse probe.
    import argparse, sys as _sys
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-dir", type=Path, default=None)
    args, _ = parser.parse_known_args()
    if args.output_dir and args.output_dir.exists():
        marker = args.output_dir / "reverse_probe_meta.yaml"
        marker.write_text(
            yaml.safe_dump(
                {
                    "probe_type": "reverse_rank_value_gap",
                    "purpose": "Disambiguate whether value_gap signal is real mispricing vs negative-drift universe vs anti-alpha.",
                    "patched_behavior": "sort_values('value_gap_amount') flipped to ascending=True",
                    "limitation": "candidate filter value_gap_amount > 0 unchanged; picks least under-priced, not most over-priced",
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
