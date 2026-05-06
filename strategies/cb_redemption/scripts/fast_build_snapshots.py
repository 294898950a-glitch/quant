#!/usr/bin/env python3
"""Fast snapshot builder — 跳过 _calc_redeem_progress_at 加速，仅用公告记录。"""
import sys, os, logging, time
sys.path.insert(0, '/home/jay/projects/quant')

from pathlib import Path
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

WAREHOUSE_DIR = Path.home() / "projects/quant/data/cb_warehouse"
SNAPSHOT_CACHE = WAREHOUSE_DIR / "strong_timeline_snapshots.parquet"

t0 = time.time()

basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")
daily = pd.read_parquet(WAREHOUSE_DIR / "cb_daily.parquet")
call = pd.read_parquet(WAREHOUSE_DIR / "cb_call.parquet")

name_map = basic.set_index("ts_code")["bond_short_name"].to_dict()
size_map = (basic.set_index("ts_code")["remain_size"] / 1e8).to_dict()

call_sorted = call.sort_values(["ts_code", "ann_date"])
call_by_code = {}
for code, group in call_sorted.groupby("ts_code"):
    call_by_code[code] = group

daily["trade_date"] = daily["trade_date"].astype(str)
daily_pivot = daily.pivot_table(index="trade_date", columns="ts_code", values="pct_chg")
market_sentiment = daily_pivot.rolling(5, min_periods=3).mean().mean(axis=1)

daily_by_code = {code: grp.sort_values("trade_date") for code, grp in daily.groupby("ts_code")}
logging.info(f"Pre-grouped: {len(daily_by_code)} CBs, {len(call_by_code)} with call records")

start = sys.argv[1] if len(sys.argv) > 1 else "20240301"
dates = sorted(daily["trade_date"].unique())
dates = [d for d in dates if d >= start]
logging.info(f"Building: {len(dates)} days, {dates[0]} ~ {dates[-1]}")

rows = []
for i, date_str in enumerate(dates):
    day_data = daily[daily["trade_date"] == date_str]
    if day_data.empty:
        continue
    sent = float(market_sentiment.get(date_str, 0.0))
    for _, row in day_data.iterrows():
        code = row["ts_code"]
        close = row["close"]
        if close <= 0:
            continue
        premium = float(row.get("cb_over_rate", 0.0) or 0.0)
        remain = float(size_map.get(code, 0.0))
        cb_hist = daily_by_code.get(code)
        if cb_hist is not None:
            pos = cb_hist[cb_hist["trade_date"] == date_str]
            if not pos.empty:
                idx = cb_hist.index.get_loc(pos.index[0])
                mom = cb_hist.iloc[max(0, idx - 5): idx + 1]["pct_chg"].sum()
            else:
                mom = 0.0
        else:
            mom = 0.0
        progress = 0.0
        records = call_by_code.get(code)
        if records is not None and not records.empty:
            past_ann = records[records["ann_date"] <= date_str]
            if not past_ann.empty:
                latest = past_ann.iloc[-1]
                iv = str(latest.get("is_call", ""))
                ct = str(latest.get("call_type", ""))
                if any(kw in iv for kw in ["已强赎","实施强赎","强赎实施","已赎回"]):
                    progress = 1.0
                elif "公告实施强赎" in ct:
                    progress = 1.0
                elif "董事会决议提前赎回" in ct or "提前赎回" in ct:
                    progress = 0.95
                elif "满足强赎条件" in iv:
                    progress = 0.8
                elif "触发" in iv or "提示" in iv:
                    progress = 0.4
        rows.append({
            "date": date_str, "ts_code": code,
            "bond_short_name": name_map.get(code, code),
            "close": close, "premium_ratio": round(premium, 2),
            "redeem_progress": round(progress, 4),
            "remaining_size": round(remain, 2),
            "stock_momentum": round(mom, 2),
            "market_sentiment": round(sent, 2),
        })
    if (i + 1) % 50 == 0:
        elapsed = time.time() - t0
        eta = elapsed / (i + 1) * len(dates) - elapsed
        logging.info(f"  [{i+1}/{len(dates)}] {elapsed:.0f}s elapsed, {eta:.0f}s eta")

result = pd.DataFrame(rows)
result.to_parquet(str(SNAPSHOT_CACHE), index=False)
elapsed = time.time() - t0
logging.info(f"DONE: {len(result)} rows, {result['date'].nunique()} days, {elapsed:.0f}s")
print(f"OK {len(result)} {result['date'].nunique()} {elapsed:.0f}")
