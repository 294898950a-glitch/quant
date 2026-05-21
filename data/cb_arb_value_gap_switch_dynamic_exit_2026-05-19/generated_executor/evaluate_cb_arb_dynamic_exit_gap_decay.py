#!/usr/bin/env python3
"""Dynamic exit evaluator: drawdown‑adjusted time‑based exit for cb_arb_value_gap_switch."""
import argparse
import json
import os
from pathlib import Path
import pandas as pd
import numpy as np
import yaml

def declare_data_requirements(command, spec):
    return {
        "required_files": [
            {"path": "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet"},
            {"path": "data/cb_warehouse/cb_basic.parquet"},
            {"path": "data/cb_warehouse/stk_daily_qfq.parquet"}
        ]
    }

def load_data(data_root):
    base = Path(data_root)
    gap_df = pd.read_parquet(base / "cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet")
    cb_basic_df = pd.read_parquet(base / "cb_warehouse/cb_basic.parquet")
    stock_df = pd.read_parquet(base / "cb_warehouse/stk_daily_qfq.parquet")
    return gap_df, cb_basic_df, stock_df

def prepare_entry_signals(gap_df):
    # Assumes gap_df has columns: trade_date, stock_code, amount (value gap)
    # We rank stocks by amount descending and pick top 10 where amount > 0.
    gap_df = gap_df.copy()
    gap_df['trade_date'] = pd.to_datetime(gap_df['trade_date'])
    # Some rows may have amount = 0 or NaN -> filter
    gap_df = gap_df[gap_df['amount'] > 0].copy()
    # daily rank
    gap_df['rank'] = gap_df.groupby('trade_date')['amount'].rank(method='first', ascending=False)
    top10 = gap_df[gap_df['rank'] <= 10].copy()
    return top10[['trade_date', 'stock_code']]

def simulate_portfolio(entry_signals, stock_returns_df, base_hold_days=30, dynamic=True):
    # entry_signals: DataFrame with columns trade_date, stock_code
    # stock_returns_df: index=date, columns=stock_code, values=simple return (or log)
    # dynamic: if True, adjust max holding period based on portfolio drawdown
    # Returns equity curve pd.Series
    dates = pd.to_datetime(sorted(stock_returns_df.index))
    # Map stock codes to columns
    col_map = {c: c for c in stock_returns_df.columns}
    # Trading calendar
    # Initialize positions list: each item dict(entry_date, stock, entry_day_idx, planned_exit_day_idx)
    positions = []
    # Portfolio value
    portfolio_value = 1.0
    peak = 1.0
    equity_curve = []
    calendar = dates.tolist()
    # Create a date->index mapping
    date2idx = {d: i for i, d in enumerate(calendar)}
    
    # Pre-process entry signals: group by date
    entry_by_date = entry_signals.groupby('trade_date')['stock_code'].apply(list).to_dict()
    
    for i, today in enumerate(calendar):
        # Process entries
        today_str = pd.Timestamp(today).strftime('%Y-%m-%d')  # ensure matching format
        if today in entry_by_date:
            stocks = entry_by_date[today]
            for s in stocks:
                if s in col_map:
                    # Determine max hold days dynamically if dynamic
                    if dynamic:
                        # compute current drawdown from peak
                        dd = (peak - portfolio_value) / peak if peak > 0 else 0.0
                        if dd >= 0.10:
                            max_hold = 10
                        elif dd >= 0.05:
                            max_hold = 20
                        else:
                            max_hold = base_hold_days
                    else:
                        max_hold = base_hold_days
                    exit_day_idx = min(i + max_hold, len(calendar) - 1)
                    positions.append({
                        'entry_idx': i,
                        'stock': s,
                        'exit_idx': exit_day_idx,
                    })
        
        # Current date index
        # Compute total return from all active positions (equal weight)
        daily_return = 0.0
        active_pos = [p for p in positions if p['entry_idx'] <= i <= p['exit_idx']]
        if active_pos:
            stock_returns_today = []
            for p in active_pos:
                # get return for stock p['stock'] on date today
                # stock_returns_df index should be date, column stock code
                if p['stock'] in stock_returns_df.columns:
                    try:
                        ret = stock_returns_df.loc[today, p['stock']]
                        if isinstance(ret, pd.Series):
                            ret = ret.iloc[0]
                        if pd.notna(ret):
                            stock_returns_today.append(ret)
                    except:
                        pass
            if stock_returns_today:
                daily_return = np.mean(stock_returns_today)
        
        # Apply return
        portfolio_value *= (1 + daily_return)
        peak = max(peak, portfolio_value)
        equity_curve.append(portfolio_value)
    
    return pd.Series(equity_curve, index=calendar)

