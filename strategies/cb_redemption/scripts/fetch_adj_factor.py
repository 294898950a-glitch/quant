#!/usr/bin/env python3
"""拉取所有正股的复权因子，保存到本地"""
import os, time, sys
import pandas as pd
import tushare as ts

API_TOKEN = os.environ['TUSHARE_TOKEN']
API_URL = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')
WD = "/home/jay/projects/quant/data/cb_warehouse"

ts.set_token(API_TOKEN)
pro = ts.pro_api()
pro._DataApi__http_url = API_URL

# 所有需要正股的转债
cb = pd.read_parquet(f"{WD}/cb_basic.parquet")
all_codes = sorted(set(cb["stk_code"].dropna().unique()))
print(f"正股总数: {len(all_codes)}", flush=True)

# 已有
try:
    existing = pd.read_parquet(f"{WD}/adj_factor.parquet")
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
no_data = 0
t0 = time.time()

for i, code in enumerate(codes):
    try:
        df = pro.adj_factor(ts_code=code)
        if df is not None and not df.empty:
            all_rows.append(df)
        else:
            no_data += 1
    except Exception as e:
        errors += 1
        print(f"[{i+1}/{len(codes)}] ❌ {code}: {e}", flush=True)

    if (i + 1) % 20 == 0:
        total_rows = sum(len(r) for r in all_rows)
        elapsed = time.time() - t0
        print(f"[{i+1}/{len(codes)}] +{total_rows}行 {elapsed:.0f}s", flush=True)

    time.sleep(0.3)

elapsed = time.time() - t0

if all_rows:
    new_df = pd.concat(all_rows, ignore_index=True)
    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    combined.to_parquet(f"{WD}/adj_factor.parquet", index=False)
    print(f"✅ 完成: {len(all_rows)}/{len(codes)} 成功, {errors} 失败, {no_data} 无数据, "
          f"{len(combined)} 总行, {elapsed:.0f}s", flush=True)
else:
    print("无新数据", flush=True)
