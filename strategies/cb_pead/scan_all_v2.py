"""
PEAD 完整扫描 V2 — 修复旧债缺失"变动原因"列的 bug
旧债 API 返回列: ['转债名称','股东大会日','下修前转股价','下修后转股价','新转股价生效日期','下修底价']
新债 API 返回列: 多了 '变动原因' 
→ 无'变动原因'列时，所有行都是下修事件
"""
import time, pandas as pd
from pathlib import Path
import akshare as ak

OUTPUT = Path.home() / "projects/quant/data/cb_pead/raw/all_down_events.parquet"
CHECKPOINT = Path.home() / "projects/quant/data/cb_pead/raw/.scan_checkpoint"

print("Step 1: 获取全量转债列表...")
all_cb = ak.bond_zh_cov()
codes = sorted(all_cb["债券代码"].astype(str).str[:6].unique())
print(f"  {len(codes)} 只转债")

# 断点续扫
done = set()
if CHECKPOINT.exists():
    done = set(CHECKPOINT.read_text().strip().split("\n"))
    print(f"  已扫: {len(done)}")

print("Step 2: 逐只扫描下修历史...")
events = []
errors = 0
t0 = time.time()

for i, code in enumerate(codes):
    if code in done:
        continue
    try:
        adj = ak.bond_cb_adj_logs_jsl(symbol=code)
        if adj is None or adj.empty:
            done.add(code)
            continue
        
        has_reason = "变动原因" in adj.columns
        
        for _, row in adj.iterrows():
            # V2 fix: 无"变动原因"列 → 全部是下修事件
            if has_reason:
                chg_reason = str(row.get("变动原因", ""))
                if "下修" not in chg_reason and "修正" not in chg_reason:
                    continue
            
            events.append({
                "bond_code": code,
                "bond_name": row.get("转债名称", ""),
                "meeting_date": str(row.get("股东大会日", ""))[:10],
                "before_price": float(row.get("下修前转股价", 0)),
                "after_price": float(row.get("下修后转股价", 0)),
                "bottom_price": float(row.get("下修底价", 0)),
                "effective_date": str(row.get("新转股价生效日期", ""))[:10],
                "change_reason": row.get("变动原因", "") if has_reason else "",
            })
        
        done.add(code)
    except Exception as e:
        errors += 1
        done.add(code)
        if errors <= 5:
            print(f"  ⚠️ {code}: {e}")
    
    if (i + 1) % 200 == 0:
        elapsed = time.time() - t0
        print(f"  {i+1}/{len(codes)} ({elapsed:.0f}s), {len(events)} events, {len(done)} done")
        CHECKPOINT.write_text("\n".join(sorted(done)))
    
    time.sleep(0.15)

# 最后保存 checkpoint
CHECKPOINT.write_text("\n".join(sorted(done)))

elapsed = time.time() - t0
df = pd.DataFrame(events)
print(f"\nStep 3: 结果...")
print(f"  {len(df)} 下修事件, {df.bond_code.nunique()} 只转债")
print(f"  errors: {errors}, elapsed: {elapsed:.0f}s")

if not df.empty:
    df["ratio"] = df["after_price"] / df["before_price"]
    df["is_deep"] = df["ratio"] <= 0.75
    df["year"] = pd.to_datetime(df["meeting_date"], errors="coerce").dt.year
    
    print(f"\n  年代分布:")
    print(df["year"].value_counts().sort_index().to_string())
    print(f"\n  下修幅度分布:")
    print(f"    大幅 (ratio≤0.75): {df.is_deep.sum()}")
    print(f"    小幅: {(~df.is_deep).sum()}")
    
    df.to_parquet(OUTPUT, index=False)
    print(f"\n  ✅ 保存到 {OUTPUT}")
else:
    print("  ⚠️ 无数据")
