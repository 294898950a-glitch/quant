"""Search behavior-parameter regime combinations with a fixed valuation frame."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_daily_regime_switch import (  # noqa: E402
    DEFAULT_FIXED_FIELDS,
    _build_daily_features,
    _config_from_best,
    _merge_fixed_fields,
    _metrics_payload,
    _pool_events,
    _write_csv,
)
from strategies.cb_arb.verifier import run_backtest_dynamic  # noqa: E402


def _parse_pool_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    excess = [float(r["excess_return"]) for r in rows if r.get("excess_return") is not None]
    returns = [float(r["total_return"]) for r in rows if r.get("total_return") is not None]
    drawdowns = [float(r["max_drawdown"]) for r in rows if r.get("max_drawdown") is not None]
    return {
        "avg_excess_return": round(statistics.mean(excess), 6) if excess else None,
        "median_excess_return": round(statistics.median(excess), 6) if excess else None,
        "min_excess_return": round(min(excess), 6) if excess else None,
        "positive_excess_count": sum(1 for x in excess if x > 0),
        "avg_total_return": round(statistics.mean(returns), 6) if returns else None,
        "worst_drawdown": round(min(drawdowns), 6) if drawdowns else None,
    }


def _score_summary(summary: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(summary.get("avg_excess_return") or -999.0),
        float(summary.get("min_excess_return") or -999.0),
        float(summary.get("positive_excess_count") or 0.0),
    )


def _evaluate_combo(
    data_root: str,
    combo: tuple[int, int, int],
    fixed_source: int,
    fixed_fields: list[str],
    lookback_days: int,
    rule: str,
    target_pools: list[int] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(data_root)
    weak_source, neutral_source, strong_source = combo
    pools = _pool_events(root)
    features_by_date = _build_daily_features(lookback_days, rule)
    cfg_by_regime = {
        "weak": _config_from_best(root, weak_source),
        "neutral": _config_from_best(root, neutral_source),
        "strong": _config_from_best(root, strong_source),
    }
    fixed_cfg = _config_from_best(root, fixed_source)
    cfg_by_regime = _merge_fixed_fields(cfg_by_regime, fixed_cfg, fixed_fields)
    default_cfg = cfg_by_regime["neutral"]
    config_by_date = {
        date: cfg_by_regime[features["regime"]]
        for date, features in features_by_date.items()
    }

    rows: list[dict[str, Any]] = []
    pool_items = sorted(pools.items())
    if target_pools is not None:
        target_set = set(target_pools)
        pool_items = [(pool_id, event_ids) for pool_id, event_ids in pool_items if pool_id in target_set]

    for pool_id, event_ids in pool_items:
        result = run_backtest_dynamic(
            default_cfg,
            config_by_date,
            oos_event_ids=set(event_ids),
        )
        row = {
            "weak_source": weak_source,
            "neutral_source": neutral_source,
            "strong_source": strong_source,
            "pool": pool_id,
            "start": min(event_ids),
            "end": max(event_ids),
            **_metrics_payload(result.oos_metrics or {}),
        }
        rows.append(row)

    summary = {
        "weak_source": weak_source,
        "neutral_source": neutral_source,
        "strong_source": strong_source,
        **_summarise(rows),
    }
    return summary, rows


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--weak-candidates", default="0,2,3,4")
    p.add_argument("--neutral-candidates", default="0,1,2,4")
    p.add_argument("--strong-candidates", default="5,6,7")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--fixed-fields", default=",".join(DEFAULT_FIXED_FIELDS))
    p.add_argument("--lookback-days", type=int, default=252)
    p.add_argument("--rule", choices=["score", "trend_guard"], default="score")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument(
        "--target-pools",
        default="",
        help="Optional comma-separated target pools for a quick first pass.",
    )
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "behavior_regime_search"
    output_dir.mkdir(parents=True, exist_ok=True)

    weak_candidates = _parse_pool_list(args.weak_candidates)
    neutral_candidates = _parse_pool_list(args.neutral_candidates)
    strong_candidates = _parse_pool_list(args.strong_candidates)
    fixed_fields = [x.strip() for x in args.fixed_fields.split(",") if x.strip()]
    target_pools = _parse_pool_list(args.target_pools) if args.target_pools.strip() else None
    combos = list(itertools.product(weak_candidates, neutral_candidates, strong_candidates))

    summaries: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    print(
        "[behavior_search] "
        f"combos={len(combos)} fixed_source=pool_{args.fixed_source} "
        f"workers={args.max_workers} target_pools={target_pools or 'all'}",
        flush=True,
    )

    with ProcessPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        futures = [
            executor.submit(
                _evaluate_combo,
                str(root),
                combo,
                args.fixed_source,
                fixed_fields,
                args.lookback_days,
                args.rule,
                target_pools,
            )
            for combo in combos
        ]
        for idx, fut in enumerate(as_completed(futures), 1):
            summary, rows = fut.result()
            summaries.append(summary)
            detail_rows.extend(rows)
            best = max(summaries, key=_score_summary)
            print(
                "[behavior_search] "
                f"done={idx}/{len(combos)} combo="
                f"{summary['weak_source']},{summary['neutral_source']},{summary['strong_source']} "
                f"avg={summary['avg_excess_return']} min={summary['min_excess_return']} "
                f"pos={summary['positive_excess_count']} best="
                f"{best['weak_source']},{best['neutral_source']},{best['strong_source']} "
                f"best_avg={best['avg_excess_return']}",
                flush=True,
            )

    summaries.sort(key=_score_summary, reverse=True)
    detail_rows.sort(key=lambda r: (r["weak_source"], r["neutral_source"], r["strong_source"], r["pool"]))
    payload = {
        "data_root": str(root),
        "fixed_source": args.fixed_source,
        "fixed_fields": fixed_fields,
        "weak_candidates": weak_candidates,
        "neutral_candidates": neutral_candidates,
        "strong_candidates": strong_candidates,
        "lookback_days": args.lookback_days,
        "rule": args.rule,
        "target_pools": target_pools,
        "summary": summaries,
        "rows": detail_rows,
    }
    (output_dir / "behavior_regime_search.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "summary.csv", summaries)
    _write_csv(output_dir / "rows.csv", detail_rows)
    print("[behavior_search] top5", json.dumps(summaries[:5], ensure_ascii=False), flush=True)
    print(f"[behavior_search] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
