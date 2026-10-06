"""Accounting identities that hold for any convertible bond or any portfolio of them.

price = conversion value * (1 + premium), conversion value = 100 * stock / conversion price, so

    log return = stock return + conversion-price change + premium change

exactly, every day. Whatever a holding earned, it earned through these three.
"""

from __future__ import annotations

import pandas as pd

COMPONENTS = ["r", "r_stock", "r_conv_price", "r_premium"]


def decompose(panel: pd.DataFrame, weights: pd.Series | None = None, by: str = "year") -> pd.DataFrame:
    """Sum of daily cross-sectional mean log returns, split into the three components.

    weights: optional per-row weight (e.g. 1 for held bonds, 0 otherwise); equal weight over all rows if None.
    by: "year" or a column name of the panel that is constant within a day.
    """
    rows = panel.dropna(subset=COMPONENTS)
    if weights is not None:
        rows = rows[weights.reindex(rows.index).fillna(0) > 0]
    daily = rows.groupby("trade_date")[COMPONENTS].mean()
    key = daily.index.str[:4] if by == "year" else rows.groupby("trade_date")[by].first().reindex(daily.index)
    out = daily.groupby(key).sum()
    out["bond_days"] = rows.groupby(rows["trade_date"].str[:4] if by == "year" else rows[by]).size()
    return out
