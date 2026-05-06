"""PEAD 完整分析 — 复现 + 基准收益调整"""
import pandas as pd, numpy as np
from scipy import stats
from pathlib import Path
import akshare as ak

ROOT = Path.home() / "projects/quant/data/cb_pead"

# Load original CSV (confirmed complete: 112 events)
df = pd.read_csv(ROOT / "raw/cb_down_events_with_returns.csv")
df["ratio"] = df["after_price"] / df["before_price"]
df["is_deep"] = df["ratio"] <= 0.75

deep = df[df["is_deep"]]
shallow = df[~df["is_deep"]]
print(f"总事件: {len(df)}")
print(f"大幅下修 (ratio≤0.75): {len(deep)}")
print(f"小幅下修: {len(shallow)}")
print()

# ============================================================
# 1. 基准复现: deep vs shallow
# ============================================================
print("=" * 70)
print("1. 基准复现 — Deep vs Shallow (ratio≤0.75)")
print("=" * 70)
print(f"{'Window':<7} {'Deep n':>6} {'Deep CAR':>10} {'t':>6} {'Shallow n':>8} {'Shallow CAR':>10} {'t':>6} {'Diff t':>6}")
print("-" * 70)
for w in [1, 5, 10, 20, 30, 40, 60]:
    col = f"T{w}_ret"
    d = deep[col].dropna()
    s = shallow[col].dropna()
    if len(d) < 3 or len(s) < 3:
        continue
    d_t = stats.ttest_1samp(d, 0)
    s_t = stats.ttest_1samp(s, 0)
    dx = stats.ttest_ind(d, s, equal_var=False)
    print(f"T+{w:<4d} {len(d):>6} {d.mean():+9.2f}% {d_t.statistic:+5.2f}  {len(s):>8} {s.mean():+9.2f}% {s_t.statistic:+5.2f}  {dx.statistic:+5.2f}")

# ============================================================
# 2. 基准收益调整 (转债等权指数)
# ============================================================
print(f"\n{'=' * 70}")
print("2. 基准收益调整 — 转债等权指数")
print("=" * 70)

try:
    idx = ak.bond_cb_index_jsl()
    idx["date"] = pd.to_datetime(idx["price_dt"])  # col is 'price_dt'
    idx = idx.sort_values("date").set_index("date")
    # Use 'price' (等权) or 'idx_price' (加权)
    idx_col = "price"
    print(f"  指数数据: {len(idx)} 天 ({idx.index[0].date()} ~ {idx.index[-1].date()})")
    
    # 指数仅覆盖~1年，对效2023-2024事件可能无匹配
    matched = 0
    for w in [1, 5, 10, 20, 30, 60]:
        bench_rets = []
        excess_rets = []
        for _, evt in df.iterrows():
            md = pd.to_datetime(str(evt["meeting_date"])[:10])
            car = evt.get(f"T{w}_ret", np.nan)
            if pd.isna(car) or md not in idx.index:
                bench_rets.append(np.nan)
                excess_rets.append(np.nan)
                continue
            ei = idx.index.get_loc(md)
            if ei + w < len(idx):
                b = (idx.iloc[ei + w][idx_col] / idx.iloc[ei][idx_col] - 1) * 100
                bench_rets.append(b)
                excess_rets.append(car - b)
                matched += 1
            else:
                bench_rets.append(np.nan)
                excess_rets.append(np.nan)
        
        df[f"bench_T{w}_ret"] = bench_rets
        df[f"excess_T{w}_ret"] = excess_rets
    
    print(f"  匹配事件: {matched} (指数覆盖期内的 event-window 对)")
    
    # Re-slice after adding excess columns
    deep2 = df[df["is_deep"]]
    shallow2 = df[~df["is_deep"]]
    print(f"\n  {'Window':<7} {'Deep CAR':>10} {'Excess':>10} {'Shallow CAR':>10} {'Excess':>10}")
    print("  " + "-" * 48)
    for w in [1, 5, 10, 20, 30, 60]:
        ex_col = f"excess_T{w}_ret"
        d_car = deep2[f"T{w}_ret"].dropna().mean()
        d_ex = deep2[ex_col].dropna().mean() if ex_col in deep2.columns and deep2[ex_col].notna().any() else np.nan
        s_car = shallow2[f"T{w}_ret"].dropna().mean()
        s_ex = shallow2[ex_col].dropna().mean() if ex_col in shallow2.columns and shallow2[ex_col].notna().any() else np.nan
        d_ex_str = f"{d_ex:+9.2f}%" if not np.isnan(d_ex) else "N/A"
        s_ex_str = f"{s_ex:+9.2f}%" if not np.isnan(s_ex) else "N/A"
        print(f"  T+{w:<4d} {d_car:+9.2f}% {d_ex_str:>9} {s_car:+9.2f}% {s_ex_str:>9}")

except Exception as e:
    print(f"  ⚠️ 指数获取失败: {e}")

# ============================================================
# 3. 修到底分类 (ratio≤0.6 近似)
# ============================================================
print(f"\n{'=' * 70}")
print("3. 修到底 (ratio≤0.6) vs 非修到底")
print("=" * 70)

df["is_floor"] = df["ratio"] <= 0.6
floor = df[df["is_floor"]]
not_floor = df[~df["is_floor"]]
print(f"  修到底 (ratio≤0.6): {len(floor)}, 非: {len(not_floor)}")

print(f"\n  {'Window':<7} {'Floor n':>7} {'Floor CAR':>10} {'Not n':>6} {'Not CAR':>10} {'t-diff':>6}")
print("  " + "-" * 50)
for w in [1, 5, 10, 20, 30, 60]:
    col = f"T{w}_ret"
    f = floor[col].dropna()
    nf = not_floor[col].dropna()
    if len(f) < 3 or len(nf) < 3:
        continue
    dx = stats.ttest_ind(f, nf, equal_var=False)
    print(f"  T+{w:<4d} {len(f):>7} {f.mean():+9.2f}% {len(nf):>6} {nf.mean():+9.2f}% {dx.statistic:+5.2f}")

# ============================================================
# 4. 年度 + 多次下修
# ============================================================
print(f"\n{'=' * 70}")
print("4. 年度分布")
print("=" * 70)

df["meeting_dt"] = pd.to_datetime(df["meeting_date"])
df["year"] = df["meeting_dt"].dt.year
for yr in sorted(df["year"].dropna().unique()):
    sub = df[df["year"] == yr]
    d = sub[sub["is_deep"]]
    print(f"  {int(yr)}: total={len(sub)}, deep={len(d)}, T+20={d['T20_ret'].dropna().mean():+.2f}%, T+60={d['T60_ret'].dropna().mean():+.2f}%")

# 多次下修
print(f"\n5. 多次下修债券")
bond_counts = df["bond_id"].value_counts()
multi = bond_counts[bond_counts >= 2]
print(f"  多次下修的转债: {len(multi)} 只")
for bid in multi.index:
    sub = df[df["bond_id"] == bid].sort_values("meeting_date")
    dates = sub["meeting_date"].tolist()
    ratios = sub["ratio"].tolist()
    info = " | ".join(f"{d}({r:.2f})" for d, r in zip(dates, ratios))
    name = sub["name"].iloc[0]
    print(f"  {bid} {name}: {len(sub)}次 — {info}")

print(f"\n✅ 分析完成")
