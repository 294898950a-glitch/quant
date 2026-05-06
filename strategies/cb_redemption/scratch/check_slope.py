import pandas as pd
import sys

df = pd.read_parquet('/home/jay/projects/quant/data/cb_warehouse/strong_timeline_snapshots.parquet')
records = pd.read_parquet('/home/jay/projects/quant/strategies/cb_redemption/output/holder_records_v2.parquet')
meta = pd.read_parquet('/home/jay/projects/quant/strategies/cb_redemption/output/announcement_metadata.parquet')

reports = records.merge(meta[['announcement_id','announcement_time']], on='announcement_id')
reports['report_date'] = pd.to_datetime(reports['announcement_time'], unit='ms').dt.strftime('%Y%m%d')

basic = pd.read_parquet('/home/jay/projects/quant/data/cb_warehouse/cb_basic.parquet')
stock_map = {}
for _, r in basic.iterrows():
    stk = str(r.get('stk_code',''))
    num = ''.join(c for c in stk if c.isdigit())
    if len(num) == 6:
        stock_map[num] = r['ts_code']

reports['ts_code'] = reports['stock_code'].astype(str).str[:6].map(stock_map)
reports = reports.dropna(subset=['ts_code'])
post2023 = reports[reports['report_date'] >= '20230101']
cnt = post2023.groupby('ts_code')['report_date'].nunique()
multi = cnt[cnt >= 3].index.tolist()

print(f'2023年后有3+报告的股票: {len(multi)}只')
for code in multi[:3]:
    rpts = post2023[post2023['ts_code']==code]['report_date'].sort_values().tolist()
    print(f'  {code}: {rpts}')

if multi:
    code = multi[0]
    sub = df[(df['ts_code']==code) & (df['top1_ratio_latest']>0)][['date','top1_ratio_latest','top1_ratio_slope','top1_ratio_drawdown']].sort_values('date')
    uniq = sub.drop_duplicates(subset=['top1_ratio_latest','top1_ratio_slope','top1_ratio_drawdown'])
    print(f'\n{code} 去重后特征变化:')
    print(uniq.to_string())
    print(f'\n不同特征组合: {len(uniq)} (>1 = 时变OK)')
