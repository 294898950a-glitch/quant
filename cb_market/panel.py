"""The one daily panel every measurement reads.

One row per bond per trading day, built from the warehouse. Nothing here is a
model: prices, the day's true conversion value, the contract, and quantities
that follow from them by arithmetic. A missing input stays NaN.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.cb_arb.call_condition import is_call_eligible

WAREHOUSE = Path(__file__).resolve().parent.parent / "data" / "cb_warehouse"
INPUT_FILES = (
    "cb_daily.parquet", "cb_conv_value_pit.parquet", "cb_basic.parquet", "stk_daily_qfq.parquet",
    "cb_contract_terms.parquet", "cb_call.parquet", "cb_events.parquet",
)


def input_fingerprints() -> dict[str, dict[str, object]]:
    """Size and content hash of every input file, so a result can be tied to the data it came from."""
    out = {}
    for name in INPUT_FILES:
        path = WAREHOUSE / name
        out[name] = {"bytes": path.stat().st_size, "sha256_16": hashlib.sha256(path.read_bytes()).hexdigest()[:16]}
    return out


def load_panel(start: str = "20170101") -> pd.DataFrame:
    cb = pd.read_parquet(WAREHOUSE / "cb_daily.parquet", columns=["ts_code", "trade_date", "close", "vol"])
    pit = pd.read_parquet(
        WAREHOUSE / "cb_conv_value_pit.parquet", columns=["ts_code", "trade_date", "conv_value", "bond_value"]
    )
    basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet", columns=["ts_code", "stk_code", "contract_maturity_date"])
    stock = pd.read_parquet(WAREHOUSE / "stk_daily_qfq.parquet", columns=["stk_code", "trade_date", "close"])
    terms = pd.read_parquet(WAREHOUSE / "cb_contract_terms.parquet")[[
        "ts_code", "conversion_start_date", "call_window", "call_required_days", "call_trigger_pct",
        "call_status", "maturity_redemption_price", "coupons_pct",
    ]]
    calls = pd.read_parquet(WAREHOUSE / "cb_call.parquet", columns=["ts_code", "ann_date"])

    p = cb.merge(pit, on=["ts_code", "trade_date"], how="left").merge(basic, on="ts_code", how="left")
    p = p.merge(stock.rename(columns={"close": "stock_close"}), on=["stk_code", "trade_date"], how="left")
    p = p.merge(terms, on="ts_code", how="left").merge(calls.rename(columns={"ann_date": "call_ann_date"}), on="ts_code", how="left")
    p = p.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    p.loc[p["conv_value"] <= 0, "conv_value"] = np.nan
    g = p.groupby("ts_code")

    p["amount"] = p["close"] * p["vol"]  # yuan; vol is in bonds (张)
    p["listing_day_index"] = g.cumcount()
    p["premium"] = p["close"] / p["conv_value"] - 1.0
    # the two floors and what the market adds on top of them
    p["time_value"] = p["close"] - np.maximum(p["conv_value"], p["bond_value"])

    # identity: log return = stock + conversion-price change + premium change
    p["r"] = np.log(p["close"] / g["close"].shift(1))
    r_conv_value = np.log(p["conv_value"] / g["conv_value"].shift(1))
    p["r_stock"] = np.log(p["stock_close"] / g["stock_close"].shift(1))
    p["r_conv_price"] = r_conv_value - p["r_stock"]
    p["r_premium"] = p["r"] - r_conv_value

    p["in_conversion_period"] = p["trade_date"] >= p["conversion_start_date"].fillna("99999999")
    p["called"] = p["trade_date"] >= p["call_ann_date"].fillna("99999999")

    # Call trigger by each bond's own contract. The rule itself lives in call_condition.py (the one
    # implementation); here it is only fed each bond's terms. stock / conversion price = conv_value / 100,
    # and days before the conversion period cannot count.
    ratio = (p["conv_value"] / 100.0).where(p["in_conversion_period"])
    eligible = pd.Series(False, index=p.index)  # stays False where the clause is not parsed; see call_status
    parsed = p[p["call_status"] == "parsed"]
    for _, g in parsed.groupby("ts_code"):
        eligible.loc[g.index] = is_call_eligible(
            ratio.loc[g.index].to_numpy(), window=int(g["call_window"].iloc[0]),
            threshold=float(g["call_trigger_pct"].iloc[0]) / 100.0, required=int(g["call_required_days"].iloc[0]),
        )
    p["call_eligible"] = eligible

    p = p[p["trade_date"] >= start].reset_index(drop=True)
    p.attrs["inputs"] = input_fingerprints()
    p.attrs["end"] = str(p["trade_date"].max())
    return p
