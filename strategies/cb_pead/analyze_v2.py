"""PEAD V2 全量分析"""
import pandas as pd, numpy as np
from pathlib import Path

sdf = pd.read_csv(Path.home() / "projects/quant/data/cb_pead/raw/cb_pead_series_v2.csv")
sdf['meeting_date'] = pd.to_datetime(sdf['meeting_date'])
sdf.set_index(['bond_code','meeting_date'], inplace=True)

print(f"总事件: {len(sdf)}, 时间: {sdf.index.get_level_values(1).min().date()} ~ {sdf.index.get_level_values(1).max().date()}")

# T+n 收益率
for T in [1,5,10,20,30,40,60]:
    col = f'T+{T}'
    ret = sdf[col] / sdf['T+0'] - 1
    print(f"  T+{T:>2}: mean={ret.mean():+.3f}  median={ret.median():+.3f}  std={ret.std():.3f}  n={ret.notna().sum()}")

# 按 ratio 分组
print(f"\n=== 按下修幅度分组 ===")
for lo, hi, label in [(0, 0.75, 'deep ≤0.75'), (0.75, 0.85, '0.75-0.85'), (0.85, 1.0, 'shallow >0.85')]:
    sub = sdf[(sdf['ratio'] > lo) & (sdf['ratio'] <= hi)]
    t60 = sub['T+60'] / sub['T+0'] - 1
    t20 = sub['T+20'] / sub['T+0'] - 1
    print(f"  {label}: n={len(sub)}  T+20={t20.mean():+.3f}  T+60={t60.mean():+.3f}  t-stat={t60.mean()/t60.std()*np.sqrt(len(t60.dropna())):.2f}")

# 年代
print(f"\n=== 年代分布 ===")
sdf['year'] = sdf.index.get_level_values(1).year
print(sdf['year'].value_counts().sort_index().to_string())

# 年化频率
days = (sdf.index.get_level_values(1).max() - sdf.index.get_level_values(1).min()).days
print(f"\n年化频率: {len(sdf)/(days/365):.1f}/年")
