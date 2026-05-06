"""
下载 PEAD 所需的价格数据
- 每只转债的日度行情 (cb_daily)
- 转债等权指数 (cb_basic + 自行构建)
"""
import os
import pandas as pd
import numpy as np
import tushare as ts
from pathlib import Path
from datetime import datetime, timedelta
import time

OUTPUT = Path.home() / "projects/quant/data/cb_pead/raw"
DEEP = pd.read_parquet(OUTPUT / "all_down_events.parquet")
DEEP = DEEP[DEEP['ratio'] <= 0.75].dropna(subset=['ratio'])
CODES = DEEP['bond_code'].unique()
print(f"深幅事件: {len(DEEP)}, 唯一转债: {len(CODES)}")

# 设置 tushare (改地址!)
ts.set_token(os.environ['TUSHARE_TOKEN'])
pro = ts.pro_api()
pro._DataApi__http_url = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')

# ============================================================
# Step 1: 下载所有 CB 日线
# ============================================================
CACHE = OUTPUT / "cb_daily.parquet"
if CACHE.exists():
    daily = pd.read_parquet(CACHE)
    print(f"已有缓存: {len(daily)} 行, {daily['ts_code'].nunique()} 只")
    existing_codes = set(daily['ts_code'].unique())
    new_codes = [c for c in CODES if f"{c[:6]}.SH" not in existing_codes and f"{c[:6]}.SZ" not in existing_codes]
    print(f"需要新下载: {len(new_codes)} 只")
else:
    daily = pd.DataFrame()
    new_codes = CODES.tolist()

if new_codes:
    dfs = []
    errors = 0
    t0 = time.time()
    for i, code in enumerate(new_codes):
        # 尝试 SH/SZ
        for mkt in ['SZ', 'SH']:
            ts_code = f"{code[:6]}.{mkt}"
            try:
                df = pro.cb_daily(ts_code=ts_code, start_date='20170101', end_date='20260427')
                if df is not None and not df.empty:
                    df['bond_code_raw'] = code
                    dfs.append(df)
                    break
            except Exception:
                continue
        
        if (i+1) % 30 == 0:
            elapsed = time.time() - t0
            print(f"  {i+1}/{len(new_codes)} ({elapsed:.0f}s), {len(dfs)} 只有数据, {errors} 错误")
        time.sleep(0.5)  # 120/min = 1 per 0.5s
    
    if dfs:
        new_daily = pd.concat(dfs, ignore_index=True)
        daily = pd.concat([daily, new_daily], ignore_index=True)
        daily.to_parquet(CACHE, index=False)
        print(f"✅ 保存 {len(daily)} 行到 {CACHE}")

# ============================================================
# Step 2: 构建 T+n 收益率矩阵
# ============================================================
print("\nStep 2: 计算 T+n 收益率...")

daily['trade_date'] = pd.to_datetime(daily['trade_date'], format='%Y%m%d')
daily = daily.sort_values(['ts_code', 'trade_date'])
daily['next_close'] = daily.groupby('ts_code')['close'].shift(-1)
daily['daily_ret'] = daily['next_close'] / daily['close'] - 1

# 构建 (code, date) -> price lookup
price_dict = {}
for _, row in daily.iterrows():
    code = row['bond_code_raw'][:6] if 'bond_code_raw' in row.index else row['ts_code'][:6]
    price_dict[(code, row['trade_date'])] = row['close']

# 对每个 event 计算 T+0..T+60 价格
series_rows = []
matched = 0
for _, evt in DEEP.iterrows():
    code = evt['bond_code'][:6]
    event_date = pd.to_datetime(evt['meeting_date'])
    
    # 找最近的交易日
    offsets = range(-10, 65)
    prices = {}
    for offset in offsets:
        target = event_date + timedelta(days=offset)
        # 找交易日
        for d in range(7):
            candidate = target + timedelta(days=d)
            key = (code, candidate)
            if key in price_dict:
                prices[offset] = price_dict[key]
                break
    
    if 0 in prices:
        row_idx = len(series_rows)
        row = {
            'bond_code': code,
            'bond_name': evt.get('bond_name', ''),
            'meeting_date': event_date,
            'ratio': evt['ratio'],
        }
        for t in range(61):
            actual_offset = prices.get(t, np.nan)
            if actual_offset is np.nan or actual_offset == 0:
                # 尝试累加收益
                row[f'T+{t}'] = np.nan
            else:
                row[f'T+{t}'] = actual_offset
        series_rows.append(row)
        matched += 1

print(f"匹配成功: {matched}/{len(DEEP)}")

series_df = pd.DataFrame(series_rows)
series_df.to_csv(OUTPUT / "cb_pead_series_v2.csv", index=False)
print(f"✅ 保存 {len(series_df)} 行到 cb_pead_series_v2.csv")
