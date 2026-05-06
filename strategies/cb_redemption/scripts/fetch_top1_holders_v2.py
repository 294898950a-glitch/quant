import os
import tushare as ts
import pandas as pd
import numpy as np
import time

API_TOKEN = os.environ['TUSHARE_TOKEN']
API_URL = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')
ts.set_token(API_TOKEN)
pro = ts.pro_api()
pro._DataApi__http_url = API_URL

# 先拿转债和正股的映射
df_issue = pro.cb_issue()
df_basic = pro.cb_basic()

m = df_issue.merge(df_basic[['ts_code','stk_code','issue_size']], 
                   on='ts_code', how='inner', suffixes=('_issue', '_basic'))
m['shd_ratio_pct'] = m['shd_ration_size'] / (m['issue_size_basic'] * 1e8) * 100

# 构建正股代码
stk_list = m[['ts_code','stk_code','shd_ratio_pct']].dropna(subset=['stk_code']).copy()
stk_list['stk_num'] = stk_list['stk_code'].str.split('.').str[0].str[:6]
stk_list['stk_suffix'] = stk_list['stk_code'].str.split('.').str[1]
stk_list['stk_full'] = stk_list['stk_num'].str.zfill(6) + '.' + stk_list['stk_suffix']

# 所有正股
all_stock_codes = stk_list['stk_full'].unique()
print(f"正股总数: {len(all_stock_codes)}")
print(f"前5: {all_stock_codes[:5]}")

# 测试单个
test_code = all_stock_codes[0]
print(f"\n测试: {test_code}")
try:
    df = pro.top10_holders(ts_code=test_code, limit=3)
    print(f"返回列: {list(df.columns)}")
    print(f"行数: {len(df)}")
    if len(df) > 0:
        print(df.head(2).to_string())
except Exception as e:
    print(f"err: {e}")

# 批量拉取
holder_records = []
errors = 0
N = len(all_stock_codes)

for i, full_code in enumerate(all_stock_codes):
    try:
        df = pro.top10_holders(ts_code=full_code, limit=5)
        
        if i == 0:
            print(f"列名: {list(df.columns)}")
        
        if df is not None and len(df) > 0:
            # 用hold_ratio最高的作为第一大股东
            df_sorted = df.sort_values('hold_ratio', ascending=False)
            top1 = df_sorted.iloc[0]
            holder_records.append({
                'ts_code': full_code,
                'top1_name': top1['holder_name'],
                'top1_ratio': float(top1['hold_ratio']),
                'top1_type': top1['holder_type'],
                'end_date': top1['end_date'],
                'ann_date': top1['ann_date'],
            })
        else:
            errors += 1
        
        if (i+1) % 100 == 0:
            print(f"进度: {i+1}/{N}, 成功: {len(holder_records)}, 失败: {errors}")
        
        time.sleep(0.35)  # 150/min ≈ 0.4s
        
    except Exception as e:
        errors += 1
        if errors <= 5 or errors % 20 == 0:
            print(f"[{full_code}] 错误: {type(e).__name__}: {str(e)[:100]}")
        time.sleep(0.5)

print(f"\n完成: 成功 {len(holder_records)}, 失败 {errors}")

# 保存
if len(holder_records) > 0:
    df_top1 = pd.DataFrame(holder_records)
    df_top1.to_parquet('/home/jay/projects/quant/data/cb_warehouse/top1_holders.parquet')
    print(f"已保存: top1_holders.parquet ({len(df_top1)}行)")

# 合并
top1_df = df_top1 if len(holder_records) > 0 else pd.DataFrame()

if len(top1_df) > 0:
    final = stk_list.merge(top1_df, on='ts_code', how='left')
    final['ts_code'] = final['ts_code']  # 这是正股代码
    # 创建原始转债代码的映射
    final.to_parquet('/home/jay/projects/quant/data/cb_warehouse/cb_top1_holder_est.parquet')
    print(f"合并结果: {len(final)}行, 有top1: {final['top1_ratio'].notna().sum()}")
    print(f"top1_ratio描述:\n{final['top1_ratio'].describe()}")
