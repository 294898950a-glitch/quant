import pandas as pd
import numpy as np
from pathlib import Path
from datetime import timedelta

df = pd.read_csv(Path.home() / "projects/quant/data/cb_pead/raw/cb_down_events_with_returns.csv")
df['meeting_date'] = pd.to_datetime(df['meeting_date'])
df['ratio'] = df['after_price'] / df['before_price']
deep = df[df['ratio'] <= 0.75].sort_values('meeting_date')

gaps = deep['meeting_date'].diff().dropna().dt.days
print(f'信号间隔: 均值={gaps.mean():.1f}天, 中位={gaps.median():.0f}天, 最小={gaps.min()}天, 最大={gaps.max()}天')

max_conc = 0
for _, row in deep.iterrows():
    start = row['meeting_date']
    end = start + timedelta(days=60)
    conc = ((deep['meeting_date'] >= start) & (deep['meeting_date'] <= end)).sum()
    max_conc = max(max_conc, conc)
print(f'最大并发持仓: {max_conc}')

cols = ['T1_ret','T5_ret','T10_ret','T20_ret','T30_ret','T40_ret','T60_ret']
print(f'\n深幅下修 CAR (n={len(deep)}):')
for c in cols:
    s = deep[c].dropna()
    print(f'  {c}: {s.mean():+.2f}%  (t={s.mean()/s.std()*np.sqrt(len(s)):.2f})')

days = (df['meeting_date'].max() - df['meeting_date'].min()).days
print(f'\n年化频率:')
print(f'  全部事件: {len(df)/(days/365):.1f}/年')
print(f'  深幅下修: {len(deep)/(days/365):.1f}/年')
