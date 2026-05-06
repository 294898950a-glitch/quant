#!/usr/bin/env python3
"""拉取所有正股日线 — 最简版，print无缓冲"""
import os, time, sys
from datetime import datetime

import pandas as pd
import tushare as ts

API_TOKEN = os.environ['TUSHARE_TOKEN']
API_URL = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')
WD = "/home/jay/projects/quant/data/cb_warehouse"

ts.set_token(API_TOKEN)
pro = ts.pro_api()
pro._DataApi__http_url = API_URL

cb = pd.read_parquet(f"{WD}/cb_basic.parquet")
all_codes = sorted(set(cb["stk_code"].dropna().unique()))
all_codes = [c for c in all_codes if not c.startswith("0")]
print(f"正股总数: {len(all_codes)}", flush=True)

try:
    existing = pd.read_parquet(f"{WD}/stk_daily.parquet")
    fetched = set(existing["ts_code"].unique())
    print(f"已有: {len(fetched)} 只", flush=True)
except:
    existing = pd.DataFrame()
    fetched = set()

codes = [c for c in all_codes if c not in fetched]
if not codes:
    print("全部已拉取", flush=True)
    sys.exit(0)

print(f"待拉取: {len(codes)} 只", flush=True)

all_rows = []
errors = 0
t0 = time.time()

for i, code in enumerate(codes):
    try:
        df = pro.daily(ts_code=code, start_date="20180101",
                       end_date=datetime.now().strftime("%Y%m%d"))
        if df is not None and not df.empty:
            all_rows.append(df)
    except Exception as e:
        errors += 1
        print(f"[{i+1}/{len(codes)}] ❌ {code}: {e}", flush=True)

    if (i + 1) % 20 == 0:
        total_rows = sum(len(r) for r in all_rows)
        elapsed = time.time() - t0
        print(f"[{i+1}/{len(codes)}] +{total_rows}行 {elapsed:.0f}s", flush=True)

    time.sleep(0.35)

elapsed = time.time() - t0

if all_rows:
    new_df = pd.concat(all_rows, ignore_index=True)
    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    combined.to_parquet(f"{WD}/stk_daily.parquet", index=False)
    print(f"✅ 完成: {len(all_rows)}/{len(codes)} 成功, {errors} 失败, {len(combined)} 总行, {elapsed:.0f}s", flush=True)
else:
    print("无新数据", flush=True)
