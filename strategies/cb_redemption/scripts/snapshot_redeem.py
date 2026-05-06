#!/usr/bin/env python3
"""
可转债强赎数据快照 — 定时存档脚本

每执行一次，将 JSL 强赎页面数据存为一个时间戳 CSV 文件。
由 cronjob 驱动（建议交易时段每 10 分钟执行一次）。

存储路径: ~/projects/quant/data/cb_redemption/snapshots/YYYY/MM/YYYY-MM-DD_HHMM.csv
"""

import sys
import os
from datetime import datetime
from pathlib import Path

# 找到项目根目录
ROOT = Path(__file__).resolve().parent.parent.parent.parent  # quant/
sys.path.insert(0, str(ROOT))

from strategies.cb_redemption.data import get_cb_redeem_data

SNAPSHOT_DIR = ROOT / "data" / "cb_redemption" / "snapshots"


def save_snapshot() -> str:
    """抓取 JSL 强赎快照并保存为 CSV，返回文件路径。"""
    df = get_cb_redeem_data()
    if df.empty:
        print("WARN: empty snapshot, skip")
        return ""

    now = datetime.now()
    # YYYY/MM/YYYY-MM-DD_HHMM.csv
    subdir = SNAPSHOT_DIR / str(now.year) / f"{now.month:02d}"
    subdir.mkdir(parents=True, exist_ok=True)

    filename = f"{now.strftime('%Y-%m-%d_%H%M')}.csv"
    filepath = subdir / filename

    # 固定列顺序、加时间戳列
    df["_snapshot_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(filepath, index=False, encoding="utf-8-sig")

    print(f"OK: {len(df)} bonds -> {filepath}")
    return str(filepath)


if __name__ == "__main__":
    save_snapshot()
