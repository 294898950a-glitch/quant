#!/usr/bin/env python3
"""
可转债强赎博弈 — 因子独立验证

从第一性原理出发：
1. 5个因子各自对"次日上涨概率/幅度"有没有预测力？
2. 因子之间相关性如何（多重共线性检查）
3. 哪个因子拖后腿？哪个是真正驱动？

方法：
- IC (Information Coefficient): 因子值 vs 次日收益的秩相关
- 分组回测: 按因子分为5组，看各组收益
- 因子相关性矩阵
"""
import sys, os
sys.path.insert(0, "/home/jay/projects/quant")

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import numpy as np
import pandas as pd
from scipy import stats
from strategies.cb_redemption.data import build_historical_snapshots, SNAPSHOT_CACHE, _load_parquet
from strategies.cb_redemption.config import DATA_DIR, LOGIT_WEIGHTS

import warnings
warnings.filterwarnings("ignore")

# ── 1. 加载快照 & 价格数据 ──

# 快照：包含每日因子值
snapshots = build_historical_snapshots("20210101", "20260424")
logger = logging.getLogger(__name__)

# 加载转债日线（含次日收盘价）
daily = _load_parquet("daily")

# ── 2. 构建因子-收益分析DataFrame ──

# 对每个交易日t的每个转债：
#   因子 = 快照中的值（t收盘后可知）
#   收益 = t+1日的涨跌幅

factors = ["redeem_progress", "premium_ratio", "remaining_size",
           "stock_momentum", "market_sentiment"]

def build_factor_return_df():
    """构建 (因子, 次日收益) 面板数据。"""
    rows = []
    
    # 按日期分组处理快照
    dates = sorted(snapshots["date"].unique())
    
    for i, date_str in enumerate(dates):
        if i >= len(dates) - 1:
            break
        
        next_date = dates[i + 1]
        day_snap = snapshots[snapshots["date"] == date_str]
        
        # 获取次日收盘价
        next_day = daily[daily["trade_date"] == next_date]
        if next_day.empty:
            continue
        
        next_prices = next_day.set_index("ts_code")["close"]
        
        for _, row in day_snap.iterrows():
            code = row["ts_code"]
            if code not in next_prices.index:
                continue
            
            entry_price = row["close"]
            exit_price = next_prices[code]
            
            if entry_price <= 0 or exit_price <= 0:
                continue
            
            ret = (exit_price - entry_price) / entry_price * 100  # 次日收益 %
            
            rows.append({
                "date": date_str,
                "ts_code": code,
                "ret_next_day": ret,
                **{f: row[f] for f in factors}
            })
    
    return pd.DataFrame(rows)

logger.info("正在构建因子-收益面板...")
df = build_factor_return_df()
logger.info(f"面板数据: {len(df)} 行, {df['date'].nunique()} 交易日")

# ── 3. IC (Information Coefficient) 分析 ──
# 每个因子与次日收益的秩相关

print("\n" + "="*80)
print("📊 因子 IC 分析 (Spearman Rank Correlation with Next-Day Return)")
print("="*80)

ic_results = []
for f in factors:
    valid = df[[f, "ret_next_day"]].dropna()
    if len(valid) < 100:
        continue
    rho, pval = stats.spearmanr(valid[f], valid["ret_next_day"])
    ic_results.append({
        "factor": f,
        "ic": round(rho, 4),
        "p_value": round(pval, 6),
        "significant": "✅" if pval < 0.01 else ("⚠️" if pval < 0.05 else "❌"),
        "samples": len(valid),
    })

ic_df = pd.DataFrame(ic_results).sort_values("ic", key=abs, ascending=False)
ic_df["abs_ic"] = ic_df["ic"].abs()
ic_df["rank"] = range(1, len(ic_df)+1)
ic_df = ic_df[["rank", "factor", "ic", "abs_ic", "p_value", "significant", "samples"]]
print(ic_df.to_string(index=False))

# ── 4. 按因子分组回测 ──
# 每个因子分5组（Q1最有利于强赎，Q5最不利），看各组平均收益

print("\n" + "="*80)
print("📊 因子分组收益率 (Q1=最有利, Q5=最不利)")
print("="*80)

