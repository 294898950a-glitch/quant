#!/usr/bin/env python3
"""
Dynamic drawdown exit evaluator for cb_arb_value_gap_switch strategy.
Simulates baseline (fixed max holding) and grid tests of drawdown-adaptive
maximum holding periods. Produces summary.json, report.yaml, l4_ack.yaml,
and diagnostic.yaml.
"""
import argparse
import os
import json
import yaml
from datetime import datetime

import pandas as pd
import numpy as np


def load_data(data_root):
    """Load required data files and return a merged daily gap DataFrame."""
    gap_path = os.path.join(
        data_root,
        "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17",
        "daily_value_gap_amounts.parquet",
    )
    gap_df = pd.read_parquet(gap_path)

    # optional cb_basic and stk_daily are not strictly needed for this executor
    # but we verify they exist
    cb_basic = pd.read_parquet(os.path.join(data_root, "data/cb_warehouse/cb_basic.parquet"))
    stk_daily = pd.read_parquet(os.path.join(data_root, "data/cb_warehouse/stk_daily_qfq.parquet"))

    # ensure date column is datetime
    gap_df["date"] = pd.to_datetime(gap_df["date"])
    return gap_df.sort_values(["date", "cb_code"])


def simulate(
    gap_df,
    start_date,
    end_date,
    base_max_hold,
    drawdown_threshold=None,
    exit_multiplier=None,
    dynamic=False,
):
    """
    Simulate portfolio value evolution.

    Parameters
    ----------
    gap_df : DataFrame with columns date, cb_code, theoretical_value, tradable_gap_amount
    start_date, end_date : str
    base_max_hold : int, fixed maximum holding days for baseline
    drawdown_threshold : float, drawdown level that triggers shortened hold
    exit_multiplier : float, multiplier applied to base_max_hold when drawdown > threshold
    dynamic : bool, if True use dynamic hold; else use fixed base_max_hold

    Returns
    -------
    list of daily portfolio values
    """
    mask = (gap_df["date"] >= start_date) & (gap_df["date"] <= end_date)
    subset = gap_df.loc[mask].copy()
    if subset.empty:
        return [1.0]

    price_pivot = subset.pivot(
        index="date", columns="cb_code", values="theoretical_value"
    )
    gap_pivot = subset.pivot(
        index="date", columns="cb_code", values="tradable_gap_amount"
    )

    dates = sorted(subset["date"].unique())
    cash = 1.0
    shares = 0.0
    current_cb = None
    hold_days = 0
    peak = 1.0
    portfolio_values = []

    for date in dates:
        price_today = np.nan
        if current_cb is not None:
            if current_cb in price_pivot.columns:
                price_t