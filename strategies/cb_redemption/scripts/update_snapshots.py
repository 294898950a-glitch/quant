"""
增量更新快照缓存 — 只构建最新 N 个交易日并追加。
用于每日收盘后 cronjob 刷新。

用法：
    python -m strategies.cb_redemption.scripts.update_snapshots [--days 5]

参数：
    --days N: 向前构建 N 天，覆盖可能的新数据（默认 5）
"""

import sys
import time
import logging
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
logger = logging.getLogger(__name__)

from strategies.cb_redemption.data import (
    build_historical_snapshots,
    SNAPSHOT_CACHE,
    clear_cache,
)


def refresh_snapshots(incremental_days: int = 5) -> pd.DataFrame:
    """增量刷新快照缓存。

    策略：
        1. 加载现有缓存
        2. 获取缓存中最大日期
        3. 从 max_date - incremental_days 开始重新构建
        4. 用新数据替换旧数据中的重叠+追加部分

    这样只重新计算最近 N 天，而不是全量 1528 天。
    如果缓存不存在则全量构建。
    """
    if not SNAPSHOT_CACHE.exists():
        logger.info("缓存不存在，全量构建...")
        clear_cache()
        return build_historical_snapshots(force_rebuild=True)

    old = pd.read_parquet(str(SNAPSHOT_CACHE))
    max_date = old["date"].max()
    logger.info(f"现有缓存: {len(old)} 行, 最新日 = {max_date}")

    # 从 max_date - incremental_days 开始构建（留出余量）
    start = str(int(max_date) - incremental_days * 10000 + 100)
    if start < "20200101":
        start = "20200101"

    logger.info(f"增量构建: 从 {start} 开始...")
    clear_cache()
    new_part = build_historical_snapshots(start=start, force_rebuild=True)

    # 合并：去掉旧中重叠部分（new_part 的日期），追加新部分
    new_dates = set(new_part["date"].unique())
    old_filtered = old[~old["date"].isin(new_dates)]
    merged = pd.concat([old_filtered, new_part], ignore_index=True)
    merged = merged.sort_values(["date", "ts_code"]).reset_index(drop=True)

    # 覆盖写入缓存
    merged.to_parquet(str(SNAPSHOT_CACHE), index=False)
    logger.info(
        f"✅ 增量更新完成: {len(old)} -> {len(merged)} 行, "
        f"新增 {(len(merged) - len(old))} 行"
    )
    return merged


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="增量更新快照缓存")
    parser.add_argument("--days", type=int, default=5, help="向前构建天数")
    args = parser.parse_args()

    t0 = time.time()
    df = refresh_snapshots(incremental_days=args.days)
    elapsed = time.time() - t0
    print(f"完成: {elapsed:.1f}s, {len(df)} 行, {df['date'].nunique()} 交易日")
