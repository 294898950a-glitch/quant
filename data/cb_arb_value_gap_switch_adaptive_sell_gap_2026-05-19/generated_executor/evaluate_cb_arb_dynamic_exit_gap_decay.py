#!/usr/bin/env python3
import argparse, json, yaml, os, sys, warnings
import pandas as pd
import numpy as np
from pathlib import Path

def parse_args():
    p = argparse.ArgumentParser(description="Evaluate dynamic exit gap decay proposal")
    p.add_argument("--data-root", required=True)
    p.add_argument("--train-start", required=True)
    p.add_argument("--train-end", required=True)
    p.add_argument("--test-start", required=True)
    p.add_argument("--test-end", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--base-ranks-path", required=True)
    p.add_argument("--lookback-days", type=int, required=True)
    p.add_argument("--multiplier-min", type=float, required=True)
    p.add_argument("--multiplier-max", type=float, required=True)
    p.add_argument("--floor-factor", type=float, required=True)
    p.add_argument("--ceiling-factor", type=float, required=True)
    p.add_argument("--cost-model-enabled", type=str, default="true")
    return p.parse_args()

def load_data(base_ranks_path, data_root):
    ranks = pd.read_parquet(base_ranks_path)
    cb_basic = pd.read_parquet(os.path.join(data_root, "data/cb_warehouse/cb_basic.parquet"))
    stk = pd.read_parquet(os.path.join(data_root, "data/cb_warehouse/stk_daily_qfq.parquet"))
    # Ensure date columns are datetime
    ranks["trade_date"] = pd.to_datetime(ranks["trade_date"])
    cb_basic = cb_basic.rename(columns={"trade_date": "date"}).copy()
    stk["trade_date"] = pd.to_datetime(stk["trade_date"])
    return ranks, cb_basic, stk

def prepare_prices(ranks, cb_basic, stk):
    # ranks expected columns: trade_date, cb_code, value_gap (or gap)
    gap_col = [c for c in ranks.columns if c in ("value_gap", "gap")][0]
    ranks = ranks.rename(columns={gap_col: "entry_value_gap"})
    # Get stock close prices (forward adjusted)
    stk_close = stk[["trade_date", "stock_code", "close_adj"]].rename(columns={"close_adj": "close"})
    # Merge with cb_basic to map cb_code to stock_code
    cb_map = cb_basic[["cb_code", "stock_code"]].drop_duplicates()
    # Ensure ranks has cb_code
    merged = ranks.merge(cb_map, on="cb_code", how="left")
    merged = merged.merge(stk_close, left_on=["trade_date", "stock_code"], right_on=["trade_date", "stock_code"], how="left")
    # Drop any rows where close is missing
    merged = merged.dropna(subset=["close"])
    return merged.sort_values("trade_date")

def compute_volatility(merged, lookback_days):
    # For each cb_code, compute daily returns and rolling std
    merged = merged.copy()
    merged["daily_return"] = merged.groupby("cb_code")["close"].pct_change()
    # rolling std over lookback business days (approx using min_periods=lookback_days)
    merged["rolling_vol"] = merged.groupby("cb_code")["daily_return"].transform(
        lambda x: x.rolling(window=lookback_days, min_periods=lookback_days).std()
    )
    return merged

def simulate_trades(merged, lookback_days, multiplier, floor_factor, ceiling_factor, cost_enabled):
    # Filter to date range covering all needed for vol; but we'll process chronologically.
    # We'll separate train and test periods later.
    # For volatility normalization, we need median volatility per cb_code using train data (2019-2024)
    # We can compute median from merged where date between train_start and train_end.
    # This function will run on a subset of dates for the backtest period.
    # We'll handle overall grid search.

    # We'll pass full merged, but filter for simulation.
    # Parameter 'merged' contains all data, we'll be called for each grid combination but with same merged.
    # For efficiency, we might precompute median vol densities, but here we compute per call.
    # We'll simulate on the whole dataset but record trades for appropriate period.
    # We'll use trade_date ordering.
    trades = []
    open_positions = {}  # key: cb_code, value: entry_price, entry_value_gap, trade_date

    # Precompute median vol for each cb_code using train data (to be set before calling)
    # We need train_start/end for train filtering. We'll pass as params.
    # For now, we'll receive median_vol_map as a dict.
    pass

def run_grid_search(merged, args):
    # Prepare volatility: compute rolling vol on all data
    merged = compute_volatility(merged, args.lookback_days)
    # Determine median volatility per cb_code using train period only
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    train_mask = (merged["trade_date"] >= train_start) & (merged["trade_date"] <= train_end)
    train_data = merged[train_mask].dropna(subset=["rolling_vol"])
    median_vol_map = train_data.groupby("cb_code")["rolling_vol"].median().to_dict()

    # Define multiplier grid
    if abs(args.multiplier_min - args.multiplier_max) < 1e-9:
        multipliers = [args.multiplier_min]
    else:
        # Use fixed steps as per proposal: [0.5,0.75,1.0,1.25,1.5]
        multipliers = [0.5, 0.75, 1.0, 1.25, 1.5]

    best_train_excess = -np.inf
    best_params = None
    best_trades_all = None
    results = []

    for mult in multipliers:
        # Simulate trades on the whole dataset
        # We'll simulate on all data, then evaluate periods separately.
        trades = simulate_trades_full(merged, median_vol_map, mult, args.floor_factor, args.ceiling_factor, args.cost_model_enabled.lower() == "true")
        if not trades:
            continue
        trades_df = pd.DataFrame(trades)
        # Ensure dates
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])
        # Compute performance on train (2019-2024)
        train_trades = trades_df[(trades_df["exit_date"] >= train_start) & (trades_df["exit_date"] <= train_end)]
        train_excess = compute_excess_return(train_trades)
        # 2020 repair: year 2020
        repair_start = pd.Timestamp("2020-01-01")
        repair_end = pd.Timestamp("2020-12-31")
        repair_trades = trades_df[(trades_df["exit_date"] >= repair_start) & (trades_df["exit_date"] <= repair_end)]
        repair_excess = compute_excess_return(repair_trades)
        results.append({"multiplier": mult, "train_excess": train_excess, "repair_excess": repair_excess})
        if train_excess > best_train_excess:
            best_train_excess = train_excess
            best_params = {"multiplier": mult}
            best_trades_all = trades_df

    # If no best, pick first
    if best_params is None:
        best_params = {"multiplier": multipliers[0]}
        best_trades_all = trades_df

    # Evaluate test period on best params
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)
    test_trades = best_trades_all[(best_trades_all["exit_date"] >= test_start) & (best_trades_all["exit_date"] <= test_end)]
    test_excess = compute_excess_return(test_trades)

    # Also compute train and repair for best
    best_train_excess = compute_excess_return(best_trades_all[best_trades_all["exit_date"].between(train_start, train_end)])
    best_repair_excess = compute_excess_return(best_trades_all[
        best_trades_all["exit_date"].between(pd.Timestamp("2020-01-01"), pd.Timestamp("2020-12-31"))
    ])

    # Prepare artifacts
    summary = {
        "best_params": best_params,
        "train_excess": best_train_excess,
        "repair_excess": best_repair_excess,
        "test_excess": test_excess,
        "lookback_days": args.lookback_days,
        "floor_factor": args.floor_factor,
        "ceiling_factor": args.ceiling_factor,
    }
    return summary, best_trades_all

