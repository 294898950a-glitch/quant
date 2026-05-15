"""Attribute cb_arb baseline 2020/2024 trades versus the CB benchmark."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


YEARS = (2020, 2024)


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
    if isinstance(value, (str, int)):
        return value
    return round(float(value), digits)


def _compound_return(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float((1.0 + series.astype(float)).prod() - 1.0)


def _load_source(source_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_csv(
        source_dir / "trades.csv",
        dtype={"entry_date": str, "exit_date": str, "cb_code": str},
    )
    equity = pd.read_csv(source_dir / "daily_equity.csv", dtype={"date": str})
    trades = trades[(trades["candidate"] == "medium_baseline") & (trades["year"].isin(YEARS))].copy()
    equity = equity[(equity["candidate"] == "medium_baseline") & (equity["year"].isin(YEARS))].copy()
    trades["entry_date"] = trades["entry_date"].str.zfill(8)
    trades["exit_date"] = trades["exit_date"].str.zfill(8)
    equity["date"] = equity["date"].str.zfill(8)
    return trades, equity


def _load_warehouse(
    cb_daily_path: Path,
    cb_basic_path: Path,
    stock_daily_path: Path,
    trades: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    needed_cb = set(trades["cb_code"].astype(str))
    needed_dates = set(trades["entry_date"].astype(str)) | set(trades["exit_date"].astype(str))

    cb_daily = pd.read_parquet(cb_daily_path, columns=["ts_code", "trade_date", "close"])
    cb_daily["trade_date"] = cb_daily["trade_date"].astype(str)
    cb_daily = cb_daily[
        cb_daily["ts_code"].astype(str).isin(needed_cb) & cb_daily["trade_date"].isin(needed_dates)
    ].copy()

    basic = pd.read_parquet(
        cb_basic_path,
        columns=["ts_code", "bond_short_name", "stk_code", "conv_price", "rating", "remain_size"],
    )
    basic["ts_code"] = basic["ts_code"].astype(str)
    basic = basic[basic["ts_code"].isin(needed_cb)].copy()
    stock_codes = set(basic["stk_code"].dropna().astype(str))

    stock_daily = pd.read_parquet(stock_daily_path, columns=["stk_code", "trade_date", "close"])
    stock_daily["trade_date"] = stock_daily["trade_date"].astype(str)
    stock_daily = stock_daily[
        stock_daily["stk_code"].astype(str).isin(stock_codes)
        & stock_daily["trade_date"].isin(needed_dates)
    ].copy()

    cb_points = (
        trades[["cb_code", "entry_date", "exit_date"]]
        .merge(basic, left_on="cb_code", right_on="ts_code", how="left")
        .drop(columns=["ts_code"])
    )
    entry = cb_points[["cb_code", "entry_date"]].rename(columns={"entry_date": "trade_date"})
    exit_ = cb_points[["cb_code", "exit_date"]].rename(columns={"exit_date": "trade_date"})
    cb_prices = pd.concat([entry, exit_], ignore_index=True).drop_duplicates()
    cb_prices = cb_prices.merge(
        cb_daily.rename(columns={"ts_code": "cb_code", "close": "cb_close"}),
        on=["cb_code", "trade_date"],
        how="left",
    )

    stock_points = cb_points[["cb_code", "stk_code", "conv_price", "entry_date", "exit_date"]].copy()
    stock_entry = stock_points[["cb_code", "stk_code", "conv_price", "entry_date"]].rename(
        columns={"entry_date": "trade_date"}
    )
    stock_exit = stock_points[["cb_code", "stk_code", "conv_price", "exit_date"]].rename(
        columns={"exit_date": "trade_date"}
    )
    stock_prices = pd.concat([stock_entry, stock_exit], ignore_index=True).drop_duplicates()
    stock_prices = stock_prices.merge(stock_daily, on=["stk_code", "trade_date"], how="left")
    stock_prices = stock_prices.rename(columns={"close": "stock_close"})
    stock_prices["conversion_value"] = stock_prices["stock_close"].astype(float) / stock_prices[
        "conv_price"
    ].astype(float) * 100.0
    stock_prices.loc[stock_prices["conversion_value"] <= 0, "conversion_value"] = pd.NA

    price_points = cb_prices.merge(
        stock_prices[["cb_code", "trade_date", "stock_close", "conversion_value"]],
        on=["cb_code", "trade_date"],
        how="left",
    )
    price_points["premium"] = price_points["cb_close"].astype(float) / price_points[
        "conversion_value"
    ].astype(float) - 1.0
    enriched_basic = basic.rename(columns={"ts_code": "cb_code"})
    return price_points, enriched_basic


def _benchmark_return(equity_by_year: dict[int, pd.DataFrame], year: int, entry: str, exit_: str) -> float:
    frame = equity_by_year[year]
    mask = (frame["date"] > entry) & (frame["date"] <= exit_)
    return _compound_return(frame.loc[mask, "benchmark_return"])


def _daily_excess_sum(equity: pd.DataFrame, year: int, month: str) -> float:
    frame = equity[(equity["year"] == year) & (equity["date"].str[:6] == month)]
    return float(frame["daily_excess"].astype(float).sum())


def _top_exit_reason(series: pd.Series) -> str:
    if series.empty:
        return ""
    reason, count = Counter(series.astype(str)).most_common(1)[0]
    return f"{reason}:{count}"


def _build_trade_rows(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    price_points: pd.DataFrame,
    basic: pd.DataFrame,
) -> pd.DataFrame:
    price_lookup = price_points.set_index(["cb_code", "trade_date"])
    equity_by_year = {year: frame.sort_values("date").reset_index(drop=True) for year, frame in equity.groupby("year")}
    basic_lookup = basic.set_index("cb_code")
    rows: list[dict[str, Any]] = []

    for trade in trades.sort_values(["year", "entry_date", "exit_date", "cb_code"]).itertuples(index=False):
        year = int(trade.year)
        entry_key = (trade.cb_code, trade.entry_date)
        exit_key = (trade.cb_code, trade.exit_date)
        entry_price = price_lookup.loc[entry_key] if entry_key in price_lookup.index else None
        exit_price = price_lookup.loc[exit_key] if exit_key in price_lookup.index else None
        basic_row = basic_lookup.loc[trade.cb_code] if trade.cb_code in basic_lookup.index else None
        bench = _benchmark_return(equity_by_year, year, trade.entry_date, trade.exit_date)
        trade_pnl = float(trade.pnl_pct)
        entry_premium = entry_price["premium"] if entry_price is not None else pd.NA
        exit_premium = exit_price["premium"] if exit_price is not None else pd.NA
        entry_stock = entry_price["stock_close"] if entry_price is not None else pd.NA
        exit_stock = exit_price["stock_close"] if exit_price is not None else pd.NA
        underlying_ret = (
            float(exit_stock) / float(entry_stock) - 1.0
            if pd.notna(entry_stock) and pd.notna(exit_stock) and float(entry_stock) != 0
            else pd.NA
        )
        rows.append(
            {
                "year": year,
                "cb_code": trade.cb_code,
                "cb_name": trade.cb_name,
                "stk_code": "" if basic_row is None or pd.isna(basic_row["stk_code"]) else basic_row["stk_code"],
                "rating": "" if basic_row is None or pd.isna(basic_row["rating"]) else basic_row["rating"],
                "remain_size": _round(None if basic_row is None else basic_row["remain_size"], 6),
                "entry_date": trade.entry_date,
                "exit_date": trade.exit_date,
                "entry_month": trade.entry_date[:6],
                "exit_month": trade.exit_date[:6],
                "holding_days": int(trade.holding_days),
                "entry_price": _round(trade.entry_price, 6),
                "exit_price": _round(trade.exit_price, 6),
                "entry_premium": _round(entry_premium, 10),
                "exit_premium": _round(exit_premium, 10),
                "premium_change_pp": _round((float(exit_premium) - float(entry_premium)) * 100.0 if pd.notna(entry_premium) and pd.notna(exit_premium) else None, 6),
                "entry_underlying_close": _round(entry_stock, 6),
                "exit_underlying_close": _round(exit_stock, 6),
                "underlying_return_pct": _round(float(underlying_ret) * 100.0 if pd.notna(underlying_ret) else None, 6),
                "trade_pnl_pct": _round(trade_pnl, 10),
                "benchmark_pnl_pct": _round(bench, 10),
                "excess_pp": _round((trade_pnl - bench) * 100.0, 6),
                "pnl_amount": _round(trade.pnl_amount, 2),
                "exit_reason": trade.exit_reason,
                "entry_gap_pct": _round(trade.entry_gap_pct, 10),
                "entry_gap_amount": _round(trade.entry_gap_amount, 2),
            }
        )
    return pd.DataFrame(rows)


def _monthly_rows(attr: pd.DataFrame, equity: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    attr = attr.copy()
    attr["excess_pp_num"] = pd.to_numeric(attr["excess_pp"], errors="coerce")
    attr["trade_pnl_pct_num"] = pd.to_numeric(attr["trade_pnl_pct"], errors="coerce")
    attr["benchmark_pnl_pct_num"] = pd.to_numeric(attr["benchmark_pnl_pct"], errors="coerce")
    attr["holding_days_num"] = pd.to_numeric(attr["holding_days"], errors="coerce")
    equity = equity.copy()
    equity["month"] = equity["date"].str[:6]
    months = equity[["year", "month"]].drop_duplicates().sort_values(["year", "month"])
    for month_row in months.itertuples(index=False):
        year = int(month_row.year)
        month = str(month_row.month)
        group = attr[(attr["year"] == year) & (attr["entry_month"] == month)]
        exit_group = attr[(attr["year"] == year) & (attr["exit_month"] == month)]
        eq_group = equity[(equity["year"] == year) & (equity["month"] == month)]
        if group.empty:
            worst_trade_cb = ""
            worst_trade_excess_pp = ""
            best_trade_excess_pp = ""
        else:
            worst_trade_cb = group.sort_values("excess_pp_num").iloc[0]["cb_code"]
            worst_trade_excess_pp = _round(group["excess_pp_num"].min(), 6)
            best_trade_excess_pp = _round(group["excess_pp_num"].max(), 6)
        rows.append(
            {
                "year": year,
                "month": month,
                "trade_count": int(len(group)),
                "unique_bonds": int(group["cb_code"].nunique()),
                "trade_excess_sum_pp": _round(group["excess_pp_num"].sum(), 6),
                "trade_excess_mean_pp": _round(group["excess_pp_num"].mean(), 6),
                "trade_excess_median_pp": _round(group["excess_pp_num"].median(), 6),
                "exit_trade_count": int(len(exit_group)),
                "exit_trade_excess_sum_pp": _round(exit_group["excess_pp_num"].sum(), 6),
                "trade_pnl_sum_pct": _round(group["trade_pnl_pct_num"].sum(), 10),
                "benchmark_pnl_sum_pct": _round(group["benchmark_pnl_pct_num"].sum(), 10),
                "daily_strategy_return_sum": _round(eq_group["strategy_return"].astype(float).sum(), 10),
                "daily_benchmark_return_sum": _round(eq_group["benchmark_return"].astype(float).sum(), 10),
                "daily_equity_excess_sum": _round(_daily_excess_sum(equity, year, month), 10),
                "avg_holding_days": _round(group["holding_days_num"].mean(), 4),
                "entry_count": int(len(group)),
                "exit_reason_top": _top_exit_reason(group["exit_reason"]),
                "stop_loss_count": int(group["exit_reason"].astype(str).str.contains("stop_loss").sum()),
                "rank_sell_count": int(group["exit_reason"].astype(str).str.contains("rank_sell").sum()),
                "switch_value_gap_count": int(group["exit_reason"].astype(str).str.contains("switch_value_gap").sum()),
                "max_holding_days_count": int(group["exit_reason"].astype(str).str.contains("max_holding_days").sum()),
                "worst_trade_cb": worst_trade_cb,
                "worst_trade_excess_pp": worst_trade_excess_pp,
                "best_trade_excess_pp": best_trade_excess_pp,
            }
        )
    return rows


def _top_rows(attr: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    attr = attr.copy()
    attr["excess_pp_num"] = pd.to_numeric(attr["excess_pp"], errors="coerce")
    for year, group in attr.groupby("year", sort=True):
        for side, part in [
            ("worst", group.sort_values("excess_pp_num", ascending=True).head(10)),
            ("best", group.sort_values("excess_pp_num", ascending=False).head(10)),
        ]:
            for rank, row in enumerate(part.to_dict("records"), start=1):
                out = {"side": side, "rank": rank}
                out.update({k: v for k, v in row.items() if k != "excess_pp_num"})
                rows.append(out)
    return rows


def _summary(attr: pd.DataFrame, monthly: list[dict[str, Any]], source_dir: Path) -> dict[str, Any]:
    attr = attr.copy()
    attr["excess_pp_num"] = pd.to_numeric(attr["excess_pp"], errors="coerce")
    monthly_df = pd.DataFrame(monthly)
    result: dict[str, Any] = {
        "source_dir": str(source_dir),
        "method": "Per-trade benchmark return compounds medium_baseline daily benchmark_return for trading days entry_date < d <= exit_date. Premium is approximate conversion premium from warehouse cb close, raw stock close, and cb_basic conv_price.",
        "years": {},
    }
    for year, group in attr.groupby("year", sort=True):
        m = monthly_df[monthly_df["year"] == year].copy()
        worst_month = m.sort_values("trade_excess_sum_pp").iloc[0].to_dict()
        best_month = m.sort_values("trade_excess_sum_pp", ascending=False).iloc[0].to_dict()
        worst_path_month = m.sort_values("daily_equity_excess_sum").iloc[0].to_dict()
        worst10 = group.sort_values("excess_pp_num").head(10)
        total_abs = float(group["excess_pp_num"].abs().sum())
        worst10_abs_share = float(worst10["excess_pp_num"].abs().sum() / total_abs) if total_abs else None
        result["years"][str(year)] = {
            "trade_count": int(len(group)),
            "median_excess_pp": _round(group["excess_pp_num"].median(), 6),
            "mean_excess_pp": _round(group["excess_pp_num"].mean(), 6),
            "sum_excess_pp": _round(group["excess_pp_num"].sum(), 6),
            "worst_month": worst_month,
            "worst_path_month": worst_path_month,
            "best_month": best_month,
            "worst10_excess_sum_pp": _round(worst10["excess_pp_num"].sum(), 6),
            "worst10_abs_share_of_total_abs_trade_excess": _round(worst10_abs_share, 6),
            "exit_reason_counts": group["exit_reason"].value_counts().to_dict(),
            "negative_excess_trade_share": _round(float((group["excess_pp_num"] < 0).mean()), 6),
            "avg_entry_premium_worst10": _round(pd.to_numeric(worst10["entry_premium"], errors="coerce").mean(), 10),
            "avg_holding_days_worst10": _round(pd.to_numeric(worst10["holding_days"], errors="coerce").mean(), 4),
        }

    # Conservative qualitative hint from concentration.
    worst_month_shares: list[float] = []
    for year, info in result["years"].items():
        total = info["sum_excess_pp"]
        worst = float(info["worst_month"]["trade_excess_sum_pp"])
        if total < 0:
            worst_month_shares.append(abs(worst) / abs(total))
    if worst_month_shares and max(worst_month_shares) >= 0.45:
        hint = "time_window_concentrated"
    elif all(info["negative_excess_trade_share"] >= 0.5 for info in result["years"].values()):
        hint = "mixed_or_broad_trade_drag"
    else:
        hint = "mixed"
    result["classification_hint"] = hint
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("data/cb_arb_breadth_confirm_ensemble_2026-05-15"),
    )
    parser.add_argument("--cb-daily", type=Path, default=Path("data/cb_warehouse/cb_daily.parquet"))
    parser.add_argument("--cb-basic", type=Path, default=Path("data/cb_warehouse/cb_basic.parquet"))
    parser.add_argument("--stock-daily", type=Path, default=Path("data/cb_warehouse/stk_daily.parquet"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_baseline_trade_diagnostic_2020_2024_2026-05-15"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    trades, equity = _load_source(args.source_dir)
    price_points, basic = _load_warehouse(args.cb_daily, args.cb_basic, args.stock_daily, trades)
    attr = _build_trade_rows(trades, equity, price_points, basic)
    monthly = _monthly_rows(attr, equity)
    top = _top_rows(attr)
    summary = _summary(attr, monthly, args.source_dir)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for year in YEARS:
        rows = attr[attr["year"] == year].to_dict("records")
        _write_rows(args.output_dir / f"trade_attribution_{year}.csv", rows)
    _write_rows(args.output_dir / "monthly_aggregation.csv", monthly)
    _write_rows(args.output_dir / "top_worst_top_best.csv", top)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
