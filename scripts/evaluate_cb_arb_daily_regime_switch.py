"""Evaluate daily market-strength switching for cb_arb.

The rule uses only information available before each trading day:

1. index percentile
2. index slope
3. amount percentile
4. breadth
5. drawdown
"""

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

from strategies.cb_arb.verifier import (  # noqa: E402
    CBArbConfig,
    _load_cb_daily,
    _unpack_config,
    run_backtest_dynamic,
)

DEFAULT_FIXED_FIELDS = (
    "vol_window_days",
    "vol_multiplier",
    "rank_buy_pct",
    "rank_sell_pct",
    "credit_spread_aaa_bp",
    "credit_spread_aa_bp",
    "fee_pct",
    "initial_capital",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _best_params(root: Path, source_pool: int) -> dict[str, Any]:
    return _read_json(root / f"pool_{source_pool}" / "best_params.json")


def _pool_events(root: Path) -> dict[int, list[str]]:
    pool_file = root / "sealed_pools.json"
    data = _read_json(pool_file)
    return {int(p["id"]): [str(x) for x in p["event_ids"]] for p in data["pools"]}


def _percentile(value: float, sample: list[float]) -> float:
    if not sample:
        return 0.5
    return sum(1 for x in sample if x <= value) / len(sample)


def _classify_score(features: dict[str, Any]) -> str:
    strong, weak = _score(features)
    if strong >= 3 and weak <= 1:
        return "strong"
    if weak >= 3 and strong <= 1:
        return "weak"
    return "neutral"


def _classify_trend_guard(features: dict[str, Any]) -> str:
    strong = 0
    weak = 0

    index_pctile = float(features["index_pctile"])
    ret_20d = float(features["ret_20d"])
    ret_60d = float(features["ret_60d"])
    amount_pctile = float(features["amount_pctile"])
    breadth_20d = float(features["breadth_20d"])
    drawdown = float(features["drawdown"])
    slope_strong = ret_20d > 0 and ret_60d > 0
    slope_weak = ret_20d < 0 and ret_60d < 0

    if index_pctile >= 0.65:
        strong += 1
    elif index_pctile <= 0.35:
        weak += 1

    if slope_strong:
        strong += 1
    elif slope_weak:
        weak += 1

    if amount_pctile >= 0.55:
        strong += 1
    elif amount_pctile <= 0.35:
        weak += 1

    if breadth_20d >= 0.55:
        strong += 1
    elif breadth_20d <= 0.45:
        weak += 1

    if drawdown > -0.08:
        strong += 1
    elif drawdown <= -0.15:
        weak += 1

    if slope_strong and drawdown > -0.12 and strong >= 3 and weak <= 1:
        return "strong"
    if slope_weak and weak >= 2 and strong <= 2:
        return "weak"
    return "neutral"


def _classify_score_4state(features: dict[str, Any]) -> str:
    strong, weak = _score(features)
    index_pctile = float(features["index_pctile"])
    ret_60d = float(features["ret_60d"])
    breadth_20d = float(features["breadth_20d"])
    drawdown = float(features["drawdown"])

    if weak >= 3 and strong <= 1:
        return "weak"
    if (
        ret_60d <= 0.01
        and (
            (index_pctile <= 0.45 and drawdown <= -0.06)
            or (drawdown <= -0.10 and breadth_20d <= 0.52)
            or (breadth_20d <= 0.50 and index_pctile <= 0.60)
        )
    ):
        return "flat_weak"
    if strong >= 3 and weak <= 1:
        return "strong"
    if drawdown <= -0.12 and breadth_20d <= 0.52:
        return "flat_weak"
    return "neutral"


def _classify(features: dict[str, Any], rule: str) -> str:
    if rule == "score_4state":
        return _classify_score_4state(features)
    if rule == "trend_guard":
        return _classify_trend_guard(features)
    return _classify_score(features)


def _build_daily_features(lookback_days: int, rule: str) -> dict[str, dict[str, Any]]:
    daily = _load_cb_daily()
    daily = daily.sort_values(["ts_code", "trade_date"]).copy()
    daily["prev_close"] = daily.groupby("ts_code")["close"].shift(1)
    daily["up"] = daily["close"] > daily["prev_close"]

    by_day = daily.groupby("trade_date").agg(
        index_level=("close", "mean"),
        amount=("amount_yuan", "sum"),
        breadth=("up", "mean"),
    ).sort_index()

    days = list(by_day.index.astype(str))
    features_by_date: dict[str, dict[str, Any]] = {}
    for idx, date in enumerate(days):
        history = by_day.iloc[:idx]
        if history.empty:
            features_by_date[date] = {
                "date": date,
                "index_pctile": 0.5,
                "ret_20d": 0.0,
                "ret_60d": 0.0,
                "amount_pctile": 0.5,
                "breadth_20d": 0.5,
                "drawdown": 0.0,
                "strong_score": 0,
                "weak_score": 0,
                "regime": "neutral",
            }
            continue

        current = history.iloc[-1]
        window = history.iloc[max(0, len(history) - lookback_days):]
        short = history.iloc[max(0, len(history) - 20):]
        mid = history.iloc[max(0, len(history) - 60):]

        ret_20d = 0.0
        ret_60d = 0.0
        if len(short) >= 2:
            ret_20d = float(short["index_level"].iloc[-1] / short["index_level"].iloc[0] - 1.0)
        if len(mid) >= 2:
            ret_60d = float(mid["index_level"].iloc[-1] / mid["index_level"].iloc[0] - 1.0)

        rolling_high = float(window["index_level"].max()) if not window.empty else float(current["index_level"])
        drawdown = 0.0
        if rolling_high > 0:
            drawdown = float(current["index_level"] / rolling_high - 1.0)

        features = {
            "date": date,
            "index_pctile": round(
                _percentile(float(current["index_level"]), [float(x) for x in window["index_level"]]),
                6,
            ),
            "ret_20d": round(ret_20d, 6),
            "ret_60d": round(ret_60d, 6),
            "amount_pctile": round(
                _percentile(float(current["amount"]), [float(x) for x in window["amount"]]),
                6,
            ),
            "breadth_20d": round(float(short["breadth"].mean()) if not short.empty else 0.5, 6),
            "drawdown": round(drawdown, 6),
        }
        regime = _classify(features, rule)
        strong_score, weak_score = _score(features)
        features["strong_score"] = strong_score
        features["weak_score"] = weak_score
        features["regime"] = regime
        features_by_date[date] = features

    return features_by_date


def _score(features: dict[str, Any]) -> tuple[int, int]:
    strong = 0
    weak = 0

    checks = [
        (float(features["index_pctile"]) >= 0.65, float(features["index_pctile"]) <= 0.35),
        (
            float(features["ret_20d"]) > 0 and float(features["ret_60d"]) > 0,
            float(features["ret_20d"]) < 0 and float(features["ret_60d"]) < 0,
        ),
        (float(features["amount_pctile"]) >= 0.55, float(features["amount_pctile"]) <= 0.35),
        (float(features["breadth_20d"]) >= 0.55, float(features["breadth_20d"]) <= 0.45),
        (float(features["drawdown"]) > -0.08, float(features["drawdown"]) <= -0.15),
    ]
    for is_strong, is_weak in checks:
        if is_strong:
            strong += 1
        elif is_weak:
            weak += 1
    return strong, weak


def _config_from_best(root: Path, pool_id: int) -> CBArbConfig:
    params = (_best_params(root, pool_id).get("params") or {})
    return _unpack_config(
        list(params.get("weights") or []),
        dict(params.get("thresholds") or {}),
        dict(params.get("rules") or {}),
    )


def _merge_fixed_fields(
    configs: dict[str, CBArbConfig],
    fixed_config: CBArbConfig | None,
    fixed_fields: list[str],
) -> dict[str, CBArbConfig]:
    if fixed_config is None:
        return configs

    merged: dict[str, CBArbConfig] = {}
    for regime, cfg in configs.items():
        new_cfg = replace(cfg)
        for field in fixed_fields:
            if not hasattr(new_cfg, field):
                raise ValueError(f"unknown fixed field: {field}")
            setattr(new_cfg, field, getattr(fixed_config, field))
        merged[regime] = new_cfg
    return merged


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


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


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


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--weak-source", type=int, default=3)
    p.add_argument("--neutral-source", type=int, default=2)
    p.add_argument("--strong-source", type=int, default=6)
    p.add_argument("--lookback-days", type=int, default=252)
    p.add_argument("--rule", choices=["score", "trend_guard", "score_4state"], default="score")
    p.add_argument("--flat-weak-source", type=int, default=None)
    p.add_argument("--strong-max-position-pct", type=float, default=None)
    p.add_argument(
        "--fixed-source",
        type=int,
        default=None,
        help="Pool whose valuation and cheap/expensive fields are shared by all regimes.",
    )
    p.add_argument(
        "--fixed-fields",
        default=",".join(DEFAULT_FIXED_FIELDS),
        help="Comma-separated CBArbConfig fields to keep identical across regimes.",
    )
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "daily_regime_eval"
    output_dir.mkdir(parents=True, exist_ok=True)

    pools = _pool_events(root)
    features_by_date = _build_daily_features(args.lookback_days, args.rule)

    cfg_by_regime = {
        "weak": _config_from_best(root, args.weak_source),
        "neutral": _config_from_best(root, args.neutral_source),
        "strong": _config_from_best(root, args.strong_source),
    }
    if args.flat_weak_source is not None:
        cfg_by_regime["flat_weak"] = _config_from_best(root, args.flat_weak_source)
    else:
        cfg_by_regime["flat_weak"] = cfg_by_regime["weak"]
    if args.strong_max_position_pct is not None:
        cfg_by_regime["strong"] = replace(
            cfg_by_regime["strong"],
            max_position_pct=float(args.strong_max_position_pct),
        )
    fixed_fields = [x.strip() for x in args.fixed_fields.split(",") if x.strip()]
    fixed_cfg = _config_from_best(root, args.fixed_source) if args.fixed_source is not None else None
    cfg_by_regime = _merge_fixed_fields(cfg_by_regime, fixed_cfg, fixed_fields)
    default_cfg = cfg_by_regime["neutral"]
    config_by_date = {
        date: cfg_by_regime[features["regime"]]
        for date, features in features_by_date.items()
    }

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
            default_cfg,
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
            "[daily_regime] "
            f"pool={pool_id} weak={counts['weak_days']} flat_weak={counts['flat_weak_days']} "
            f"neutral={counts['neutral_days']} "
            f"strong={counts['strong_days']} excess={row['excess_return']} "
            f"return={row['total_return']}",
            flush=True,
        )

    summary = {
        "weak_source": args.weak_source,
        "flat_weak_source": args.flat_weak_source,
        "neutral_source": args.neutral_source,
        "strong_source": args.strong_source,
        "strong_max_position_pct": args.strong_max_position_pct,
        "lookback_days": args.lookback_days,
        "rule": args.rule,
        "fixed_source": args.fixed_source,
        "fixed_fields": fixed_fields,
        **_summarise(rows),
    }
    payload = {"summary": summary, "rows": rows}
    (output_dir / "daily_regime_eval.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "daily_regime_eval.csv", rows)
    _write_csv(output_dir / "daily_regime_by_day.csv", daily_rows)
    print("[daily_regime] summary", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[daily_regime] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
