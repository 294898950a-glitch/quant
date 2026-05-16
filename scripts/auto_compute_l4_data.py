#!/usr/bin/env python3
"""Auto-compute L4 question data from grid artifacts.

For each data/<run-id>/ where spec.yaml status is RUNNING/COMPLETE:
- Read ranked.csv → compute Q1 floor_binding distribution
- Read ranked.csv → compute Q4 selected vs grid edges
- Read trades.csv (baseline + selected) → compute Q5 overlap / exit_changes / pnl_gap
- Read summary.csv → Q3 baseline pass/fail on floors

Write data into data/<run-id>/l4_ack.yaml under each question's `computed_data`
field, plus top-level `auto_computed_at` timestamp.

This is the "数据自动填" half of L4. Claude fills the other half (`answer` +
`pass` + `overall_decision` + `overall_reason`) by hand.

Usage:
  python3 scripts/auto_compute_l4_data.py             # process all active runs
  python3 scripts/auto_compute_l4_data.py --dry-run   # show what would change
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def safe_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def compute_q1_floor_binding(ranked: list[dict], hard_floors: dict) -> dict:
    """Distribution of which floors each candidate passes."""
    if not ranked or not hard_floors:
        return {"note": "ranked.csv empty or hard_floors missing"}
    floor_keys = [f"replay_{y}_excess" for y in (k.split("_")[1] for k in hard_floors.keys() if k.startswith("replay_"))]
    # Fallback: try common 6-year floor cols
    if not floor_keys:
        floor_keys = ["replay_2020_excess", "replay_2021_excess",
                      "replay_2022_excess", "replay_2023_excess"]
    total = len(ranked)
    pass_count = {fk: 0 for fk in floor_keys}
    pass_all = 0
    fail_all = 0
    for row in ranked:
        passes = []
        for fk in floor_keys:
            yr_key = fk.replace("_excess", "").replace("replay_", "")
            floor = hard_floors.get(f"replay_{yr_key}") or hard_floors.get(yr_key) or None
            val = safe_float(row.get(fk))
            if val is not None and floor is not None and val >= floor:
                pass_count[fk] += 1
                passes.append(fk)
        if len(passes) == len(floor_keys):
            pass_all += 1
        elif not passes:
            fail_all += 1
    return {
        "total_candidates": total,
        "pass_all_floors": pass_all,
        "fail_all_floors": fail_all,
        "pass_count_per_floor": pass_count,
        "pass_all_pct": round(pass_all / total * 100, 2) if total else 0.0,
        "binding_assessment": (
            "floor 太松 (>50% 全过)" if total and pass_all / total > 0.5
            else "至少一 floor binding (健康)" if pass_all > 0 and pass_all < total
            else "0 候选全过 (严苛但 grid 没有 winner)"
        ),
    }


def compute_q4_edge_of_grid(ranked: list[dict], spec: dict) -> dict:
    """Check if top candidate is at edge of parameter ranges."""
    if not ranked:
        return {"note": "ranked.csv empty"}
    top = ranked[0]
    pspace = spec.get("parameter_space", [])
    edges = []
    for p in pspace:
        name = p.get("name")
        rng = p.get("range") or [None, None]
        if name and name in top:
            val = safe_float(top.get(name))
            if val is not None and rng[0] is not None and rng[1] is not None:
                lo, hi = float(rng[0]), float(rng[1])
                if val <= lo + (hi - lo) * 0.05 or val >= hi - (hi - lo) * 0.05:
                    edges.append({"param": name, "value": val, "range": [lo, hi]})
    return {
        "top_candidate_id": top.get("id") or top.get("rank") or "rank=0",
        "params_at_edge": edges,
        "edge_count": len(edges),
        "assessment": (
            f"selected 有 {len(edges)} 个参数在 grid 边缘 (可能 path-sensitive)"
            if edges else "selected 不在 grid 边缘 (相对 robust)"
        ),
    }


def compute_q5_trade_overlap(trades_baseline: list[dict], trades_selected: list[dict]) -> dict:
    """Compute trade overlap, exit changes, pnl gap."""
    if not trades_baseline or not trades_selected:
        return {"note": "trades.csv missing (baseline or selected)"}
    base_set = {(r.get("ts_code"), r.get("entry_date")) for r in trades_baseline}
    sel_set = {(r.get("ts_code"), r.get("entry_date")) for r in trades_selected}
    common = base_set & sel_set
    baseline_total_pnl = sum(safe_float(r.get("trade_pnl")) or 0 for r in trades_baseline)
    selected_total_pnl = sum(safe_float(r.get("trade_pnl")) or 0 for r in trades_selected)
    return {
        "baseline_trade_count": len(trades_baseline),
        "selected_trade_count": len(trades_selected),
        "common_trades": len(common),
        "baseline_only": len(base_set - sel_set),
        "selected_only": len(sel_set - base_set),
        "baseline_total_pnl": round(baseline_total_pnl, 2),
        "selected_total_pnl": round(selected_total_pnl, 2),
        "pnl_gap_selected_minus_baseline": round(selected_total_pnl - baseline_total_pnl, 2),
    }


def is_target_run(run_dir: Path) -> bool:
    spec_path = run_dir / "spec.yaml"
    if not spec_path.exists():
        return False
    try:
        spec = load_yaml(spec_path)
        return spec.get("status") in ("RUNNING", "COMPLETE")
    except Exception:
        return False


def process_run(run_dir: Path, dry_run: bool) -> bool:
    spec = load_yaml(run_dir / "spec.yaml")
    ranked = read_csv_rows(run_dir / "ranked.csv")
    trades_baseline = read_csv_rows(run_dir / "trades_baseline.csv") or read_csv_rows(run_dir / "trades.csv")
    trades_selected = read_csv_rows(run_dir / "trades_selected.csv")

    q1 = compute_q1_floor_binding(ranked, spec.get("hard_floors", {}))
    q4 = compute_q4_edge_of_grid(ranked, spec)
    q5 = compute_q5_trade_overlap(trades_baseline, trades_selected)

    ack_path = run_dir / "l4_ack.yaml"
    existing = load_yaml(ack_path) if ack_path.exists() else {}

    def merge_q(ex: dict, qkey: str, computed: dict) -> dict:
        slot = ex.get(qkey) or {}
        slot["computed_data"] = computed
        slot["computed_at"] = dt.datetime.utcnow().isoformat() + "Z"
        return slot

    existing["q1_floor_binding"] = merge_q(existing, "q1_floor_binding", q1)
    existing["q4_monotonic"] = merge_q(existing, "q4_monotonic", q4)
    existing["q5_trade_overlap"] = merge_q(existing, "q5_trade_overlap", q5)
    existing["auto_computed_at"] = dt.datetime.utcnow().isoformat() + "Z"

    if dry_run:
        print(f"DRY-RUN: {ack_path.relative_to(REPO_ROOT)}")
        print(f"  Q1 pass_all={q1.get('pass_all_floors')}/{q1.get('total_candidates')}")
        print(f"  Q4 edge_count={q4.get('edge_count')}")
        print(f"  Q5 common={q5.get('common_trades')} baseline_only={q5.get('baseline_only')}")
        return True

    ack_path.write_text(yaml.dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"WROTE: {ack_path.relative_to(REPO_ROOT)} (auto data filled, Claude still needs to fill 'answer' + 'pass' + 'overall_*')")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dirs = sorted([d for d in DATA_DIR.iterdir() if d.is_dir() and (d / "spec.yaml").exists()])
    target_runs = [d for d in run_dirs if is_target_run(d)]
    print(f"Found {len(target_runs)} active run(s) (spec.yaml status RUNNING/COMPLETE)")

    if not target_runs:
        print("No active runs to process. Auto data filled when spec.yaml status flips to RUNNING/COMPLETE.")
        return 0

    for run_dir in target_runs:
        process_run(run_dir, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
