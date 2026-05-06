"""
可转债强赎博弈策略 — 主入口

运行方式（从 ~/projects/quant 目录）:
    source .venv/bin/activate && python -m strategies.cb_redemption.main

支持参数:
    --dry-run    只打印结果，不推送 Telegram
    --interval   连续运行模式（每 N 分钟轮询一次，默认 30 分钟）
    --once       单次运行（默认）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, date
from pathlib import Path

# 确保项目根在 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from strategies.cb_redemption.config import (
    MONITOR_INTERVAL_MINUTES,
    LOGS_DIR,
)
from strategies.cb_redemption.data import (
    get_cb_list,
    get_cb_redeem_data,
    is_trade_day,
    activate_venv,
)
from strategies.cb_redemption.signals import RedemptionTracker, LogitScorer
from strategies.cb_redemption.push import send_redemption_alert, send_error_alert

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / f"cb_redemption_{date.today().isoformat()}.log"),
    ],
)
logger = logging.getLogger(__name__)


async def run_pipeline(dry_run: bool = False) -> dict:
    """
    执行完整监控管线。

    Returns:
        管线运行统计
    """
    start_time = time.time()
    stats = {
        "total_bonds": 0,
        "scanned": 0,
        "alerts": [],
        "errors": [],
        "duration_sec": 0,
    }

    try:
        # 1. 获取全市场数据
        logger.info("📥 获取全市场可转债数据...")
        df_list = get_cb_list()
        df_redeem = get_cb_redeem_data()
        stats["total_bonds"] = len(df_list)
        logger.info(f"✅ 可转债共 {len(df_list)} 只, 强赎数据 {len(df_redeem)} 只")

        # 2. 构建 Tracker 和 Scorer
        tracker = RedemptionTracker()
        scorer = LogitScorer()

        # 3. 从强赎数据获取所有代码
        cb_codes = df_redeem["代码"].tolist() if "代码" in df_redeem.columns else df_list["债券代码"].tolist()
        stats["scanned"] = len(cb_codes)

        # 4. 预加载正股数据（大幅提升批量计算速度）
        logger.info(f"📦 预加载 {len(cb_codes)} 只转债的对应正股数据...")
        cached = tracker.preload_stock_cache(cb_codes)
        logger.info(f"✅ 正股缓存就绪: {cached} 只")

        # 5. 批量计算状态和评分
        logger.info(f"🧮 计算 {len(cb_codes)} 只转债的强赎状态...")
        tracker_results = tracker.batch_compute(cb_codes)

        if not tracker_results:
            logger.warning("⚠️ 未获取到有效追踪结果")
            return stats

        logger.info(f"✅ 状态计算完成，共 {len(tracker_results)} 条")

        # 5. 批量评分
        scored_results = scorer.batch_score(tracker_results)
        logger.info(f"✅ 评分完成")

        # 6. 过滤预警级及以上 (概率 >= 30%)
        alerts = [r for r in scored_results if r.get("probability", 0) >= 0.30]
        alerts.sort(key=lambda x: x.get("probability", 0), reverse=True)
        stats["alerts"] = alerts

        logger.info(f"🔔 预警信号: {len(alerts)} 只")
        for a in alerts[:5]:
            prob = a.get("probability", 0) * 100
            name = a.get("cb_name", "?")
            code = a.get("cb_code", "?")
            level = a.get("signal_level", "?")
            logger.info(f"  {level} {name}({code}) 概率 {prob:.1f}%")

        # 7. 推送 Telegram
        if not dry_run:
            success = send_redemption_alert(
                alerts=alerts,
                num_total=stats["total_bonds"],
                num_scanned=stats["scanned"],
            )
            if not success:
                stats["errors"].append("Telegram 推送失败")
        else:
            # Dry run: 打印详细结果
            print("\n" + "=" * 60)
            print(f"📊 DRY RUN — 共扫描 {stats['scanned']} 只, 预警 {len(alerts)} 只")
            print("=" * 60)
            for a in alerts:
                prob = a.get("probability", 0) * 100
                name = a.get("cb_name", "?")
                code = a.get("cb_code", "?")
                level = a.get("signal_level", "?")
                premium = a.get("conversion_premium", "?")
                progress = a.get("trigger_progress", 0) * 100
                print(f"{level} {name}({code}) | 概率 {prob:.0f}% | 溢价 {premium}% | 进度 {progress:.0f}%")

    except Exception as e:
        logger.exception(f"❌ 管线运行异常: {e}")
        stats["errors"].append(str(e))
        if not dry_run:
            send_error_alert(f"监控管线异常: {e}")

    finally:
        stats["duration_sec"] = time.time() - start_time
        logger.info(f"⏱ 耗时 {stats['duration_sec']:.1f}s")

    return stats


async def run_continuous(interval_minutes: int, dry_run: bool = False):
    """
    连续监控模式。

    定时运行 pipeline，仅在交易日和交易时段运行。
    """
    logger.info(f"🔄 启动连续监控模式，每 {interval_minutes} 分钟轮询一次")

    while True:
        # 检查是否为交易日
        if is_trade_day():
            now = datetime.now()
            hour = now.hour

            # 9:00 - 15:30 为交易时段（含盘前盘后处理时间）
            if 9 <= hour < 16:
                logger.info(f"⏰ 交易时段，执行扫描...")
                stats = await run_pipeline(dry_run=dry_run)
                if stats["errors"]:
                    logger.warning(f"⚠️ 本次扫描有 {len(stats['errors'])} 个错误")
            else:
                logger.info(f"💤 非交易时段 ({hour}:00)，跳过扫描")
        else:
            logger.info(f"💤 非交易日，跳过扫描")

        # 等待下一个间隔（每分钟检查一次，但只打印一次状态）
        next_run = interval_minutes * 60
        logger.info(f"⏳ 下次扫描在 {next_run//60} 分钟后...")
        await asyncio.sleep(next_run)


def main():
    parser = argparse.ArgumentParser(description="可转债强赎博弈策略监控")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    parser.add_argument(
        "--interval", type=int, default=0,
        help=f"连续模式运行间隔（分钟），默认单次运行"
    )
    parser.add_argument(
        "--once", action="store_true", default=True,
        help="单次运行模式（默认）"
    )
    args = parser.parse_args()

    if args.interval:
        asyncio.run(run_continuous(args.interval, dry_run=args.dry_run))
    else:
        stats = asyncio.run(run_pipeline(dry_run=args.dry_run))
        print(f"\n📊 运行统计: 扫描 {stats['scanned']} 只, "
              f"预警 {len(stats['alerts'])} 只, "
              f"耗时 {stats['duration_sec']:.1f}s, "
              f"错误 {len(stats['errors'])} 个")


if __name__ == "__main__":
    main()