def compute_max_drawdown(equity):
    peak = equity.expanding(min_periods=1).max()
    dd = (peak - equity) / peak
    return dd.max()

def compute_excess_return(equity, period_start, period_end):
    sub = equity.loc[period_start:period_end]
    if len(sub) < 2:
        return 0.0
    return sub.iloc[-1] / sub.iloc[0] - 1

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--train-start', required=True)
    parser.add_argument('--train-end', required=True)
    parser.add_argument('--test-start', required=True)
    parser.add_argument('--test-end', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    gap_df, cb_basic_df, stock_df = load_data(args.data_root)

    # Build stock return matrix
    stock_df['trade_date'] = pd.to_datetime(stock_df['trade_date'])
    stock_df = stock_df.pivot_table(index='trade_date', columns='stock_code', values='close')
    # Compute daily returns
    stock_returns = stock_df.pct_change()
    stock_returns = stock_returns.loc[args.train_start:args.test_end]

    # Prepare entry signals from value gaps
    entry_signals = prepare_entry_signals(gap_df)

    # Simulate baseline (fixed exit)
    baseline_equity = simulate_portfolio(entry_signals, stock_returns, dynamic=False)
    # Simulate dynamic exit
    dynamic_equity = simulate_portfolio(entry_signals, stock_returns, dynamic=True)

    train_slice = slice(args.train_start, args.train_end)
    test_slice = slice(args.test_start, args.test_end)
    repair_slice = slice('2020-01-01', '2020-12-31')

    # Compute metrics
    baseline_train_max_dd = compute_max_drawdown(baseline_equity[train_slice])
    dynamic_train_max_dd = compute_max_drawdown(dynamic_equity[train_slice])

    baseline_test_excess_return = compute_excess_return(baseline_equity, args.test_start, args.test_end)
    dynamic_test_excess_return = compute_excess_return(dynamic_equity, args.test_start, args.test_end)

    baseline_repair_excess = compute_excess_return(baseline_equity, '2020-01-01', '2020-12-31')
    dynamic_repair_excess = compute_excess_return(dynamic_equity, '2020-01-01', '2020-12-31')

    # Adoption criteria
    train_dd_improve = (abs(baseline_train_max_dd) - abs(dynamic_train_max_dd)) / abs(baseline_train_max_dd) >= 0.10 if baseline_train_max_dd != 0 else False
    test_excess_ok = dynamic_test_excess_return >= 0.9 * baseline_test_excess_return
    repair_ok = dynamic_repair_excess >= baseline_repair_excess  # not worse
    adoption_pass = train_dd_improve and test_excess_ok and repair_ok

    summary = {
        'adoption_pass': bool(adoption_pass),
        'baseline': {
            'train_max_drawdown': float(baseline_train_max_dd),
            'test_excess_return': float(baseline_test_excess_return),
            'repair_excess_return': float(baseline_repair_excess)
        },
        'dynamic': {
            'train_max_drawdown': float(dynamic_train_max_dd),
            'test_excess_return': float(dynamic_test_excess_return),
            'repair_excess_return': float(dynamic_repair_excess)
        }
    }
    with open(output_dir / 'summary.json', 'w') as f:
        json.dump(summary, f, indent=2)

    report = {
        'train_max_drawdown_improvement_pct': float((abs(baseline_train_max_dd) - abs(dynamic_train_max_dd)) / abs(baseline_train_max_dd) * 100 if baseline_train_max_dd != 0 else 0),
        'test_excess_return_ratio': float(dynamic_test_excess_return / baseline_test_excess_return if baseline_test_excess_return != 0 else 1),
        'repair_excess_change': float(dynamic_repair_excess - baseline_repair_excess),
        'adoption_pass': adoption_pass
    }
    with open(output_dir / 'report.yaml', 'w') as f:
        yaml.dump(report, f)

    l4_ack = {
        'acknowledged': True,
        'reason': 'Dynamic drawdown exit evaluation complete'
    }
    with open(output_dir / 'l4_ack.yaml', 'w') as f:
        yaml.dump(l4_ack, f)

    diagnostic = {
        'data_range': {'train': [args.train_start, args.train_end], 'test': [args.test_start, args.test_end]},
        'repair_period': ['2020-01-01', '2020-12-31'],
        'status': 'completed'
    }
    with open(output_dir / 'diagnostic.yaml', 'w') as f:
        yaml.dump(diagnostic, f)

if __name__ == '__main__':
    main()