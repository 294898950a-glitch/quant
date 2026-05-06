#!/usr/bin/env python3
"""对 stk_daily.parquet 的正股价格做前复权处理，输出 qfq_stk_daily.parquet

方法：利用 pre_close 字段恢复复权因子。
  如果 pre_close[t] == close[t-1] → 无除权，adj_ratio[t] = 1
  如果 pre_close[t] < close[t-1] → 除权，adj_ratio[t] = close[t-1] / pre_close[t]
  累积乘积得到累计复权因子 cum_adj
  前复权价格 = 原始价格 × cum_adj（以最后一个交易日为基准）

数据仓库：~/projects/quant/data/cb_warehouse/
"""
import time
import pandas as pd
import numpy as np
from pathlib import Path

WAREHOUSE = Path.home() / "projects" / "quant" / "data" / "cb_warehouse"

def compute_qfq():
    t0 = time.time()
    
    print(f"📂 加载 stk_daily.parquet ...")
    stk = pd.read_parquet(WAREHOUSE / "stk_daily.parquet")
    print(f"   {len(stk):,} 行, {stk['ts_code'].nunique()} 只正股")
    
    # 确保排序
    stk = stk.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    
    # 计算每日复权比值
    # 对每个正股：factor = prev_close / pre_close
    # 如果没除权（prev_close == pre_close），factor = 1
    prev_close = stk.groupby("ts_code")["close"].shift(1)
    stk["adj_ratio"] = np.where(
        prev_close.notna() & (prev_close != stk["pre_close"]),
        prev_close / stk["pre_close"],
        1.0
    )
    
    # 累积复权因子（以最新为基准，前复权）
    stk["cum_adj"] = stk.groupby("ts_code")["adj_ratio"].cumprod()
    
    # 前复权价格
    stk["close_qfq"] = (stk["close"] * stk["cum_adj"]).round(2)
    stk["open_qfq"] = (stk["open"] * stk["cum_adj"]).round(2)
    stk["high_qfq"] = (stk["high"] * stk["cum_adj"]).round(2)
    stk["low_qfq"] = (stk["low"] * stk["cum_adj"]).round(2)
    
    # 统计
    n_adj = int((stk["adj_ratio"] != 1.0).sum())
    print(f"📊 除权除息日: {n_adj:,} 天 ({n_adj/len(stk)*100:.2f}%)")
    
    # 只保留需要的列
    cols_out = ["ts_code", "trade_date", "close_qfq", "open_qfq", "high_qfq", "low_qfq",
                "close", "open", "high", "low", "pre_close", "pct_chg", "vol", "amount"]
    out = stk[[c for c in cols_out if c in stk.columns]]
    
    out_path = WAREHOUSE / "stk_daily_qfq.parquet"
    out.to_parquet(out_path, index=False)
    elapsed = time.time() - t0
    print(f"✅ 输出 {out_path}")
    print(f"   {len(out):,} 行, 耗时 {elapsed:.0f}s")
    return out

if __name__ == "__main__":
    compute_qfq()
