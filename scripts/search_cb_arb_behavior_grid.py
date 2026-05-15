"""Local grid search for cb_arb behavior parameters with fixed valuation."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
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
from scripts.search_cb_arb_behavior_regimes import _parse_pool_list  # noqa: E402
from strategies.cb_arb.verifier import CBArbConfig, run_backtest_dynamic  # noqa: E402


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


def _make_cfg(base: CBArbConfig, values: dict[str, Any]) -> CBArbConfig:
    cfg = replace(base)
    for key, value in values.items():
        setattr(cfg, key, value)
    return cfg


def _candidate_grid(base: CBArbConfig, grid: dict[str, list[Any]], prefix: str) -> list[tuple[str, CBArbConfig]]:
    keys = list(grid)
    candidates: list[tuple[str, CBArbConfig]] = []
    for idx, vals in enumerate(itertools.product(*(grid[k] for k in keys))):
        values = dict(zip(keys, vals))
        name = prefix + "_" + str(idx)
        candidates.append((name, _make_cfg(base, values)))
    return candidates


def _build_candidates(root: Path, fixed_fields: list[str], fixed_source: int) -> dict[str, list[tuple[str, CBArbConfig]]]:
    base_by_regime = {
        "weak": _config_from_best(root, 0),
        "neutral": _config_from_best(root, 2),
        "strong": _config_from_best(root, 6),
    }
    fixed_cfg = _config_from_best(root, fixed_source)
    base_by_regime = _merge_fixed_fields(base_by_regime, fixed_cfg, fixed_fields)

    weak_grid = {
        "max_position_pct": [0.03],
        "max_holdings": [20, 30],
        "max_holding_days": [60, 90],
        "stop_loss_pct": [-0.08],
        "rating_floor_int": [2, 4],
    }
    neutral_grid = {
        "max_position_pct": [0.03, 0.04],
        "max_holdings": [30],
        "max_holding_days": [90],
        "stop_loss_pct": [-0.08],
        "rating_floor_int": [2],
    }
    strong_grid = {
        "max_position_pct": [0.06, 0.08],
        "max_holdings": [30],
        "max_holding_days": [90, 120],
        "stop_loss_pct": [-0.10],
        "rating_floor_int": [1, 2],
    }

    return {
        "weak": [("weak_base", base_by_regime["weak"])]
        + _candidate_grid(base_by_regime["weak"], weak_grid, "weak"),
        "neutral": [("neutral_base", base_by_regime["neutral"])]
        + _candidate_grid(base_by_regime["neutral"], neutral_grid, "neutral"),
        "strong": [("strong_base", base_by_regime["strong"])]
        + _candidate_grid(base_by_regime["strong"], strong_grid, "strong"),
    }


def _evaluate_combo(
    data_root: str,
    weak_name: str,
    weak_cfg: CBArbConfig,
    neutral_name: str,
    neutral_cfg: CBArbConfig,
    strong_name: str,
    strong_cfg: CBArbConfig,
    lookback_days: int,
    rule: str,
    target_pools: list[int] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(data_root)
    pools = _pool_events(root)
    features_by_date = _build_daily_features(lookback_days, rule)
    cfg_by_regime = {
        "weak": weak_cfg,
        "neutral": neutral_cfg,
        "strong": strong_cfg,
    }
    config_by_date = {
        date: cfg_by_regime[features["regime"]]
        for date, features in features_by_date.items()
    }
    pool_items = sorted(pools.items())
    if target_pools is not None:
        target_set = set(target_pools)
        pool_items = [(pool_id, event_ids) for pool_id, event_ids in pool_items if pool_id in target_set]

    rows: list[dict[str, Any]] = []
    for pool_id, event_ids in pool_items:
        result = run_backtest_dynamic(
            neutral_cfg,
            config_by_date,
            oos_event_ids=set(event_ids),
        )
        rows.append({
            "weak_candidate": weak_name,
            "neutral_candidate": neutral_name,
            "strong_candidate": strong_name,
            "pool": pool_id,
            "start": min(event_ids),
            "end": max(event_ids),
            **_metrics_payload(result.oos_metrics or {}),
        })

    summary = {
        "weak_candidate": weak_name,
        "neutral_candidate": neutral_name,
        "strong_candidate": strong_name,
        **_summarise(rows),
    }
    return summary, rows


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--fixed-fields", default=",".join(DEFAULT_FIXED_FIELDS))
    p.add_argument("--lookback-days", type=int, default=252)
    p.add_argument("--rule", choices=["score", "trend_guard"], default="score")
    p.add_argument("--target-pools", default="0,3,6,7")
    p.add_argument("--max-workers", type=int, default=2)
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument(
        "--only-combo",
        default="",
        help="Optional candidate names as weak,neutral,strong.",
    )
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "behavior_grid_search"
    output_dir.mkdir(parents=True, exist_ok=True)

    fixed_fields = [x.strip() for x in args.fixed_fields.split(",") if x.strip()]
    target_pools = _parse_pool_list(args.target_pools) if args.target_pools.strip() else None
    candidates = _build_candidates(root, fixed_fields, args.fixed_source)

    # Keep the first pass small enough for the VM: vary one regime at a time
    # around the current best 0/2/6 shape, then combine the strongest findings.
    combos: list[tuple[str, CBArbConfig, str, CBArbConfig, str, CBArbConfig]] = []
    base_w = ("weak_base", candidates["weak"][0][1])
    base_n = ("neutral_base", candidates["neutral"][0][1])
    base_s = ("strong_base", candidates["strong"][0][1])
    for w in candidates["weak"]:
        combos.append((w[0], w[1], base_n[0], base_n[1], base_s[0], base_s[1]))
    for n in candidates["neutral"]:
        combos.append((base_w[0], base_w[1], n[0], n[1], base_s[0], base_s[1]))
    for s in candidates["strong"]:
        combos.append((base_w[0], base_w[1], base_n[0], base_n[1], s[0], s[1]))
    if args.only_combo.strip():
        wanted = [x.strip() for x in args.only_combo.split(",")]
        if len(wanted) != 3:
            raise ValueError("--only-combo must be weak,neutral,strong")
        combos = [
            combo for combo in combos
            if [combo[0], combo[2], combo[4]] == wanted
        ]
        if not combos:
            raise ValueError(f"combo not found: {args.only_combo}")

    print(
        "[behavior_grid] "
        f"combos={len(combos)} workers={args.max_workers} target_pools={target_pools or 'all'}",
        flush=True,
    )
    summaries: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        futures = [
            executor.submit(
                _evaluate_combo,
                str(root),
                weak_name,
                weak_cfg,
                neutral_name,
                neutral_cfg,
                strong_name,
                strong_cfg,
                args.lookback_days,
                args.rule,
                target_pools,
            )
            for weak_name, weak_cfg, neutral_name, neutral_cfg, strong_name, strong_cfg in combos
        ]
        for idx, fut in enumerate(as_completed(futures), 1):
            summary, rows = fut.result()
            summaries.append(summary)
            detail_rows.extend(rows)
            best = max(summaries, key=_score_summary)
            print(
                "[behavior_grid] "
                f"done={idx}/{len(combos)} avg={summary['avg_excess_return']} "
                f"min={summary['min_excess_return']} best={best['avg_excess_return']} "
                f"best_combo={best['weak_candidate']},{best['neutral_candidate']},{best['strong_candidate']}",
                flush=True,
            )

    summaries.sort(key=_score_summary, reverse=True)
    detail_rows.sort(key=lambda r: (
        r["weak_candidate"],
        r["neutral_candidate"],
        r["strong_candidate"],
        r["pool"],
    ))
    payload = {
        "data_root": str(root),
        "fixed_source": args.fixed_source,
        "fixed_fields": fixed_fields,
        "target_pools": target_pools,
        "summary": summaries,
        "rows": detail_rows,
    }
    (output_dir / "behavior_grid_search.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "summary.csv", summaries[:args.top_n])
    _write_csv(output_dir / "rows.csv", detail_rows)
    print("[behavior_grid] top5", json.dumps(summaries[:5], ensure_ascii=False), flush=True)
    print(f"[behavior_grid] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
