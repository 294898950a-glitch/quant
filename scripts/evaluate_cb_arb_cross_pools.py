"""Evaluate each pool's best cb_arb params on every pool.

This is the 8x8 check after concurrent tuning:

- rows: source pool whose ``best_params.json`` supplies the parameter set
- columns: target pool whose dates are used for evaluation

The script does not call an LLM and does not edit parameters. It only reads
saved best params and runs the verifier on each target pool.
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

from strategies.cb_arb.verifier import run_backtest


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pool_event_ids(root: Path, pool_id: int) -> list[str]:
    pool_file = root / f"pool_{pool_id}" / "sealed_pools.json"
    if not pool_file.exists():
        pool_file = root / "sealed_pools.json"
    data = _read_json(pool_file)
    for pool in data.get("pools", []):
        if int(pool.get("id")) == pool_id or len(data.get("pools", [])) == 1:
            return [str(x) for x in pool.get("event_ids", [])]
    raise KeyError(f"pool {pool_id} not found in {pool_file}")


def _best_params(root: Path, pool_id: int) -> dict[str, Any] | None:
    path = root / f"pool_{pool_id}" / "best_params.json"
    if not path.exists():
        return None
    return _read_json(path)


def _metrics_payload(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "excess_return": metrics.get("excess_return"),
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "total_trades": metrics.get("total_trades"),
        "win_rate": metrics.get("win_rate"),
        "n_days": metrics.get("n_days"),
    }


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarise(rows: list[dict[str, Any]], source_pool: int) -> dict[str, Any]:
    source_rows = [r for r in rows if r["source_pool"] == source_pool]
    excess = [_as_float(r.get("excess_return")) for r in source_rows]
    excess_vals = [x for x in excess if x is not None]
    returns = [_as_float(r.get("total_return")) for r in source_rows]
    return_vals = [x for x in returns if x is not None]
    drawdowns = [_as_float(r.get("max_drawdown")) for r in source_rows]
    drawdown_vals = [x for x in drawdowns if x is not None]
    if not excess_vals:
        return {
            "source_pool": source_pool,
            "evaluated_targets": 0,
        }
    return {
        "source_pool": source_pool,
        "evaluated_targets": len(excess_vals),
        "avg_excess_return": round(statistics.mean(excess_vals), 6),
        "median_excess_return": round(statistics.median(excess_vals), 6),
        "min_excess_return": round(min(excess_vals), 6),
        "max_excess_return": round(max(excess_vals), 6),
        "positive_excess_count": sum(1 for x in excess_vals if x > 0),
        "avg_total_return": round(statistics.mean(return_vals), 6) if return_vals else None,
        "worst_drawdown": round(min(drawdown_vals), 6) if drawdown_vals else None,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--n-pools", type=int, default=8)
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument(
        "--allow-partial",
        action="store_true",
        default=False,
        help="Evaluate available best_params.json files instead of requiring all pools.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "cross_eval"
    output_dir.mkdir(parents=True, exist_ok=True)

    best_by_pool: dict[int, dict[str, Any]] = {}
    missing: list[int] = []
    for source_pool in range(args.n_pools):
        best = _best_params(root, source_pool)
        if best is None:
            missing.append(source_pool)
            continue
        best_by_pool[source_pool] = best

    if missing and not args.allow_partial:
        print(
            f"[cross_eval] missing best_params for pools {missing}; "
            "rerun with --allow-partial to evaluate available pools",
            file=sys.stderr,
        )
        return 1

    target_events = {
        target_pool: _pool_event_ids(root, target_pool)
        for target_pool in range(args.n_pools)
    }

    rows: list[dict[str, Any]] = []
    for source_pool, best in sorted(best_by_pool.items()):
        params = best.get("params") or {}
        weights = list(params.get("weights") or [])
        thresholds = dict(params.get("thresholds") or {})
        rules = dict(params.get("rules") or {})
        for target_pool, event_ids in target_events.items():
            result = run_backtest(
                weights,
                thresholds,
                rules,
                oos_event_ids=set(event_ids),
            )
            metrics = _metrics_payload(result.oos_metrics or {})
            row = {
                "timestamp_iso": _utcnow_iso(),
                "source_pool": source_pool,
                "source_best_iteration": best.get("iteration"),
                "target_pool": target_pool,
                "target_start": min(event_ids) if event_ids else None,
                "target_end": max(event_ids) if event_ids else None,
                **metrics,
            }
            rows.append(row)
            print(
                "[cross_eval] "
                f"source=pool_{source_pool} target=pool_{target_pool} "
                f"excess={metrics.get('excess_return')} "
                f"return={metrics.get('total_return')}",
                flush=True,
            )

    summary = [_summarise(rows, source_pool) for source_pool in sorted(best_by_pool)]
    summary.sort(
        key=lambda r: (
            r.get("positive_excess_count") or 0,
            r.get("avg_excess_return") or -999,
            r.get("min_excess_return") or -999,
        ),
        reverse=True,
    )

    payload = {
        "data_root": str(root),
        "n_pools": args.n_pools,
        "created_at": _utcnow_iso(),
        "missing_best_params": missing,
        "rows": rows,
        "summary": summary,
    }
    (output_dir / "cross_eval.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "cross_eval.csv", rows)
    _write_csv(output_dir / "summary.csv", summary)
    print(f"[cross_eval] wrote {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
