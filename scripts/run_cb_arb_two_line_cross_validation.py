"""Cross-validate cb_arb HDRF baseline against self-loop best params.

This is an evaluation wrapper only. It reuses existing cb_arb value-gap and
verifier runners, writes comparison artifacts, and does not search parameters.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_selfpnl_regime_switch import (  # noqa: E402
    _default_ranks_path,
    _medium_params,
)
from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
)
from strategies.cb_arb.verifier import _load_trading_days, run_backtest  # noqa: E402


YEARS = [2019, 2020, 2021, 2022, 2023, 2024]
METRIC_KEYS = [
    "excess_return",
    "total_return",
    "sharpe",
    "max_drawdown",
    "total_trades",
    "win_rate",
    "n_days",
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _metrics(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{key}": metrics.get(key) for key in METRIC_KEYS}


def _trading_days_between(start: str, end: str) -> list[str]:
    return [d for d in _load_trading_days() if start <= d <= end]


def _pool_rows(
    ranks,
    data_root: Path,
    fixed_source: int,
    rule: str,
    hdrf_params: dict[str, Any],
    loop_params: dict[str, Any],
    sealed_pools: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pool in sealed_pools.get("pools", []):
        pool_id = int(pool["id"])
        event_ids = [str(x) for x in pool.get("event_ids", [])]
        if not event_ids:
            continue
        start, end = min(event_ids), max(event_ids)
        hdrf_result = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
            start,
            end,
            data_root,
            fixed_source,
            rule,
            hdrf_params,
        )
        loop_result = run_backtest(
            list(loop_params.get("weights") or []),
            dict(loop_params.get("thresholds") or {}),
            dict(loop_params.get("rules") or {}),
            oos_event_ids=set(event_ids),
        )
        hdrf_excess = float(hdrf_result["metrics"]["excess_return"])
        loop_excess = float(loop_result.oos_metrics["excess_return"])
        row = {
            "pool_id": pool_id,
            "start": start,
            "end": end,
            "event_count": len(event_ids),
            **_metrics("hdrf", hdrf_result["metrics"]),
            **_metrics("loop", loop_result.oos_metrics),
            "excess_gap_hdrf_minus_loop": round(hdrf_excess - loop_excess, 6),
            "abs_excess_gap": round(abs(hdrf_excess - loop_excess), 6),
            "converged_3pp": int(abs(hdrf_excess - loop_excess) <= 0.03),
        }
        rows.append(row)
        print(
            "[two-line] sealed_pool "
            f"pool={pool_id} hdrf_excess={hdrf_excess:.6f} "
            f"loop_excess={loop_excess:.6f}",
            flush=True,
        )
    return rows


def _year_rows(
    ranks,
    data_root: Path,
    fixed_source: int,
    rule: str,
    hdrf_params: dict[str, Any],
    loop_params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for year in YEARS:
        start = f"{year}0101"
        end = f"{year}1231"
        event_ids = _trading_days_between(start, end)
        hdrf_result = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
            start,
            end,
            data_root,
            fixed_source,
            rule,
            hdrf_params,
        )
        loop_result = run_backtest(
            list(loop_params.get("weights") or []),
            dict(loop_params.get("thresholds") or {}),
            dict(loop_params.get("rules") or {}),
            oos_event_ids=set(event_ids),
        )
        hdrf_excess = float(hdrf_result["metrics"]["excess_return"])
        loop_excess = float(loop_result.oos_metrics["excess_return"])
        row = {
            "year": year,
            "start": event_ids[0] if event_ids else start,
            "end": event_ids[-1] if event_ids else end,
            "event_count": len(event_ids),
            **_metrics("loop", loop_result.oos_metrics),
            **_metrics("hdrf", hdrf_result["metrics"]),
            "excess_gap_loop_minus_hdrf": round(loop_excess - hdrf_excess, 6),
            "abs_excess_gap": round(abs(loop_excess - hdrf_excess), 6),
            "converged_3pp": int(abs(loop_excess - hdrf_excess) <= 0.03),
        }
        rows.append(row)
        print(
            "[two-line] hdrf_holdout "
            f"year={year} loop_excess={loop_excess:.6f} "
            f"hdrf_excess={hdrf_excess:.6f}",
            flush=True,
        )
    return rows


def _summarise_gaps(rows: list[dict[str, Any]], gap_key: str) -> dict[str, Any]:
    gaps = [float(row[gap_key]) for row in rows]
    abs_gaps = [abs(x) for x in gaps]
    if not gaps:
        return {"n": 0}
    return {
        "n": len(gaps),
        "mean_gap": round(statistics.mean(gaps), 6),
        "median_gap": round(statistics.median(gaps), 6),
        "max_abs_gap": round(max(abs_gaps), 6),
        "mean_abs_gap": round(statistics.mean(abs_gaps), 6),
        "converged_3pp_count": sum(1 for x in abs_gaps if x <= 0.03),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=Path("data/cb_arb_concurrent_supervised_20260511_094500"))
    p.add_argument("--sealed-pools", type=Path, default=Path("data/cb_arb_rerun_fixed_20260510_124155/sealed_pools.json"))
    p.add_argument("--loop-best", type=Path, default=Path("data/cb_arb_rerun_fixed_20260510_124155/best_params.json"))
    p.add_argument("--panic-file", type=Path, default=None)
    p.add_argument("--ranks-path", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, default=Path("data/cb_arb_two_line_cross_validation_2026-05-15"))
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    panic_file = args.panic_file or args.data_root / "panic_detector_training/panic_detector_trained_daily.csv"
    ranks_path = args.ranks_path or _default_ranks_path(args.data_root)
    print(f"[two-line] ranks_path={ranks_path}", flush=True)
    ranks = _load_or_build_value_ranks(
        args.data_root,
        "20190101",
        "20260508",
        args.fixed_source,
        args.rule,
        ranks_path,
        True,
    )
    ranks["trade_date"] = ranks["trade_date"].astype(str)

    hdrf_params = _medium_params(panic_file, recovery_days=4, switch_hurdle_pct=0.15)
    loop_best = _read_json(args.loop_best)
    loop_params = dict(loop_best.get("params") or {})
    sealed_pools = _read_json(args.sealed_pools)

    pool_rows = _pool_rows(
        ranks,
        args.data_root,
        args.fixed_source,
        args.rule,
        hdrf_params,
        loop_params,
        sealed_pools,
    )
    year_rows = _year_rows(
        ranks,
        args.data_root,
        args.fixed_source,
        args.rule,
        hdrf_params,
        loop_params,
    )

    full_start = "20220101"
    full_end = "20260508"
    full_oos = _run_value_gap_backtest(
        ranks[(ranks["trade_date"] >= full_start) & (ranks["trade_date"] <= full_end)],
        full_start,
        full_end,
        args.data_root,
        args.fixed_source,
        args.rule,
        hdrf_params,
    )
    loop_best_excess = loop_best.get("metrics", {}).get("excess_return")
    full_hdrf_excess = full_oos["metrics"].get("excess_return")

    pool_gap_summary = _summarise_gaps(pool_rows, "excess_gap_hdrf_minus_loop")
    year_gap_summary = _summarise_gaps(year_rows, "excess_gap_loop_minus_hdrf")
    max_abs_gap = max(
        float(pool_gap_summary.get("max_abs_gap") or 0.0),
        float(year_gap_summary.get("max_abs_gap") or 0.0),
    )
    convergence = "converged" if max_abs_gap <= 0.03 else "not_converged"

    summary = {
        "created_at": _utcnow_iso(),
        "data_root": str(args.data_root),
        "sealed_pools": str(args.sealed_pools),
        "loop_best": str(args.loop_best),
        "loop_best_iteration": loop_best.get("iteration"),
        "loop_best_saved_excess_return": loop_best_excess,
        "hdrf_params": hdrf_params,
        "pool_gap_summary_hdrf_minus_loop": pool_gap_summary,
        "year_gap_summary_loop_minus_hdrf": year_gap_summary,
        "hdrf_full_oos_20220101_20260508_metrics": full_oos["metrics"],
        "hdrf_full_oos_minus_loop_saved_excess": (
            round(float(full_hdrf_excess) - float(loop_best_excess), 6)
            if full_hdrf_excess is not None and loop_best_excess is not None
            else None
        ),
        "convergence_rule": "converged if all compared abs excess gaps are <= 0.03",
        "convergence": convergence,
    }

    _write_csv(args.output_dir / "hdrf_best_on_sealed_pools.csv", pool_rows)
    _write_csv(args.output_dir / "loop_best_on_hdrf_holdout.csv", year_rows)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[two-line] wrote {args.output_dir}", flush=True)
    print(f"[two-line] convergence={convergence}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
