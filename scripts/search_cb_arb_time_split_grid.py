"""Time-split grid search for cb_arb behavior parameters.

This keeps valuation and cheap/expensive thresholds fixed, searches a small
behavior grid on an older training period, then evaluates the top candidates
on a later untouched historical test period.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_daily_regime_switch import (  # noqa: E402
    _build_daily_features,
    _config_from_best,
    _metrics_payload,
    _write_csv,
)
from scripts.evaluate_cb_arb_valuation_switch import (  # noqa: E402
    FIXED_NON_BEHAVIOR_FIELDS,
    VALUATION_FIELDS,
    _copy_fields,
)
from strategies.cb_arb.verifier import CBArbConfig, _load_trading_days, run_backtest_dynamic  # noqa: E402


REGIMES = ("weak", "flat_weak", "neutral", "strong")


def _date_ids(start: str, end: str) -> list[str]:
    return [d for d in _load_trading_days() if start <= d <= end]


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


def _score(metrics: dict[str, Any]) -> float:
    avg = float(metrics.get("avg_excess_return") or -999.0)
    worst = float(metrics.get("min_excess_return") or -999.0)
    dd = abs(float(metrics.get("worst_drawdown") or 0.0))
    return round(avg + 0.5 * worst - 0.25 * dd, 6)


def _base_configs(root: Path, fixed_source: int) -> dict[str, CBArbConfig]:
    fixed_cfg = _config_from_best(root, fixed_source)
    source_for = {
        "weak": 0,
        "flat_weak": 4,
        "neutral": 2,
        "strong": 6,
    }
    configs: dict[str, CBArbConfig] = {}
    for regime, source in source_for.items():
        cfg = _config_from_best(root, source)
        cfg = _copy_fields(cfg, fixed_cfg, VALUATION_FIELDS)
        cfg = _copy_fields(cfg, fixed_cfg, FIXED_NON_BEHAVIOR_FIELDS)
        if regime == "strong":
            cfg = replace(cfg, max_position_pct=0.08)
        configs[regime] = cfg
    return configs


def _candidate_configs(base: dict[str, CBArbConfig]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add(name: str, regime: str | None, values: dict[str, Any]) -> None:
        cfgs = {key: replace(value) for key, value in base.items()}
        if regime is not None:
            cfg = cfgs[regime]
            for key, value in values.items():
                setattr(cfg, key, value)
            cfgs[regime] = cfg
        candidates.append({
            "name": name,
            "changed_regime": regime or "none",
            "values": values,
            "configs": cfgs,
        })

    add("base", None, {})

    for max_pos in [0.06, 0.08, 0.10]:
        for stop_loss in [-0.08, -0.10, -0.12]:
            add(
                f"strong_pos_{max_pos}_sl_{stop_loss}",
                "strong",
                {"max_position_pct": max_pos, "stop_loss_pct": stop_loss},
            )

    for max_pos in [0.02, 0.03]:
        for rating in [2, 4]:
            add(
                f"flat_pos_{max_pos}_rating_{rating}",
                "flat_weak",
                {"max_position_pct": max_pos, "rating_floor_int": rating},
            )

    for rating in [2, 3, 4]:
        add(
            f"weak_rating_{rating}",
            "weak",
            {"rating_floor_int": rating},
        )

    for max_pos in [0.03, 0.04, 0.05]:
        add(
            f"neutral_pos_{max_pos}",
            "neutral",
            {"max_position_pct": max_pos},
        )

    return candidates


def _regime_variant(
    base: dict[str, CBArbConfig],
    regime: str,
    name: str,
    values: dict[str, Any],
) -> tuple[str, CBArbConfig, dict[str, Any]]:
    cfg = replace(base[regime])
    for key, value in values.items():
        setattr(cfg, key, value)
    return name, cfg, values


def _full_combo_candidates(base: dict[str, CBArbConfig]) -> list[dict[str, Any]]:
    variants = {
        "weak": [
            _regime_variant(base, "weak", "weak_base", {}),
            _regime_variant(base, "weak", "weak_rating_3", {"rating_floor_int": 3}),
            _regime_variant(base, "weak", "weak_rating_4", {"rating_floor_int": 4}),
        ],
        "flat_weak": [
            _regime_variant(base, "flat_weak", "flat_base", {}),
            _regime_variant(
                base,
                "flat_weak",
                "flat_pos_0.02_rating_2",
                {"max_position_pct": 0.02, "rating_floor_int": 2},
            ),
            _regime_variant(
                base,
                "flat_weak",
                "flat_pos_0.03_rating_4",
                {"max_position_pct": 0.03, "rating_floor_int": 4},
            ),
        ],
        "neutral": [
            _regime_variant(base, "neutral", "neutral_base", {}),
            _regime_variant(base, "neutral", "neutral_pos_0.04", {"max_position_pct": 0.04}),
        ],
        "strong": [
            _regime_variant(base, "strong", "strong_base", {}),
            _regime_variant(
                base,
                "strong",
                "strong_pos_0.06_sl_-0.08",
                {"max_position_pct": 0.06, "stop_loss_pct": -0.08},
            ),
            _regime_variant(
                base,
                "strong",
                "strong_pos_0.10_sl_-0.08",
                {"max_position_pct": 0.10, "stop_loss_pct": -0.08},
            ),
            _regime_variant(
                base,
                "strong",
                "strong_pos_0.10_sl_-0.10",
                {"max_position_pct": 0.10, "stop_loss_pct": -0.10},
            ),
        ],
    }

    candidates: list[dict[str, Any]] = []
    for weak, flat, neutral, strong in product(
        variants["weak"],
        variants["flat_weak"],
        variants["neutral"],
        variants["strong"],
    ):
        pieces = {
            "weak": weak,
            "flat_weak": flat,
            "neutral": neutral,
            "strong": strong,
        }
        name = "|".join(piece[0] for piece in pieces.values())
        configs = {regime: piece[1] for regime, piece in pieces.items()}
        values = {regime: piece[2] for regime, piece in pieces.items()}
        candidates.append({
            "name": name,
            "changed_regime": "full_combo",
            "values": values,
            "configs": configs,
        })
    return candidates


def _strong_exit_candidates(base: dict[str, CBArbConfig]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for rank_sell, hold_days in product([0.50, 0.70, 0.80, 0.90], [90, 150, 180, 240]):
        cfgs = {key: replace(value) for key, value in base.items()}
        cfgs["strong"] = replace(
            cfgs["strong"],
            rank_sell_pct=rank_sell,
            max_holding_days=hold_days,
        )
        candidates.append({
            "name": f"strong_exit_sell_{rank_sell}_hold_{hold_days}",
            "changed_regime": "strong",
            "values": {
                "rank_sell_pct": rank_sell,
                "max_holding_days": hold_days,
            },
            "configs": cfgs,
        })
    return candidates


def _evaluate(
    cfgs: dict[str, CBArbConfig],
    features_by_date: dict[str, dict[str, Any]],
    event_ids: list[str],
) -> dict[str, Any]:
    config_by_date = {
        date: cfgs[features["regime"]]
        for date, features in features_by_date.items()
        if features["regime"] in cfgs
    }
    result = run_backtest_dynamic(
        cfgs["neutral"],
        config_by_date,
        oos_event_ids=set(event_ids),
    )
    return _metrics_payload(result.oos_metrics or {})


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20140101")
    p.add_argument("--train-end", default="20181231")
    p.add_argument("--test-start", default="20190101")
    p.add_argument("--test-end", default="20211231")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--lookback-days", type=int, default=252)
    p.add_argument("--rule", choices=["score", "trend_guard", "score_4state"], default="score_4state")
    p.add_argument("--mode", choices=["local", "full_combo", "strong_exit"], default="local")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "time_split_grid"
    output_dir.mkdir(parents=True, exist_ok=True)

    train_ids = _date_ids(args.train_start, args.train_end)
    test_ids = _date_ids(args.test_start, args.test_end)
    if not train_ids or not test_ids:
        raise ValueError("empty train/test split")

    features_by_date = _build_daily_features(args.lookback_days, args.rule)
    base = _base_configs(root, args.fixed_source)
    if args.mode == "full_combo":
        candidates = _full_combo_candidates(base)
    elif args.mode == "strong_exit":
        candidates = _strong_exit_candidates(base)
    else:
        candidates = _candidate_configs(base)

    train_rows: list[dict[str, Any]] = []
    print(
        "[time_grid] "
        f"train={train_ids[0]}..{train_ids[-1]} n={len(train_ids)} "
        f"test={test_ids[0]}..{test_ids[-1]} n={len(test_ids)} "
        f"candidates={len(candidates)}",
        flush=True,
    )
    for idx, cand in enumerate(candidates, 1):
        metrics = _evaluate(cand["configs"], features_by_date, train_ids)
        row = {
            "candidate": cand["name"],
            "changed_regime": cand["changed_regime"],
            "values_json": json.dumps(cand["values"], sort_keys=True),
            **metrics,
        }
        row["score"] = _score(_summarise([metrics]))
        train_rows.append(row)
        print(
            "[time_grid] "
            f"train {idx}/{len(candidates)} {cand['name']} "
            f"excess={row['excess_return']} dd={row['max_drawdown']} score={row['score']}",
            flush=True,
        )

    train_rows.sort(key=lambda r: float(r["score"]), reverse=True)
    top_names = {r["candidate"] for r in train_rows[: args.top_n]}

    test_rows: list[dict[str, Any]] = []
    for cand in candidates:
        if cand["name"] not in top_names:
            continue
        metrics = _evaluate(cand["configs"], features_by_date, test_ids)
        row = {
            "candidate": cand["name"],
            "changed_regime": cand["changed_regime"],
            "values_json": json.dumps(cand["values"], sort_keys=True),
            **metrics,
        }
        row["score"] = _score(_summarise([metrics]))
        test_rows.append(row)
        print(
            "[time_grid] "
            f"test {cand['name']} excess={row['excess_return']} "
            f"dd={row['max_drawdown']} score={row['score']}",
            flush=True,
        )

    test_rows.sort(key=lambda r: float(r["score"]), reverse=True)
    summary = {
        "train_start": train_ids[0],
        "train_end": train_ids[-1],
        "test_start": test_ids[0],
        "test_end": test_ids[-1],
        "fixed_source": args.fixed_source,
        "rule": args.rule,
        "mode": args.mode,
        "n_candidates": len(candidates),
        "top_n": args.top_n,
        "train_best": train_rows[0] if train_rows else None,
        "test_best": test_rows[0] if test_rows else None,
    }
    (output_dir / "time_split_grid.json").write_text(
        json.dumps(
            {"summary": summary, "train_rows": train_rows, "test_rows": test_rows},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "train_summary.csv", train_rows)
    _write_csv(output_dir / "test_summary.csv", test_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[time_grid] summary", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[time_grid] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
