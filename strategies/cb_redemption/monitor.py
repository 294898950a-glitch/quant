"""
巨潮资讯公告采集器 (CNINFO Monitor)

从巨潮资讯网 (http://www.cninfo.com.cn) 采集可转债相关公告，
按关键词分类为：redemption（赎回）、revision（下修）、putback（回售）、other（其他）。

依赖：
- requests
- config.py（策略配置，含 KEYWORD_MAP、CNINFO_HEADERS、CNINFO_QUERY_URL）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from strategies.cb_redemption.config import CNINFO_HEADERS, CNINFO_QUERY_URL, KEYWORD_MAP

logger = logging.getLogger(__name__)

# 北京时间（UTC+8）
BJT = timezone(timedelta(hours=8))

# =============================================================================
# 核心函数
# =============================================================================


def fetch_cninfo_announcements(
    keywords: list[str],
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """从巨潮资讯网查询公告，返回标题包含任一*keywords*的公告列表。

    流程：
        1. GET http://www.cninfo.com.cn/ 建立会话并获取 Cookie。
        2. POST /new/hisAnnouncement/query 查询深市（orgId=gssz0003001）定期报告公告。
        3. 根据标题过滤：只要标题包含 keywords 中任意一个关键词则保留。

    参数
    ----------
    keywords : list[str]
        用于过滤标题的关键词列表（如 ["赎回", "下修", "回售"]）。
    page_size : int
        每页查询数量，默认 20。

    返回
    -------
    list[dict]
        每项为单条公告字典，结构如：
        {
            "id": "…",
            "announcementTitle": "…",
            "announcementTime": 1700000000000,   # Unix 毫秒
            "secCode": "…",
            "secName": "…",
            ...
        }
        异常时返回空列表。
    """
    try:
        session = requests.Session()
        session.headers.update(CNINFO_HEADERS)

        # 1. 先访问首页获取 Cookie
        resp = session.get(
            "http://www.cninfo.com.cn/",
            timeout=10,
        )
        resp.raise_for_status()

        # 2. 发起查询 POST
        payload: dict[str, Any] = {
            "orgId": "gssz0003001",
            "category": "category_ndbg_szsh;",
            "pageNum": 1,
            "pageSize": page_size,
        }
        resp = session.post(
            CNINFO_QUERY_URL,
            data=payload,
            timeout=10,
        )
        resp.raise_for_status()

        data = resp.json()
        total_announcements = data.get("totalAnnouncement", []) or []
        announcements = data.get("announcements", [])

        # 有的接口返回结构是 {"announcements": [...]}, 也有可能是 {"totalAnnouncement": [...]}
        items: list[dict] = total_announcements or announcements or []

        # 3. 按关键词过滤标题
        result: list[dict[str, Any]] = []
        for item in items:
            title: str = (item.get("announcementTitle") or "").strip()
            if any(kw in title for kw in keywords):
                result.append(item)

        return result

    except requests.RequestException as exc:
        logger.error("巨潮资讯请求失败: %s", exc)
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("巨潮资讯响应解析失败: %s", exc)

    return []


def classify_announcement(title: str) -> str:
    """根据标题将公告分类。

    - redemption（赎回/强赎/不赎回）
    - revision（下修/转股价格修正）
    - putback（回售）
    - other（以上皆否）

    检查顺序：redemption → revision → putback → other。
    """
    for category, kw_list in KEYWORD_MAP.items():
        for kw in kw_list:
            if kw in title:
                return category
    return "other"


def monitor_recent_announcements(minutes_back: int = 30) -> list[dict[str, Any]]:
    """获取最近 *minutes_back* 分钟内发布的公告，自动分类。

    参数
    ----------
    minutes_back : int
        回溯分钟数，默认 30。

    返回
    -------
    list[dict]
        每项公告已额外附加 'category' 字段，结构如：
        {
            "id": "…",
            "announcementTitle": "…",
            "announcementTime": 1700000000000,
            "secCode": "…",
            "secName": "…",
            "category": "redemption",   # ← 分类结果
            ...
        }
        异常时返回空列表。
    """
    # 收集所有分类关键词
    all_keywords: list[str] = []
    for kw_list in KEYWORD_MAP.values():
        all_keywords.extend(kw_list)

    announcements = fetch_cninfo_announcements(keywords=all_keywords, page_size=50)

    # 时间阈值（北京时间），公告时间在 minutes_back 分钟内
    threshold_ts = datetime.now(BJT) - timedelta(minutes=minutes_back)
    threshold_ms = int(threshold_ts.timestamp() * 1000)

    result: list[dict[str, Any]] = []
    for ann in announcements:
        ann_time_ms = ann.get("announcementTime", 0)
        if ann_time_ms >= threshold_ms:
            title: str = (ann.get("announcementTitle") or "").strip()
            ann["category"] = classify_announcement(title)
            result.append(ann)

    return result
