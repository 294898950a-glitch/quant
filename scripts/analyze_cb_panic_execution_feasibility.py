"""Check whether trained panic signals are usable and tradable."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: float | None) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 6)


def _quantile(series: pd.Series, q: float) -> float | None:
    clean = series.dropna()
    if clean.empty:
        return None
    return float(clean.quantile(q))


def _next_day_map(dates: list[str]) -> dict[str, str | None]:
    return {d: dates[i + 1] if i + 1 < len(dates) else None for i, d in enumerate(dates)}


def _load_cb_daily(path: Path) -> pd.DataFrame:
    cb = pd.read_parquet(path).copy()
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb["amount_yuan"] = cb["close"].astype(float) * cb["vol"].astype(float)
    return cb.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def _market_rows(cb: pd.DataFrame, signal_dates: list[str]) -> list[dict[str, Any]]:
    dates = sorted(cb["trade_date"].unique().tolist())
    next_map = _next_day_map(dates)
    rows = []
    by_date = {d: g for d, g in cb.groupby("trade_date")}
    for date in signal_dates:
        today = by_date.get(date)
        next_date = next_map.get(date)
        nxt = by_date.get(next_date) if next_date else None
        if today is None:
            continue
        today_amount = today["amount_yuan"].astype(float)
        row = {
            "signal_date": date,
            "usable_same_day": False,
            "usable_date_assumption": "after_close_signal_next_trade_day",
            "next_trade_date": next_date or "",
            "n_bonds_today": int(len(today)),
            "zero_vol_share_today": _fmt(float((today["vol"].astype(float) <= 0).mean())),
            "p10_amount_today": _fmt(_quantile(today_amount, 0.10)),
            "median_amount_today": _fmt(_quantile(today_amount, 0.50)),
            "bonds_amount_ge_100k_today": int((today_amount >= 100_000).sum()),
            "bonds_amount_ge_500k_today": int((today_amount >= 500_000).sum()),
            "bonds_amount_ge_1m_today": int((today_amount >= 1_000_000).sum()),
            "bonds_amount_ge_5m_today": int((today_amount >= 5_000_000).sum()),
        }
        if nxt is not None and not nxt.empty:
            next_amount = nxt["amount_yuan"].astype(float)
            today_idx = float(today["close"].mean())
            next_open_idx = float(nxt["open"].mean())
            next_close_idx = float(nxt["close"].mean())
            row.update(
                {
                    "n_bonds_next": int(len(nxt)),
                    "zero_vol_share_next": _fmt(float((nxt["vol"].astype(float) <= 0).mean())),
                    "p10_amount_next": _fmt(_quantile(next_amount, 0.10)),
                    "median_amount_next": _fmt(_quantile(next_amount, 0.50)),
                    "bonds_amount_ge_100k_next": int((next_amount >= 100_000).sum()),
                    "bonds_amount_ge_500k_next": int((next_amount >= 500_000).sum()),
                    "bonds_amount_ge_1m_next": int((next_amount >= 1_000_000).sum()),
                    "bonds_amount_ge_5m_next": int((next_amount >= 5_000_000).sum()),
                    "index_next_open_vs_signal_close": _fmt(next_open_idx / today_idx - 1.0 if today_idx > 0 else None),
                    "index_next_close_vs_signal_close": _fmt(next_close_idx / today_idx - 1.0 if today_idx > 0 else None),
                }
            )
        rows.append(row)
    return rows


def _estimate_position_value(row: Any, fallback: float) -> float:
    pnl_pct = float(getattr(row, "pnl_pct", 0.0) or 0.0)
    pnl_amount = float(getattr(row, "pnl_amount", 0.0) or 0.0)
    if abs(pnl_pct) > 1e-6 and abs(pnl_amount) > 0:
        return abs(pnl_amount / pnl_pct)
    return fallback


def _held_trade_rows(
    cb: pd.DataFrame,
    trades_path: Path,
    signal_dates: list[str],
    fallback_position_value: float,
) -> list[dict[str, Any]]:
    if not trades_path.exists():
        return []
    trades = pd.read_csv(trades_path)
    if trades.empty:
        return []
    trades["entry_date"] = trades["entry_date"].astype(str)
    trades["exit_date"] = trades["exit_date"].astype(str)
    trades["cb_code"] = trades["cb_code"].astype(str)
    dates = sorted(cb["trade_date"].unique().tolist())
    next_map = _next_day_map(dates)
    by_key = {(str(r.trade_date), str(r.ts_code)): r for r in cb.itertuples(index=False)}

    rows = []
    for date in signal_dates:
        held = trades[(trades["entry_date"] <= date) & (trades["exit_date"] >= date)]
        if held.empty:
            continue
        next_date = next_map.get(date)
        for trade in held.itertuples(index=False):
            today = by_key.get((date, str(trade.cb_code)))
            nxt = by_key.get((next_date, str(trade.cb_code))) if next_date else None
            position_value = _estimate_position_value(trade, fallback_position_value)
            today_amount = float(today.amount_yuan) if today is not None else None
            next_amount = float(nxt.amount_yuan) if nxt is not None else None
            rows.append(
                {
                    "signal_date": date,
                    "next_trade_date": next_date or "",
                    "cb_code": str(trade.cb_code),
                    "cb_name": getattr(trade, "cb_name", ""),
                    "entry_date": str(trade.entry_date),
                    "exit_date": str(trade.exit_date),
                    "estimated_position_value": _fmt(position_value),
                    "today_amount": _fmt(today_amount),
                    "next_amount": _fmt(next_amount),
                    "position_to_today_amount": _fmt(position_value / today_amount if today_amount and today_amount > 0 else None),
                    "position_to_next_amount": _fmt(position_value / next_amount if next_amount and next_amount > 0 else None),
                    "same_day_amount_ok_10pct": bool(today_amount and position_value <= today_amount * 0.10),
                    "next_day_amount_ok_10pct": bool(next_amount and position_value <= next_amount * 0.10),
                    "next_open_vs_signal_close": _fmt(
                        float(nxt.open) / float(today.close) - 1.0
                        if today is not None and nxt is not None and float(today.close) > 0
                        else None
                    ),
                    "next_close_vs_signal_close": _fmt(
                        float(nxt.close) / float(today.close) - 1.0
                        if today is not None and nxt is not None and float(today.close) > 0
                        else None
                    ),
                }
            )
    return rows


def _summarize_market(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    return {
        "signal_days": int(len(df)),
        "same_day_usable": False,
        "execution_assumption": "signal_after_close_execute_next_trade_day",
        "median_p10_amount_today": _fmt(df["p10_amount_today"].median()),
        "median_p10_amount_next": _fmt(df["p10_amount_next"].median()),
        "median_zero_vol_share_today": _fmt(df["zero_vol_share_today"].median()),
        "median_zero_vol_share_next": _fmt(df["zero_vol_share_next"].median()),
        "median_index_next_open_vs_signal_close": _fmt(df["index_next_open_vs_signal_close"].median()),
        "worst_index_next_open_vs_signal_close": _fmt(df["index_next_open_vs_signal_close"].min()),
        "median_index_next_close_vs_signal_close": _fmt(df["index_next_close_vs_signal_close"].median()),
    }


def _summarize_held(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {"held_signal_rows": 0}
    return {
        "held_signal_rows": int(len(df)),
        "same_day_amount_ok_10pct_share": _fmt(float(df["same_day_amount_ok_10pct"].mean())),
        "next_day_amount_ok_10pct_share": _fmt(float(df["next_day_amount_ok_10pct"].mean())),
        "median_position_to_today_amount": _fmt(df["position_to_today_amount"].median()),
        "p90_position_to_today_amount": _fmt(df["position_to_today_amount"].quantile(0.90)),
        "median_position_to_next_amount": _fmt(df["position_to_next_amount"].median()),
        "p90_position_to_next_amount": _fmt(df["position_to_next_amount"].quantile(0.90)),
        "median_next_open_vs_signal_close": _fmt(df["next_open_vs_signal_close"].median()),
        "worst_next_open_vs_signal_close": _fmt(df["next_open_vs_signal_close"].min()),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cb-daily", type=Path, default=Path("data/cb_warehouse/cb_daily.parquet"))
    parser.add_argument(
        "--signals",
        type=Path,
        default=Path(
            "data/cb_arb_concurrent_supervised_20260511_094500/"
            "panic_detector_training/panic_detector_trained_daily.csv"
        ),
    )
    parser.add_argument(
        "--trades",
        type=Path,
        default=Path(
            "data/cb_arb_concurrent_supervised_20260511_094500/"
            "value_gap_switch_pct_hurdle_2019_2024_train_2025_2026_test/"
            "yearly_trades_best_pct_hurdle.csv"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_concurrent_supervised_20260511_094500/panic_execution_feasibility"),
    )
    parser.add_argument("--start", default="20150101")
    parser.add_argument("--end", default="20251231")
    parser.add_argument("--fallback-position-value", type=float, default=30_000.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    cb = _load_cb_daily(args.cb_daily)
    cb = cb[(cb["trade_date"] >= args.start) & (cb["trade_date"] <= args.end)].copy()
    signals = pd.read_csv(args.signals)
    signals["trade_date"] = signals["trade_date"].astype(str)
    signal_dates = sorted(
        signals.loc[
            (signals["trade_date"] >= args.start)
            & (signals["trade_date"] <= args.end)
            & signals["panic_day_trained"].astype(bool),
            "trade_date",
        ].astype(str).unique().tolist()
    )

    market_rows = _market_rows(cb, signal_dates)
    held_rows = _held_trade_rows(cb, args.trades, signal_dates, args.fallback_position_value)
    _write_csv(args.output_dir / "panic_signal_market_execution.csv", market_rows)
    _write_csv(args.output_dir / "panic_signal_held_position_execution.csv", held_rows)

    summary = {
        "signal_source": str(args.signals),
        "date_range": [args.start, args.end],
        "market": _summarize_market(market_rows),
        "held_positions": _summarize_held(held_rows),
        "important_assumption": "The trained detector uses same-day close data, so same-day close execution is not valid without intraday data.",
        "outputs": [
            str(args.output_dir / "panic_signal_market_execution.csv"),
            str(args.output_dir / "panic_signal_held_position_execution.csv"),
        ],
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
