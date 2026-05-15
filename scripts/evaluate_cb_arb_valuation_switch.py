"""Counterfactual test: keep regime behavior fixed, switch valuation fields."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_daily_regime_switch import (  # noqa: E402
    _build_daily_features,
    _config_from_best,
    _metrics_payload,
    _pool_events,
    _write_csv,
)
from strategies.cb_arb.verifier import CBArbConfig, run_backtest_dynamic  # noqa: E402

VALUATION_FIELDS = (
    "vol_window_days",
    "vol_multiplier",
    "credit_spread_aaa_bp",
    "credit_spread_aa_bp",
)

FIXED_NON_BEHAVIOR_FIELDS = (
    "rank_buy_pct",
    "rank_sell_pct",
    "fee_pct",
    "initial_capital",
)


def _copy_fields(cfg: CBArbConfig, source: CBArbConfig, fields: tuple[str, ...]) -> CBArbConfig:
    out = replace(cfg)
    for field in fields:
        setattr(out, field, getattr(source, field))
    return out


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    excess = [float(r["excess_return"]) for r in rows if r.get("excess_return") is not None]
    returns = [float(r["total_return"]) for r in rows if r.get("total_return") is not None]
    drawdowns = [float(r["max_drawdown"]) for r in rows if r.get("max_drawdown") is not None]
    return {
        "evaluated_pools": len(rows),
        "avg_excess_return": round(statistics.mean(excess), 6) if excess else None,
        "median_excess_return": round(statistics.median(excess), 6) if excess else None,
        "min_excess_return": round(min(excess), 6) if excess else None,
        "positive_excess_count": sum(1 for x in excess if x > 0),
        "avg_total_return": round(statistics.mean(returns), 6) if returns else None,
        "worst_drawdown": round(min(drawdowns), 6) if drawdowns else None,
    }


def _parse_sources(value: str) -> dict[str, int]:
    parts = [x.strip() for x in value.split(",") if x.strip()]
    if len(parts) != 4:
        raise ValueError("sources must be weak,flat_weak,neutral,strong")
    keys = ["weak", "flat_weak", "neutral", "strong"]
    return {key: int(part) for key, part in zip(keys, parts)}


def _build_configs(
    root: Path,
    behavior_sources: dict[str, int],
    valuation_sources: dict[str, int],
    fixed_source: int,
    strong_max_position_pct: float | None,
) -> dict[str, CBArbConfig]:
    fixed_cfg = _config_from_best(root, fixed_source)
    configs: dict[str, CBArbConfig] = {}
    for regime, behavior_source in behavior_sources.items():
        cfg = _config_from_best(root, behavior_source)
        cfg = _copy_fields(cfg, fixed_cfg, FIXED_NON_BEHAVIOR_FIELDS)
        valuation_cfg = _config_from_best(root, valuation_sources[regime])
        cfg = _copy_fields(cfg, valuation_cfg, VALUATION_FIELDS)
        if regime == "strong" and strong_max_position_pct is not None:
            cfg = replace(cfg, max_position_pct=float(strong_max_position_pct))
        configs[regime] = cfg
    return configs


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--behavior-sources", default="0,4,2,6")
    p.add_argument("--valuation-sources", default="2,2,2,2")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--lookback-days", type=int, default=252)
    p.add_argument("--rule", choices=["score", "trend_guard", "score_4state"], default="score_4state")
    p.add_argument("--strong-max-position-pct", type=float, default=0.08)
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "valuation_switch_eval"
    output_dir.mkdir(parents=True, exist_ok=True)

    behavior_sources = _parse_sources(args.behavior_sources)
    valuation_sources = _parse_sources(args.valuation_sources)
    features_by_date = _build_daily_features(args.lookback_days, args.rule)
    cfg_by_regime = _build_configs(
        root,
        behavior_sources,
        valuation_sources,
        args.fixed_source,
        args.strong_max_position_pct,
    )
    config_by_date = {
        date: cfg_by_regime[features["regime"]]
        for date, features in features_by_date.items()
    }
    pools = _pool_events(root)

    rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    for pool_id, event_ids in sorted(pools.items()):
        event_set = set(event_ids)
        pool_features = [features_by_date[d] for d in sorted(event_set) if d in features_by_date]
        counts = {
            "weak_days": sum(1 for f in pool_features if f["regime"] == "weak"),
            "flat_weak_days": sum(1 for f in pool_features if f["regime"] == "flat_weak"),
            "neutral_days": sum(1 for f in pool_features if f["regime"] == "neutral"),
            "strong_days": sum(1 for f in pool_features if f["regime"] == "strong"),
        }
        for f in pool_features:
            daily_rows.append({"pool": pool_id, **f})
        result = run_backtest_dynamic(
            cfg_by_regime["neutral"],
            config_by_date,
            oos_event_ids=event_set,
        )
        row = {
            "pool": pool_id,
            "start": min(event_ids),
            "end": max(event_ids),
            **counts,
            **_metrics_payload(result.oos_metrics or {}),
        }
        rows.append(row)
        print(
            "[valuation_switch] "
            f"pool={pool_id} excess={row['excess_return']} return={row['total_return']}",
            flush=True,
        )

    summary = {
        "behavior_sources": behavior_sources,
        "valuation_sources": valuation_sources,
        "fixed_source": args.fixed_source,
        "lookback_days": args.lookback_days,
        "rule": args.rule,
        "strong_max_position_pct": args.strong_max_position_pct,
        "valuation_fields": list(VALUATION_FIELDS),
        "fixed_non_behavior_fields": list(FIXED_NON_BEHAVIOR_FIELDS),
        **_summarise(rows),
    }
    (output_dir / "valuation_switch_eval.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "valuation_switch_eval.csv", rows)
    _write_csv(output_dir / "daily_regime_by_day.csv", daily_rows)
    print("[valuation_switch] summary", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[valuation_switch] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
