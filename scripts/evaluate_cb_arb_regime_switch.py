"""Evaluate a simple strong/weak market parameter switch on cb_arb pools."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from strategies.cb_arb.verifier import _load_cb_daily, run_backtest


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _best_params(root: Path, source_pool: int) -> dict[str, Any]:
    return _read_json(root / f"pool_{source_pool}" / "best_params.json")


def _pool_events(root: Path) -> dict[int, list[str]]:
    data = _read_json(root / "sealed_pools.json")
    return {int(p["id"]): [str(x) for x in p["event_ids"]] for p in data["pools"]}


def _percentile(value: float, sample: list[float]) -> float:
    if not sample:
        return 0.5
    below_or_equal = sum(1 for x in sample if x <= value)
    return below_or_equal / len(sample)


def _market_features(start_date: str, lookback_days: int) -> dict[str, Any]:
    daily = _load_cb_daily()
    days = sorted(set(daily["trade_date"].astype(str).tolist()))
    try:
        idx = days.index(start_date)
    except ValueError:
        idx = next(i for i, d in enumerate(days) if d >= start_date)
    prev_days = days[max(0, idx - lookback_days):idx]
    if len(prev_days) < 2:
        return {
            "lookback_start": None,
            "lookback_end": None,
            "market_return": 0.0,
            "breadth": 0.5,
            "n_bonds": 0,
        }
    start = prev_days[0]
    end = prev_days[-1]
    sub = daily[daily["trade_date"].isin({start, end})]
    pivot = sub.pivot_table(index="ts_code", columns="trade_date", values="close", aggfunc="last")
    if start not in pivot.columns or end not in pivot.columns:
        return {
            "lookback_start": start,
            "lookback_end": end,
            "market_return": 0.0,
            "breadth": 0.5,
            "n_bonds": 0,
        }
    paired = pivot[[start, end]].dropna()
    paired = paired[(paired[start] > 0) & (paired[end] > 0)]
    returns = paired[end] / paired[start] - 1.0
    if returns.empty:
        market_return = 0.0
        breadth = 0.5
    else:
        market_return = float(paired[end].mean() / paired[start].mean() - 1.0)
        breadth = float((returns > 0).mean())
    return {
        "lookback_start": start,
        "lookback_end": end,
        "market_return": round(market_return, 6),
        "breadth": round(breadth, 6),
        "n_bonds": int(len(returns)),
    }


def _percentile_features(start_date: str, lookback_days: int) -> dict[str, Any]:
    daily = _load_cb_daily()
    by_day = daily.groupby("trade_date").agg(
        index_level=("close", "mean"),
        amount=("amount_yuan", "sum"),
    ).sort_index()
    days = list(by_day.index.astype(str))
    try:
        idx = days.index(start_date)
    except ValueError:
        idx = next(i for i, d in enumerate(days) if d >= start_date)

    history = by_day.iloc[:idx]
    if history.empty:
        return {
            "lookback_start": None,
            "lookback_end": None,
            "index_pctile": 0.5,
            "amount_pctile": 0.5,
            "index_level": None,
            "amount": None,
            "history_days": 0,
        }
    current = history.iloc[-1]
    window = history.iloc[max(0, len(history) - lookback_days):]
    short_window = history.iloc[max(0, len(history) - 20):]
    mid_window = history.iloc[max(0, len(history) - 60):]
    ret_20d = 0.0
    ret_60d = 0.0
    if len(short_window) >= 2:
        ret_20d = float(short_window["index_level"].iloc[-1] / short_window["index_level"].iloc[0] - 1.0)
    if len(mid_window) >= 2:
        ret_60d = float(mid_window["index_level"].iloc[-1] / mid_window["index_level"].iloc[0] - 1.0)
    return {
        "lookback_start": str(window.index[0]),
        "lookback_end": str(window.index[-1]),
        "index_pctile": round(_percentile(float(current["index_level"]), [float(x) for x in window["index_level"]]), 6),
        "amount_pctile": round(_percentile(float(current["amount"]), [float(x) for x in window["amount"]]), 6),
        "ret_20d": round(ret_20d, 6),
        "ret_60d": round(ret_60d, 6),
        "index_level": round(float(current["index_level"]), 6),
        "amount": round(float(current["amount"]), 2),
        "history_days": int(len(window)),
    }


def _classify(features: dict[str, Any]) -> str:
    ret = float(features.get("market_return") or 0.0)
    breadth = float(features.get("breadth") or 0.5)
    if ret > 0.05 and breadth > 0.55:
        return "strong"
    if ret < -0.05 and breadth < 0.45:
        return "weak"
    return "neutral"


def _classify_percentile(features: dict[str, Any]) -> str:
    index_pctile = float(features.get("index_pctile") or 0.5)
    amount_pctile = float(features.get("amount_pctile") or 0.5)
    if index_pctile >= 0.70 and amount_pctile >= 0.55:
        return "strong"
    if index_pctile <= 0.30 and amount_pctile <= 0.45:
        return "weak"
    return "neutral"


def _classify_percentile_momentum(features: dict[str, Any]) -> str:
    index_pctile = float(features.get("index_pctile") or 0.5)
    amount_pctile = float(features.get("amount_pctile") or 0.5)
    ret_20d = float(features.get("ret_20d") or 0.0)
    ret_60d = float(features.get("ret_60d") or 0.0)
    if (ret_20d > 0.02 or ret_60d > 0.05) and amount_pctile >= 0.45:
        return "strong"
    if index_pctile >= 0.70 and amount_pctile >= 0.55 and ret_60d > 0.0:
        return "strong"
    if (ret_20d < -0.02 or ret_60d < -0.05) and amount_pctile <= 0.65:
        return "weak"
    if index_pctile <= 0.30 and amount_pctile <= 0.45 and ret_60d <= 0.0:
        return "weak"
    return "neutral"


def _metrics_payload(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "excess_return": metrics.get("excess_return"),
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "total_trades": metrics.get("total_trades"),
        "win_rate": metrics.get("win_rate"),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--weak-source", type=int, default=3)
    p.add_argument("--neutral-source", type=int, default=2)
    p.add_argument("--strong-source", type=int, default=6)
    p.add_argument("--lookback-days", type=int, default=60)
    p.add_argument(
        "--classifier",
        choices=["return_breadth", "percentile", "percentile_momentum"],
        default="return_breadth",
    )
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.data_root
    output_dir = args.output_dir or root / "regime_eval"
    output_dir.mkdir(parents=True, exist_ok=True)
    pools = _pool_events(root)
    source_for = {
        "weak": args.weak_source,
        "neutral": args.neutral_source,
        "strong": args.strong_source,
    }
    params_by_source = {
        source: _best_params(root, source).get("params") or {}
        for source in set(source_for.values())
    }

    rows: list[dict[str, Any]] = []
    for pool_id, event_ids in sorted(pools.items()):
        start = min(event_ids)
        end = max(event_ids)
        if args.classifier in {"percentile", "percentile_momentum"}:
            features = _percentile_features(start, args.lookback_days)
            regime = (
                _classify_percentile_momentum(features)
                if args.classifier == "percentile_momentum"
                else _classify_percentile(features)
            )
        else:
            features = _market_features(start, args.lookback_days)
            regime = _classify(features)
        source = source_for[regime]
        params = params_by_source[source]
        result = run_backtest(
            list(params.get("weights") or []),
            dict(params.get("thresholds") or {}),
            dict(params.get("rules") or {}),
            oos_event_ids=set(event_ids),
        )
        row = {
            "pool": pool_id,
            "start": start,
            "end": end,
            "regime": regime,
            "source_pool": source,
            **features,
            **_metrics_payload(result.oos_metrics or {}),
        }
        rows.append(row)
        print(
            "[regime_eval] "
            f"pool={pool_id} regime={regime} source=pool_{source} "
            f"excess={row['excess_return']} return={row['total_return']}",
            flush=True,
        )

    excess_vals = [float(r["excess_return"]) for r in rows if r.get("excess_return") is not None]
    return_vals = [float(r["total_return"]) for r in rows if r.get("total_return") is not None]
    summary = {
        "weak_source": args.weak_source,
        "neutral_source": args.neutral_source,
        "strong_source": args.strong_source,
        "lookback_days": args.lookback_days,
        "classifier": args.classifier,
        "avg_excess_return": round(sum(excess_vals) / len(excess_vals), 6) if excess_vals else None,
        "min_excess_return": round(min(excess_vals), 6) if excess_vals else None,
        "positive_excess_count": sum(1 for x in excess_vals if x > 0),
        "avg_total_return": round(sum(return_vals) / len(return_vals), 6) if return_vals else None,
    }
    (output_dir / "regime_eval.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "regime_eval.csv", rows)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[regime_eval] summary", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[regime_eval] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
