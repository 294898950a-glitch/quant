"""Build event-level diagnostics for true vs false cb_arb panic dates."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

EVENTS = [
    ("20200123", "true_panic"),
    ("20240122", "false_positive_2024"),
    ("20240205", "false_positive_2024"),
    ("20240228", "false_positive_2024"),
    ("20240624", "false_positive_2024"),
    ("20241009", "false_positive_2024"),
]


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
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


def _round(value: Any, digits: int = 10) -> Any:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (int, str)):
        return value
    return round(float(value), digits)


def _window_sum(frame: pd.DataFrame, idx: int, col: str, before: int = 0, after: int = 0) -> float:
    start = max(0, idx - before)
    end = min(len(frame), idx + after + 1)
    return float(frame.iloc[start:end][col].astype(float).sum())


def _lookback_sum(frame: pd.DataFrame, idx: int, col: str, days: int) -> float:
    start = max(0, idx - days)
    return float(frame.iloc[start:idx][col].astype(float).sum())


def _forward_sum(frame: pd.DataFrame, idx: int, col: str, days: int) -> float:
    end = min(len(frame), idx + days + 1)
    return float(frame.iloc[idx + 1 : end][col].astype(float).sum())


def _build_pool_frame(cb_daily_path: Path) -> pd.DataFrame:
    cb = pd.read_parquet(cb_daily_path)
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb = cb[(cb["trade_date"] >= "20190101") & (cb["trade_date"] <= "20241231")].copy()
    cb = cb.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    cb["prev_close"] = cb.groupby("ts_code")["close"].shift(1)
    cb["bond_day_ret"] = cb["close"].astype(float) / cb["prev_close"].astype(float) - 1.0
    grouped = cb.groupby("trade_date", sort=True)
    frame = grouped.agg(
        n_bonds=("ts_code", "nunique"),
        valid_returns=("bond_day_ret", "count"),
        pool_mean_return=("bond_day_ret", "mean"),
        pool_median_return=("bond_day_ret", "median"),
        pool_min_return=("bond_day_ret", "min"),
        pool_p10_return=("bond_day_ret", lambda s: float(s.quantile(0.10))),
    ).reset_index()
    for threshold, name in [(-0.03, "drop3"), (-0.05, "drop5"), (-0.07, "drop7")]:
        frame[f"{name}_share"] = grouped["bond_day_ret"].apply(
            lambda s, threshold=threshold: float((s <= threshold).mean())
        ).to_numpy()
    frame["missing_return_ratio"] = 1.0 - frame["valid_returns"] / frame["n_bonds"]
    return frame.sort_values("trade_date").reset_index(drop=True)


def _event_row(
    event_date: str,
    label: str,
    pool: pd.DataFrame,
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    true_metrics: dict[str, float] | None,
) -> dict[str, Any]:
    pool_idx = int(pool.index[pool["trade_date"] == event_date][0])
    eq_idx = int(equity.index[equity["date"] == event_date][0])
    pool_row = pool.iloc[pool_idx]
    eq_row = equity.iloc[eq_idx]

    next5_dates = set(equity.iloc[eq_idx : min(len(equity), eq_idx + 6)]["date"].astype(str))
    next5_after_dates = set(equity.iloc[eq_idx + 1 : min(len(equity), eq_idx + 6)]["date"].astype(str))
    event_open = trades[
        (trades["entry_date"].astype(str) <= event_date)
        & (trades["exit_date"].astype(str) >= event_date)
    ]
    entries_t_to_t5 = trades[trades["entry_date"].astype(str).isin(next5_dates)]
    entries_t1_to_t5 = trades[trades["entry_date"].astype(str).isin(next5_after_dates)]

    row: dict[str, Any] = {
        "event_date": event_date,
        "event_label": label,
        "year": int(event_date[:4]),
        "n_bonds": int(pool_row["n_bonds"]),
        "valid_returns": int(pool_row["valid_returns"]),
        "missing_return_ratio": _round(pool_row["missing_return_ratio"], 6),
        "pool_mean_return": _round(pool_row["pool_mean_return"], 10),
        "pool_median_return": _round(pool_row["pool_median_return"], 10),
        "pool_min_return": _round(pool_row["pool_min_return"], 10),
        "pool_p10_return": _round(pool_row["pool_p10_return"], 10),
        "breadth_drop3_share": _round(pool_row["drop3_share"], 10),
        "breadth_drop5_share": _round(pool_row["drop5_share"], 10),
        "breadth_drop7_share": _round(pool_row["drop7_share"], 10),
        "pool_mean_le_m1pct": int(float(pool_row["pool_mean_return"]) <= -0.01),
        "baseline_equity": _round(eq_row["equity"], 6),
        "baseline_strategy_return_t": _round(eq_row["strategy_return"], 10),
        "baseline_benchmark_return_t": _round(eq_row["benchmark_return"], 10),
        "baseline_daily_excess_t": _round(eq_row["daily_excess"], 10),
        "baseline_excess_pre5": _round(_lookback_sum(equity, eq_idx, "daily_excess", 5), 10),
        "baseline_excess_pre10": _round(_lookback_sum(equity, eq_idx, "daily_excess", 10), 10),
        "baseline_excess_pre20": _round(_lookback_sum(equity, eq_idx, "daily_excess", 20), 10),
        "baseline_excess_post5": _round(_forward_sum(equity, eq_idx, "daily_excess", 5), 10),
        "baseline_excess_post10": _round(_forward_sum(equity, eq_idx, "daily_excess", 10), 10),
        "baseline_excess_post30": _round(_forward_sum(equity, eq_idx, "daily_excess", 30), 10),
        "baseline_strategy_return_post30": _round(_forward_sum(equity, eq_idx, "strategy_return", 30), 10),
        "baseline_benchmark_return_post30": _round(_forward_sum(equity, eq_idx, "benchmark_return", 30), 10),
        "pool_mean_pre5": _round(_lookback_sum(pool, pool_idx, "pool_mean_return", 5), 10),
        "pool_mean_pre10": _round(_lookback_sum(pool, pool_idx, "pool_mean_return", 10), 10),
        "pool_mean_pre20": _round(_lookback_sum(pool, pool_idx, "pool_mean_return", 20), 10),
        "pool_mean_post5": _round(_forward_sum(pool, pool_idx, "pool_mean_return", 5), 10),
        "pool_mean_post10": _round(_forward_sum(pool, pool_idx, "pool_mean_return", 10), 10),
        "pool_mean_post30": _round(_forward_sum(pool, pool_idx, "pool_mean_return", 30), 10),
        "drop3_max_pre5": _round(float(pool.iloc[max(0, pool_idx - 5) : pool_idx]["drop3_share"].max()), 10),
        "drop5_max_pre5": _round(float(pool.iloc[max(0, pool_idx - 5) : pool_idx]["drop5_share"].max()), 10),
        "drop7_max_pre5": _round(float(pool.iloc[max(0, pool_idx - 5) : pool_idx]["drop7_share"].max()), 10),
        "pool_vol_pre20": _round(float(pool.iloc[max(0, pool_idx - 20) : pool_idx]["pool_mean_return"].std()), 10),
        "pool_vol_post30": _round(float(pool.iloc[pool_idx + 1 : min(len(pool), pool_idx + 31)]["pool_mean_return"].std()), 10),
        "baseline_open_positions_on_day": int(len(event_open)),
        "baseline_entry_count_t_to_t5": int(len(entries_t_to_t5)),
        "baseline_entry_count_t1_to_t5": int(len(entries_t1_to_t5)),
        "baseline_entry_unique_bonds_t_to_t5": int(entries_t_to_t5["cb_code"].nunique()),
        "baseline_entry_eventual_pnl_t_to_t5": _round(entries_t_to_t5["pnl_amount"].astype(float).sum(), 2),
        "next5_trading_dates": ",".join(sorted(next5_dates)),
    }
    if true_metrics:
        true_pool_mean = abs(true_metrics["pool_mean_return"])
        row["pool_mean_abs_vs_20200123"] = _round(
            abs(float(pool_row["pool_mean_return"])) / true_pool_mean if true_pool_mean else None,
            6,
        )
        for col in ["drop3_share", "drop5_share", "drop7_share"]:
            denom = true_metrics[col]
            out_col = f"breadth_{col.replace('_share', '')}_vs_20200123"
            row[out_col] = _round(float(pool_row[col]) / denom if denom else None, 6)
    return row


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("data/cb_arb_breadth_confirm_ensemble_2026-05-15"),
    )
    parser.add_argument("--cb-daily", type=Path, default=Path("data/cb_warehouse/cb_daily.parquet"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_panic_calendar_2024_diagnostic_2026-05-15"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    equity = pd.read_csv(args.source_dir / "daily_equity.csv", dtype={"date": str})
    equity = equity[equity["candidate"] == "medium_baseline"].copy()
    equity = equity.sort_values("date").reset_index(drop=True)
    trades = pd.read_csv(
        args.source_dir / "trades.csv",
        dtype={"entry_date": str, "exit_date": str, "cb_code": str},
    )
    trades = trades[trades["candidate"] == "medium_baseline"].copy()
    pool = _build_pool_frame(args.cb_daily)

    missing = [
        date
        for date, _ in EVENTS
        if date not in set(pool["trade_date"].astype(str)) or date not in set(equity["date"].astype(str))
    ]
    if missing:
        raise ValueError(f"event dates missing from pool/equity calendars: {missing}")

    true_pool_row = pool[pool["trade_date"] == "20200123"].iloc[0]
    true_metrics = {
        "pool_mean_return": float(true_pool_row["pool_mean_return"]),
        "drop3_share": float(true_pool_row["drop3_share"]),
        "drop5_share": float(true_pool_row["drop5_share"]),
        "drop7_share": float(true_pool_row["drop7_share"]),
    }
    rows = [_event_row(date, label, pool, equity, trades, true_metrics) for date, label in EVENTS]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_rows(args.output_dir / "panic_calendar.csv", rows)
    (args.output_dir / "summary.json").write_text(
        json.dumps(
            {
                "source_dir": str(args.source_dir),
                "cb_daily": str(args.cb_daily),
                "events": EVENTS,
                "method": "Event-day breadth/pool metrics from cb_daily close-to-close returns; baseline windows from medium_baseline daily_equity/trades artifacts.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output_dir / 'panic_calendar.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
