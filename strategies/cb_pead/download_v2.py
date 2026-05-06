"""快速下载 + 构建 T+n 收益率"""
import os
import pandas as pd, numpy as np, tushare as ts, time
from pathlib import Path
from datetime import datetime, timedelta

OUT = Path.home() / "projects/quant/data/cb_pead/raw"
DEEP = pd.read_parquet(OUT / "all_down_events.parquet")
DEEP = DEEP[DEEP['ratio'] <= 0.75].dropna(subset=['ratio','meeting_date'])
DEEP['meeting_date'] = pd.to_datetime(DEEP['meeting_date'])
CODES = sorted(DEEP['bond_code'].unique())
print(f"深幅事件: {len(DEEP)}, 唯一转债: {len(CODES)}")

ts.set_token(os.environ['TUSHARE_TOKEN'])
pro = ts.pro_api()
pro._DataApi__http_url = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')

# ====== 下载 ======
CACHE = OUT / "cb_daily_all.parquet"
if CACHE.exists():
    daily = pd.read_parquet(CACHE)
    existing = set(daily['bond_code'].unique())
    new_codes = [c for c in CODES if c not in existing]
    print(f"已有 {len(existing)} 只, 需下载 {len(new_codes)} 只")
else:
    daily = pd.DataFrame()
    new_codes = CODES

if new_codes:
    dfs, errors, t0 = [], 0, time.time()
    for i, code in enumerate(new_codes):
        df = None
        for mkt in ['SZ','SH']:
            try:
                r = pro.cb_daily(ts_code=f"{code[:6]}.{mkt}", start_date='20160101', end_date='20260427')
                if r is not None and not r.empty:
                    r['bond_code'] = code
                    dfs.append(r)
                    break
            except Exception:
                continue
        if df is None:
            errors += 1
        if (i+1) % 50 == 0:
            print(f"  {i+1}/{len(new_codes)} ({time.time()-t0:.0f}s), {len(dfs)} ok, {errors} err")
        time.sleep(0.5)
    
    if dfs:
        new_daily = pd.concat(dfs, ignore_index=True)
        daily = pd.concat([daily, new_daily], ignore_index=True)
        daily.to_parquet(CACHE, index=False)
    print(f"✅ 下载完成: {len(daily)} 行, {daily['bond_code'].nunique()} 只")

# ====== T+n 收益率 ======
print("\n计算 T+n 收益率...")
daily['td'] = pd.to_datetime(daily['trade_date'], format='%Y%m%d', errors='coerce')
daily = daily.sort_values(['bond_code','td'])

series_rows, matched = [], 0
for _, evt in DEEP.iterrows():
    code = evt['bond_code'][:6]
    ed = evt['meeting_date']
    bond = daily[daily['bond_code'] == code]
    
    # 找 >= event_date 的第一个交易日
    idx = bond['td'].searchsorted(ed)
    if idx >= len(bond):
        continue
    
    prices = bond.iloc[idx:idx+61][['td','close']].copy()
    if prices.empty:
        continue
    
    row = {'bond_code': code, 'bond_name': evt['bond_name'],
           'meeting_date': ed, 'ratio': evt['ratio']}
    for t in range(min(61, len(prices))):
        row[f'T+{t}'] = float(prices.iloc[t]['close'])
    series_rows.append(row)
    matched += 1

print(f"匹配: {matched}/{len(DEEP)}")

sdf = pd.DataFrame(series_rows)
sdf.to_csv(OUT / "cb_pead_series_v2.csv", index=False)
print(f"✅ {OUT / 'cb_pead_series_v2.csv'}  ({len(sdf)} 行)")
