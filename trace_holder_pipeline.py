import pandas as pd, numpy as np

BASE = "/home/jay/projects/quant"

# ====== Step 1: 原始数据 ======
records = pd.read_parquet(f"{BASE}/strategies/cb_redemption/output/holder_records_v2.parquet")
meta = pd.read_parquet(f"{BASE}/strategies/cb_redemption/output/announcement_metadata.parquet")

print("=== Step 1: holder_records_v2 (PDF解析出品) ===")
print(f"Shape: {records.shape}")
print(f"Columns: {records.columns.tolist()}")
print(records.head(3).to_string())
print()

# ====== Step 2: 拼接日期 ======
reports = records.merge(meta[["announcement_id", "announcement_time"]], on="announcement_id")
reports["report_date"] = pd.to_datetime(reports["announcement_time"], unit="ms")
reports["report_date_str"] = reports["report_date"].dt.strftime("%Y%m%d")

print("=== Step 2: 拼接 announcement_metadata → 得到日期 ===")
print("用 announcement_id 做 join，announcement_time (ms) → datetime → YYYYMMDD")
print(f"日期范围: {reports['report_date_str'].min()} ~ {reports['report_date_str'].max()}")
print(reports[["announcement_id","stock_code","top1_ratio","report_date_str"]].head(3).to_string())
print()

# ====== Step 3: 逐报告计算 slope/drawdown ======
reports = reports[reports["top1_ratio"].notna()].sort_values(["stock_code", "report_date_str"])
demo_code = "002717"
demo = reports[reports["stock_code"]==demo_code].sort_values("report_date_str")

print(f"=== Step 3: slope/drawdown 计算 ({demo_code}, {len(demo)}份报告) ===")
ratios = demo["top1_ratio"].values
for i in range(len(ratios)):
    hist = ratios[:i+1]
    if len(hist) == 1:
        slope = 0.0; dd = 0.0
    elif len(hist) == 2:
        slope = (hist[-1] - hist[0]) / (len(hist)-1)
        dd = max(hist) - hist[-1]
    else:
        x = np.arange(len(hist), dtype=float); y = hist.astype(float)
        xm, ym = x.mean(), y.mean()
        num = ((x-xm)*(y-ym)).sum(); den = ((x-xm)**2).sum()
        slope = num/den if den!=0 else 0
        dd = max(hist) - hist[-1]
    print(f"  #{i} ({demo.iloc[i]['report_date_str']}): ratio={hist[-1]:.2f}, hist={[f'{h:.1f}' for h in hist]}, slope={slope:+.4f}/report, dd={dd:.2f}")
print()

# ====== Step 4: stock_code → ts_code 映射 ======
basic = pd.read_parquet(f"{BASE}/data/cb_warehouse/cb_basic.parquet")
stock_map = {}
for _, r in basic.iterrows():
    stk = str(r.get("stk_code",""))
    num = "".join(c for c in stk if c.isdigit())
    if len(num) == 6:
        stock_map[num] = r["ts_code"]

reports["ts_code"] = reports["stock_code"].astype(str).str[:6].map(stock_map)
mapped = reports.dropna(subset=["ts_code"])

print(f"=== Step 4: stock_code → ts_code 映射 ===")
print(f"映射池: {len(stock_map)}个 (cb_basic.stk_code按6位数字提取)")
print(f"映射成功率: {len(mapped)}/{len(reports)} ({len(mapped)/len(reports)*100:.1f}%)")
print()

# ====== Step 5: merge_asof 到快照 ======
snap = pd.read_parquet(f"{BASE}/data/cb_warehouse/strong_timeline_snapshots.parquet")
has_h = snap["top1_ratio_latest"] > 0
has_s = snap["top1_ratio_slope"] != 0

print("=== Step 5: merge_asof → 快照 ===")
print(f"快照: {snap.shape[0]:,} 行, {snap.shape[1]} 列, {snap['ts_code'].nunique()} 转债")
print(f"  top1_ratio_latest>0: {has_h.sum():,}行 ({has_h.sum()/len(snap)*100:.1f}%)")
print(f"  slope≠0:            {has_s.sum():,}行 ({has_s.sum()/len(snap)*100:.1f}%)")
print(f"  有holder数据的转债:  {snap[has_h]['ts_code'].nunique()}/{snap['ts_code'].nunique()} ({snap[has_h]['ts_code'].nunique()/snap['ts_code'].nunique()*100:.1f}%)")
print()

# 演示 merge_asof 效果
ts = stock_map.get(demo_code, "?")
print(f"=== merge_asof 效果: {demo_code} → {ts} ===")
sub = snap[(snap["top1_ratio_latest"]>0)&(snap["ts_code"]==ts)][["date","top1_ratio_latest","top1_ratio_slope","top1_ratio_drawdown"]].sort_values("date")
uniq = sub.drop_duplicates(subset=["top1_ratio_latest","top1_ratio_slope","top1_ratio_drawdown"])
print(uniq.to_string())
print()
print("解读: 新报告发布前，merge_asof backfill → 几百个交易日特征冻结不变")
print("      新报告发布后 → slope/drawdown 更新为新值 → 再次冻结到下一份报告")
print("      90只股票 × 7.2份报告 = ~600次更新，覆盖39万行快照")
