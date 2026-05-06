"""修复合并逻辑：top1_holders 的 ts_code 是正股代码（如 600461.SH）"""
import os
import tushare as ts
import pandas as pd
import numpy as np
import time
import sys

API_TOKEN = os.environ['TUSHARE_TOKEN']
API_URL = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')
ts.set_token(API_TOKEN)
pro = ts.pro_api()
pro._DataApi__http_url = API_URL

# 1. 拉基础数据
print("拉取 cb_issue + cb_basic...")
df_issue = pro.cb_issue()
df_basic = pro.cb_basic()

m = df_issue.merge(df_basic[['ts_code','stk_code','issue_size','remain_size','stk_short_name','list_date']], 
                   on='ts_code', how='inner', suffixes=('_issue', '_basic'))

m['shd_ratio_pct'] = m['shd_ration_size'] / (m['issue_size_basic'] * 1e8) * 100

stk_codes = m[['ts_code','stk_code','stk_short_name','shd_ratio_pct','list_date']].dropna(subset=['stk_code']).copy()
stk_codes = stk_codes[stk_codes['stk_code'].apply(lambda x: len(str(x)) == 6)]
stk_codes['stk_full'] = stk_codes['stk_code'].apply(lambda x: str(int(x))[0:6] + ('.SH' if str(int(x)).startswith('6') else '.SZ'))

all_stk = stk_codes['stk_full'].unique()
print(f"共有 {len(all_stk)} 只正股需要拉取")

# 2. 批量拉取
holder_records = []
count = 0
errors = 0
N = len(all_stk)

for full_code in all_stk:
    try:
        df = pro.top10_holders(ts_code=full_code, limit=10)
        count += 1
        if count % 50 == 0:
            print(f"  进度: {count}/{N}, 错误: {errors}")
        
        if df is None or len(df) == 0:
            errors += 1
            time.sleep(0.2)
            continue
        
        top1 = df.loc[df['hold_ratio'].idxmax()]
        holder_records.append({
            'stk_full': full_code,
            'top1_holder_name': top1['holder_name'],
            'top1_hold_ratio': top1['hold_ratio'],
            'top1_holder_type': top1['holder_type'],
            'ann_date': top1['ann_date'],
            'end_date': top1['end_date']
        })
        time.sleep(0.3)
    except Exception as e:
        errors += 1
        if errors % 10 == 0:
            print(f"  累积错误: {errors}, 最近: {e}")
        time.sleep(0.5)

print(f"\n完成: 成功 {len(holder_records)}, 失败 {errors}")

# 3. 保存
df_top1 = pd.DataFrame(holder_records)
df_top1.to_parquet('/home/jay/projects/quant/data/cb_warehouse/top1_holders.parquet')
print(f"已保存: top1_holders.parquet")

# 4. 合并
final = stk_codes.merge(df_top1, on='stk_full', how='left')
print(f"合并后: {len(final)} 行, 有top1数据: {final['top1_hold_ratio'].notna().sum()}")

# 5. 计算交互特征
final['est_major_holder_cb_ratio'] = final['shd_ratio_pct'] * final['top1_hold_ratio'] / 100
final['major_holder_cb_est'] = final['est_major_holder_cb_ratio'].clip(0, 100)

print(f"\n大股东持债比例估算:")
print(final['major_holder_cb_est'].describe())

final.to_parquet('/home/jay/projects/quant/data/cb_warehouse/cb_top1_holder_est.parquet')
print("已保存: cb_top1_holder_est.parquet")