def simulate_trades_full(merged, median_vol_map, multiplier, floor_factor, ceiling_factor, cost_enabled):
    trades = []
    open_positions = {}  # cb_code -> entry_row (dict)
    # Sort merged by trade_date
    merged = merged.sort_values("trade_date")
    # We'll track current date
    for _, row in merged.iterrows():
        date = row["trade_date"]
        cb_code = row["cb_code"]
        close = row["close"]
        # Check exit first
        if cb_code in open_positions:
            entry = open_positions[cb_code]
            entry_price = entry["entry_price"]
            entry_gap = entry["entry_value_gap"]
            # Compute volatility ratio
            med_vol = median_vol_map.get(cb_code, None)
            if med_vol is None or pd.isna(row["rolling_vol"]) or med_vol == 0:
                vol_ratio = 1.0  # fallback
            else:
                vol_ratio = row["rolling_vol"] / med_vol
            adaptive_mult = vol_ratio * multiplier
            adaptive_mult = max(min(adaptive_mult, ceiling_factor), floor_factor)
            target_sell_gap = entry_gap * adaptive_mult
            price_change_pct = (close - entry_price) / entry_price
            # If price increase meets target, exit
            if price_change_pct >= target_sell_gap:
                profit_pct = price_change_pct
                if cost_enabled:
                    # simplified cost: 0.1% entry and exit
                    profit_pct -= 0.002  # 0.2% roundtrip
                trades.append({
                    "cb_code": cb_code,
                    "entry_date": entry["trade_date"],
                    "entry_price": entry_price,
                    "entry_value_gap": entry_gap,
                    "exit_date": date,
                    "exit_price": close,
                    "profit_pct": profit_pct,
                    "multiplier": multiplier,
                })
                del open_positions[cb_code]
        # Entry logic: if no open position for this cb_code?, and value_gap > 0, enter
        # We allow only one open position per CB (no re-entry while still open)
        # Also we could limit total positions, but not necessary.
        if cb_code not in open_positions:
            entry_gap = row["entry_value_gap"]
            if entry_gap > 1e-6:  # positive gap
                open_positions[cb_code] = {
                    "cb_code": cb_code,
                    "trade_date": date,
                    "entry_price": close,
                    "entry_value_gap": entry_gap,
                }
    return trades

def compute_excess_return(trades_df):
    if trades_df.empty:
        return 0.0
    # Sum of profit percentages (assuming equal capital per trade)
    total_profit = trades_df["profit_pct"].sum()
    # Number of trades
    # Annualized: assume each trade uses full capital, so total return is total_profit (if reinvest). 
    # Annualize using period length. Simple: total return over period.
    return round(total_profit, 6)

def main():
    args = parse_args()
    # Load data
    ranks, cb_basic, stk = load_data(args.base_ranks_path, args.data_root)
    merged = prepare_prices(ranks, cb_basic, stk)
    summary, trades_df = run_grid_search(merged, args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Write summary.json
    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    # Write report.yaml with more detailed metrics
    report = {
        "train_excess": summary["train_excess"],
        "repair_excess": summary.get("repair_excess", 0.0),
        "test_excess": summary["test_excess"],
        "best_params": summary["best_params"],
        "lookback_days": args.lookback_days,
        "floor_factor": args.floor_factor,
        "ceiling_factor": args.ceiling_factor,
    }
    with open(output_dir / "report.yaml", "w") as f:
        yaml.dump(report, f)
    # Write l4_ack.yaml (simple acknowledgment)
    ack = {"status": "completed", "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"]}
    with open(output_dir / "l4_ack.yaml", "w") as f:
        yaml.dump(ack, f)
    # diagnostic.yaml with trades stats
    diag = {
        "total_trades": len(trades_df),
        "avg_profit_pct": float(trades_df["profit_pct"].mean()) if not trades_df.empty else 0.0,
        "params_used": summary["best_params"],
    }
    with open(output_dir / "diagnostic.yaml", "w") as f:
        yaml.dump(diag, f)

if __name__ == "__main__":
    main()