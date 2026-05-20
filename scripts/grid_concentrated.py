#!/usr/bin/env python3
"""方向三: 降频+集中持仓 — 网格搜索包装器。

遍历 max_holdings × max_position_pct × max_holding_days 组合，
对每个组合运行 evaluate_cb_arb_value_gap_switch.py，
收集 train/test 指标并排名。

用法:
    .venv/bin/python scripts/grid_concentrated.py \
        --data-root data/cb_arb_concurrent_supervised_20260511_094500 \
        --output-dir data/cb_arb_value_gap_switch_low-freq-concentrated_2026-05-18
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVALUATOR = REPO_ROOT / "scripts" / "evaluate_cb_arb_value_gap_switch.py"
PYTHON = Path(sys.executable)

DEFAULT_DATA_ROOT = "data/cb_arb_concurrent_supervised_20260511_094500"
DEFAULT_TRAIN_START = "20190101"
DEFAULT_TRAIN_END = "20241231"
DEFAULT_TEST_START = "20250101"
DEFAULT_TEST_END = "20260508"
DEFAULT_TOP_N = 8


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_config(
    data_root: str,
    max_holdings: int,
    max_position_pct: float,
    max_holding_days: int,
    output_dir: str,
    *,
    train_start: str = DEFAULT_TRAIN_START,
    train_end: str = DEFAULT_TRAIN_END,
    test_start: str = DEFAULT_TEST_START,
    test_end: str = DEFAULT_TEST_END,
    top_n: int = DEFAULT_TOP_N,
    dry_run: bool = False,
) -> dict | None:
    """Run the evaluator with one config. Returns parsed summary dict."""
    label = f"h{max_holdings}_p{int(max_position_pct*100)}_d{max_holding_days}"
    run_dir = Path(output_dir) / label
    run_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(PYTHON), str(EVALUATOR),
        "--data-root", str(data_root),
        "--train-start", train_start, "--train-end", train_end,
        "--test-start", test_start, "--test-end", test_end,
        "--fixed-source", "2", "--rule", "score_4state",
        "--cost-model-enabled",
        "--max-holdings", str(max_holdings),
        "--max-position-pct", str(max_position_pct),
        "--max-holding-days", str(max_holding_days),
        "--top-n", str(top_n),
        "--output-dir", str(run_dir),
    ]

    print(f"[grid] {label} starting...", flush=True)
    if dry_run:
        print(f"  DRY_RUN: {' '.join(cmd)}")
        return None

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        print(f"[grid] {label} TIMEOUT", flush=True)
        return {"config": {"max_holdings": max_holdings, "max_position_pct": max_position_pct, "max_holding_days": max_holding_days}, "status": "timeout"}

    if result.returncode != 0:
        print(f"[grid] {label} FAILED: {result.stderr[:500]}", flush=True)
        return {"config": {"max_holdings": max_holdings, "max_position_pct": max_position_pct, "max_holding_days": max_holding_days}, "status": "error", "stderr": result.stderr[:500]}

    # Parse summary.json for best train/test
    summary_json = run_dir / "summary.json"
    best_train = {}
    best_test = {}
    if summary_json.exists():
        try:
            sj = json.loads(summary_json.read_text())
            best_train = sj.get("train_best") or {}
            best_test = sj.get("test_best") or {}
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    config_info = {
        "max_holdings": max_holdings,
        "max_position_pct": max_position_pct,
        "max_holding_days": max_holding_days,
    }

    out = {
        "config": config_info,
        "label": label,
        "status": "ok",
        "best_train": best_train,
        "best_test": best_test,
        "run_dir": str(run_dir),
    }
    print(f"[grid] {label} done", flush=True)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-holdings-list", default="5,8")
    p.add_argument("--max-position-pct-list", default="0.08,0.15")
    p.add_argument("--max-holding-days-list", default="180,365")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    args = p.parse_args()

    holdings = [int(x) for x in args.max_holdings_list.split(",")]
    positions = [float(x) for x in args.max_position_pct_list.split(",")]
    days = [int(x) for x in args.max_holding_days_list.split(",")]

    configs = list(itertools.product(holdings, positions, days))
    print(f"[grid] total configs={len(configs)}: holdings={holdings} positions={positions} days={days}")
    print(f"[grid] started at {now_iso()}", flush=True)

    results = []
    for h, pct, d in configs:
        r = run_config(args.data_root, h, pct, d, args.output_dir,
                       top_n=args.top_n, dry_run=args.dry_run)
        if r:
            results.append(r)

    # Write summary
    output_path = Path(args.output_dir) / "grid_summary.json"
    output_path.write_text(json.dumps({
        "run_at": now_iso(),
        "configs_tested": len(configs),
        "results": results,
    }, indent=2, ensure_ascii=False) + "\n")

    print(f"[grid] finished at {now_iso()}, results written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
