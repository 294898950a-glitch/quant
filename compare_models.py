"""三组对比: 5F baseline vs 5F+3 AI vs 5F+3 NumHolder"""
import logging, time, pandas as pd
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
import sys; sys.path.insert(0, ".")

from strategies.cb_redemption.data import build_historical_snapshots, clear_cache, SNAPSHOT_CACHE
from strategies.cb_redemption.backtest import BacktestEngine, calc_performance

# 清缓存重建
import os
if SNAPSHOT_CACHE.exists():
    os.remove(SNAPSHOT_CACHE)
clear_cache()

print("⏳ 重建快照（含AI信号）...")
t0 = time.time()
snap = build_historical_snapshots(start="20230101", force_rebuild=True)
print(f"快照: {snap.shape[0]:,}行, {snap.shape[1]}列, {snap['date'].nunique()}交易日, {time.time()-t0:.0f}s")

# 检查AI列
for c in ["ai_signal_score","ai_reduction_score","ai_is_original"]:
    if c in snap.columns:
        print(f"  {c}: {snap[c].nunique()} unique, non-zero={snap[c].astype(bool).sum():,}")
    else:
        print(f"  {c}: MISSING!")

# 三组权重
groups = {
    "5F Baseline (无holder)": [1.222, -2.981, -2.514, 1.627, -0.614, 0, 0, 0],
    "5F+3Num (数值Holder)": [1.222, -2.981, -2.514, 1.627, -0.614, 0.5, 0.3, 0.5],
    "5F+3AI (DeepSeek信号)": [1.222, -2.981, -2.514, 1.627, -0.614, 0.5, 0.5, 0.5],
}

results = {}
for name, w in groups.items():
    print(f"\n--- {name} ---")
    # 用对应列做评分
    engine = BacktestEngine(
        weights=w,
        hold_max_days=15, target_exit_pct=10.0, stop_loss_pct=-8.0,
        max_positions=5, top_k=10,
    )
    trades = engine.run(snapshots=snap)
    perf = calc_performance(trades)
    results[name] = perf
    print(f"  trades={perf['total_trades']}, win={perf['win_rate']}%, avg_ret={perf['avg_return']:+.2f}%, total_pnl={perf['total_pnl']:+.0f}, sharpe={perf.get('sharpe',0):.2f}")

print(f"\n{'='*60}")
print(f"{'Metric':<20} {'5F Base':>12} {'5F+3Num':>12} {'5F+3AI':>12}")
print(f"{'-'*60}")
for k in ["total_trades","win_rate","avg_return","total_pnl","sharpe"]:
    vals = [f"{results[n].get(k,0)}" for n in groups]
    print(f"{k:<20} {vals[0]:>12} {vals[1]:>12} {vals[2]:>12}")
