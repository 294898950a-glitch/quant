#!/usr/bin/env python3
"""
强赎策略短持模式 — 每10分钟优化 cron 入口

从快照缓存加载数据 → 预构建快照 → optimizer 15次迭代 → 推送Telegram
"""
import sys, os, logging, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from data import build_historical_snapshots, clear_cache
from optimizer import run_optimization

def main():
    t0 = time.time()

    # 1. 预构建快照（走缓存）
    clear_cache()
    snapshots = build_historical_snapshots()
    n_stocks = len(snapshots)
    n_days = snapshots["date"].nunique()
    logging.info(f"快照: {n_stocks} 行, {n_days} 交易日")

    # 2. 运行优化器（15次迭代）
    result = run_optimization(
        iterations=15,
        score_mode="balanced",
        push_telegram=True,
        verbose=False,
        apply_if_improved=True,
        hold_max_days=5,
        target_exit_pct=4.0,
        stop_loss_pct=-3.0,
        max_positions=10,
        top_k=10,
    )

    elapsed = time.time() - t0
    logging.info(f"总耗时: {elapsed:.1f}s")

if __name__ == "__main__":
    main()