for f in factors:
    valid = df[[f, "ret_next_day"]].dropna()
    if len(valid) < 100:
        continue
    # 方向判断：对于正因子（预期与收益正相关），Q1=高值
    # 统一用高值为Q1
    try:
        valid[f"{f}_q"] = pd.qcut(valid[f], 5, labels=[f"Q{i}" for i in range(5, 0, -1)],
                                   duplicates="drop")
    except ValueError:
        # 如果仍有问题，用等距分段
        valid[f"{f}_q"] = pd.cut(valid[f], 5, labels=[f"Q{i}" for i in range(5, 0, -1)])
    
    group_stats = valid.groupby(f"{f}_q")["ret_next_day"].agg(["mean", "median", "std", "count"])
    group_stats.columns = ["mean_ret(%)", "median_ret(%)", "std", "count"]
    group_stats = group_stats.round(2)
    
    # 单调性：Q1的mean > Q5的mean 表示因子方向正确
    q1_mean = group_stats.loc["Q1", "mean_ret(%)"] if "Q1" in group_stats.index else 0
    q5_mean = group_stats.loc["Q5", "mean_ret(%)"] if "Q5" in group_stats.index else 0
    monotonic = "✅" if q1_mean > q5_mean else "❌"
    
    print(f"\n因子: {f} (单调性: {monotonic})")
    print(group_stats.to_string())
    print(f"  Q1 - Q5 spread: {round(q1_mean - q5_mean, 2)}%")

# ── 5. 因子相关性矩阵 ──

print("\n" + "="*80)
print("📊 因子相关性矩阵 (Pearson)")
print("="*80)

corr = df[factors].corr()
print(corr.round(3).to_string())
print()

# 高相关标记
high_corr_pairs = []
for i in range(len(factors)):
    for j in range(i+1, len(factors)):
        c = corr.iloc[i, j]
        if abs(c) > 0.5:
            high_corr_pairs.append((factors[i], factors[j], c))

if high_corr_pairs:
    print("⚠️ 高相关对 (|r| > 0.5)：")
    for f1, f2, c in high_corr_pairs:
        print(f"  {f1} ↔ {f2}: r={c:.3f}")
else:
    print("✅ 无高相关对 (所有 |r| < 0.5)")

# ── 6. 复合评分验证 ──
# 用当前权重看复合评分的预测力

from strategies.cb_redemption.backtest import logit_prob

# 获取最新权重
try:
    from strategies.cb_redemption.config import LOGIT_WEIGHTS
    weights = LOGIT_WEIGHTS
except:
    weights = [1.23, -3.87, -1.25, 0.80, 1.13]

print("\n" + "="*80)
print(f"📊 复合评分 IC (当前权重: {[round(w,2) for w in weights]})")
print("="*80)

valid = df.dropna(subset=factors + ["ret_next_day"])
scores = valid.apply(lambda r: logit_prob(
    r["redeem_progress"], r["premium_ratio"], r["remaining_size"],
    r["stock_momentum"], r["market_sentiment"], weights
), axis=1)

rho, pval = stats.spearmanr(scores, valid["ret_next_day"])
print(f"复合评分 vs 次日收益: IC={rho:.4f}, p={pval:.6f} {'✅' if pval<0.01 else '❌'}")

# 评分分组
try:
    valid["score_bin"] = pd.qcut(scores, 5, labels=["Q5", "Q4", "Q3", "Q2", "Q1"], duplicates="drop")
except ValueError:
    valid["score_bin"] = pd.cut(scores, 5, labels=["Q5", "Q4", "Q3", "Q2", "Q1"])
group_ret = valid.groupby("score_bin")["ret_next_day"].agg(["mean", "median", "count"])
group_ret.columns = ["mean_ret(%)", "median_ret(%)", "count"]
print(group_ret.round(2).to_string())

# 信号阈值命中率分析
print("\n" + "="*80)
print("📊 高评分候选池的分布")
print("="*80)

for thr in [0.3, 0.35, 0.4, 0.45, 0.5, 0.6]:
    hits = scores[scores >= thr]
    hit_next_ret = valid.loc[hits.index, "ret_next_day"]
    if len(hits) > 10:
        mean_ret = hit_next_ret.mean()
        win_rate = (hit_next_ret > 0).mean() * 100
        print(f"  score ≥ {thr:.2f}: {len(hits)} 次, 次日均收益={mean_ret:.2f}%, 胜率={win_rate:.1f}%")
    else:
        print(f"  score ≥ {thr:.2f}: {len(hits)} 次 (太少)")

print("\n✅ 因子验证完成!")
