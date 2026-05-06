"""
Telegram 推送引擎 — 将强赎监控结果推送到 Telegram。

通过 Hermes 网关的 httpx 直接调用 Telegram Bot API。
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from strategies.cb_redemption.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# Telegram Bot API 端点
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    发送 Telegram 消息。

    Args:
        text: 消息内容
        parse_mode: 解析模式 (HTML / MarkdownV2)

    Returns:
        bool: 发送成功与否
    """
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN 未配置，消息不会发送")
        print(f"[推送预览] 以下消息未发送（Token 未配置）:\n{text[:200]}...")
        return False

    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"✅ 消息已推送到 Telegram (chat_id={TELEGRAM_CHAT_ID})")
        return True
    except Exception as e:
        logger.error(f"❌ Telegram 推送失败: {e}")
        return False


def send_redemption_alert(
    alerts: list[dict],
    num_total: int,
    num_scanned: int,
) -> bool:
    """
    发送强赎监控预警消息。

    Args:
        alerts: 按信号等级排序的预警列表
        num_total: 全市场转债总数
        num_scanned: 本次扫描总数

    每个 alert 的格式:
        {
            "cb_code": "113654",
            "cb_name": "永02转债",
            "signal_level": "🚨 行动",
            "probability": 0.92,
            "conversion_premium": 2.5,
            "trigger_progress": 1.0,
            "trigger_days_count": 15,
            "status_label": "active_redeeming",
            "remaining_balance": 3.041,
            "last_trade_date": "2026-05-07",
        }
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"🔥 <b>可转债强赎监控</b> | {now}",
        f"扫描 {num_scanned}/{num_total} 只转债，预警 {len(alerts)} 只",
        "",
    ]

    # 按等级分组
    levels = ["🚨 行动", "🔴 警惕", "🟠 预警"]
    level_labels = {"🚨 行动": "行动级 (≥85%)", "🔴 警惕": "警惕级 (60-85%)", "🟠 预警": "预警级 (30-60%)"}

    for level in levels:
        level_alerts = [a for a in alerts if a.get("signal_level") == level or level in a.get("signal_level", "")]
        if not level_alerts:
            continue

        lines.append(f"<b>{level_labels.get(level, level)}</b>")
        for a in level_alerts:
            # 筛选关键字段
            cb_name = a.get("cb_name", "?")
            cb_code = a.get("cb_code", "?")
            prob_pct = a.get("probability", 0) * 100
            premium = a.get("conversion_premium", "?")
            progress = a.get("trigger_progress", 0) * 100

            # 构建行
            parts = [f"📌 {cb_name}({cb_code})"]
            parts.append(f"概率 {prob_pct:.0f}%")

            if isinstance(premium, (int, float)):
                parts.append(f"溢价率 {premium:.1f}%")
            if progress > 0:
                parts.append(f"进度 {progress:.0f}%")

            status = a.get("status_label", "")
            if status == "active_redeeming":
                last_date = a.get("last_trade_date", "?")
                parts.append(f"⏰ 最后交易 {last_date}")

            lines.append(" | ".join(parts))

        lines.append("")

    if not alerts:
        lines.append("🟢 无预警，市场平稳")

    message = "\n".join(lines)
    success = _send_message(message)

    # 同时也打印到控制台
    if not success:
        print(message)

    return success


def send_status_report(num_tracked: int, num_alerts: int, errors: list[str]) -> bool:
    """
    发送每日运行状态报告。

    用于定时任务的开始/结束通知。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📊 <b>可转债强赎监控简报</b> | {now}",
        f"追踪 {num_tracked} 只转债，预警 {num_alerts} 只",
    ]

    if errors:
        lines.append("")
        lines.append("⚠️ <b>错误日志:</b>")
        for err in errors[-5:]:
            lines.append(f"  • {err}")

    message = "\n".join(lines)
    return _send_message(message)


def send_error_alert(error_msg: str) -> bool:
    """发送错误告警"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = f"❌ <b>可转债监控异常</b> | {now}\n\n{error_msg}"
    return _send_message(message)
