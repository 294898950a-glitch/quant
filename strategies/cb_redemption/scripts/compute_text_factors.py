"""
文本因子计算 — 将 cb_news.parquet 转换为强赎策略可用特征

产出两类因子:
  1. 市场级（所有券共享）: news_volume, redemption_heat, revision_heat, source_quality
  2. 个券级（仅含转债名/代码的文章）: per_bond_mentions

输出: cb_news_factors.parquet (date + 市场因子)
       cb_news_bond_mentions.parquet (date + ts_code + 个券提及次数)

用途:
  build_historical_snapshots() 中 merge_asof 按日期合并市场因子
  个券因子按 ts_code + date 合并
"""

import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── 关键词规则 ──────────────────────────────────────────
REDEMPTION_KW = ["强赎", "赎回", "提前赎回", "不赎回", "不行使赎回权"]
REVISION_KW = ["下修", "向下修正", "转股价格修正", "下修到底"]
PUTBACK_KW = ["回售", "有条件回售"]
POSITIVE_KW = ["利好", "大涨", "飙升", "涨停", "增持", "买入", "看好", "机会"]
NEGATIVE_KW = ["利空", "大跌", "暴跌", "跌停", "减持", "风险", "警惕", "亏损"]

# 四大报 + 高权重媒体
HIGH_QUALITY_MEDIA = [
    "证券日报", "上海证券报", "中国证券报", "证券时报",
    "证券时报网", "上海证券报·中国证券网", "中国证券报·中证网",
    "财联社", "21世纪经济报道", "第一财经", "每日经济新闻",
    "界面新闻", "新华财经",
]

# 转债简称模式: XX转债
BOND_NAME_PATTERN = re.compile(r"([\u4e00-\u9fa5]{2,6})转债")

# ── 加载 ────────────────────────────────────────────────


def load_news() -> pd.DataFrame:
    """加载文本数据。"""
    fpath = DATA_DIR / "cb_news.parquet"
    if not fpath.exists():
        raise FileNotFoundError(f"新闻数据不存在: {fpath}，请先运行 fetch_cb_news.py")
    df = pd.read_parquet(fpath)
    df["pub_date"] = pd.to_datetime(df["pub_time"]).dt.strftime("%Y%m%d")
    df["title_lower"] = df["title"].fillna("").str.lower()
    df["content_lower"] = df["content"].fillna("").str.lower()
    return df


def load_bond_map() -> dict[str, list[str]]:
    """
    构建转债简称 → ts_code 映射。

    从 cb_basic.parquet 读取所有转债的简称和代码。
    注意: 简称可能有重复（如同一公司多次发债: XX转债、XX转02）
          同名简称映射到所有可能的 ts_code。
    """
    warehouse = Path.home() / "projects" / "quant" / "data" / "cb_warehouse"
    basic_path = warehouse / "cb_basic.parquet"
    if not basic_path.exists():
        log.warning(f"⚠️ cb_basic.parquet 不存在，个券映射跳过")
        return {}

    basic = pd.read_parquet(basic_path)
    name_map: dict[str, list[str]] = {}
    for _, row in basic.iterrows():
        name = str(row.get("bond_short_name", ""))
        code = str(row.get("ts_code", ""))
        if name and code:
            name_map.setdefault(name, []).append(code)
    log.info(f"📋 转债简称映射: {len(name_map)} 个简称 → {len(basic)} 只转债")
    return name_map


# ── 市场级因子 ──────────────────────────────────────────


def compute_market_factors(news: pd.DataFrame) -> pd.DataFrame:
    """
    按日聚合市场级文本因子。

    Returns:
        DataFrame with columns: date, news_volume, redemption_articles,
        revision_articles, putback_articles, positive_ratio, negative_ratio,
        high_quality_ratio, avg_daily_volume_5d, redemption_heat_5d
    """
    df = news.copy()

    # 每日统计
    daily = df.groupby("pub_date").agg(
        news_volume=("url", "count"),
        redemption_articles=("title_lower", lambda x: sum(
            any(kw in t for kw in REDEMPTION_KW) for t in x
        )),
        revision_articles=("title_lower", lambda x: sum(
            any(kw in t for kw in REVISION_KW) for t in x
        )),
        putback_articles=("title_lower", lambda x: sum(
            any(kw in t for kw in PUTBACK_KW) for t in x
        )),
        high_quality_count=("media", lambda x: sum(
            m in HIGH_QUALITY_MEDIA for m in x
        )),
    ).reset_index()

    # 衍生比率
    daily["redemption_ratio"] = daily["redemption_articles"] / daily["news_volume"].clip(lower=1)
    daily["revision_ratio"] = daily["revision_articles"] / daily["news_volume"].clip(lower=1)
    daily["high_quality_ratio"] = daily["high_quality_count"] / daily["news_volume"].clip(lower=1)

    # 5日均值（市场热度趋势）
    daily = daily.sort_values("pub_date")
    daily["news_volume_5d"] = daily["news_volume"].rolling(5, min_periods=1).mean()
    daily["redemption_heat_5d"] = daily["redemption_articles"].rolling(5, min_periods=1).sum()
    daily["revision_heat_5d"] = daily["revision_articles"].rolling(5, min_periods=1).sum()

    # 情绪：简单关键词计数（粗糙但快）
    def _count_kw(text_series, kw_list):
        return sum(
            any(kw in str(t) for kw in kw_list)
            for t in text_series
        )

    daily_pos = df.groupby("pub_date").apply(
        lambda g: pd.Series({
            "positive_count": _count_kw(g["title_lower"], POSITIVE_KW),
            "negative_count": _count_kw(g["title_lower"], NEGATIVE_KW),
        })
    ).reset_index()

    daily = daily.merge(daily_pos, on="pub_date", how="left")
    total = daily["positive_count"].fillna(0) + daily["negative_count"].fillna(0)
    daily["sentiment_score"] = np.where(
        total > 0,
        (daily["positive_count"].fillna(0) - daily["negative_count"].fillna(0)) / total,
        0.0,
    )

    # 排序和清理
    daily["date"] = daily["pub_date"].astype(int)
    keep_cols = [
        "date", "news_volume", "redemption_articles", "revision_articles",
        "redemption_ratio", "revision_ratio", "high_quality_ratio",
        "news_volume_5d", "redemption_heat_5d", "revision_heat_5d",
        "sentiment_score",
    ]
    daily = daily[keep_cols].sort_values("date").reset_index(drop=True)

    log.info(f"✅ 市场级因子: {len(daily)} 个交易日, 范围 {daily['date'].min()}~{daily['date'].max()}")
    return daily


# ── 个券级因子 ──────────────────────────────────────────


def compute_bond_mentions(
    news: pd.DataFrame,
    bond_map: dict[str, list[str]],
) -> pd.DataFrame:
    """
    按日期 + 转债映射，统计每只转债被提及的次数。

    从标题中提取「XX转债」简称，映射到 ts_code。
    一条文章可能匹配多只转债（如「A转债和B转债对比」）。

    Returns:
        DataFrame with columns: date, ts_code, mention_count
    """
    rows = []

    for _, article in news.iterrows():
        title = str(article.get("title", ""))
        pub_date = article.get("pub_date", "")
        if not pub_date:
            continue

        # 提取「XX转债」
        matches = BOND_NAME_PATTERN.findall(title)
        if not matches:
            continue

        matched_codes: set[str] = set()
        for m in matches:
            full_name = f"{m}转债"
            codes = bond_map.get(full_name, [])
            for c in codes:
                matched_codes.add(c)

        for code in matched_codes:
            rows.append({
                "date": int(pub_date),
                "ts_code": code,
                "mention_count": 1,
            })

    if not rows:
        log.warning("⚠️ 无个券提及匹配")
        return pd.DataFrame(columns=["date", "ts_code", "mention_count"])

    df = pd.DataFrame(rows)
    # 按日汇总
    daily = df.groupby(["date", "ts_code"]).agg(
        mention_count=("mention_count", "sum")
    ).reset_index()

    log.info(f"✅ 个券提及: {len(daily)} 条, {daily['ts_code'].nunique()} 只转债")
    return daily


# ── 主流程 ──────────────────────────────────────────────


def main():
    log.info("=" * 60)
    log.info("📰 文本因子计算")
    log.info("=" * 60)

    # 1. 加载数据
    news = load_news()
    log.info(f"📦 加载新闻: {len(news)} 条")
    log.info(f"   日期范围: {news['pub_date'].min()} ~ {news['pub_date'].max()}")

    # 2. 市场级因子
    market_factors = compute_market_factors(news)
    market_path = DATA_DIR / "cb_news_factors.parquet"
    market_factors.to_parquet(market_path, index=False)
    log.info(f"💾 市场因子: {market_path} ({len(market_factors)} 天)")

    # 3. 个券级因子
    bond_map = load_bond_map()
    if bond_map:
        bond_mentions = compute_bond_mentions(news, bond_map)
        mentions_path = DATA_DIR / "cb_news_bond_mentions.parquet"
        bond_mentions.to_parquet(mentions_path, index=False)
        log.info(f"💾 个券提及: {mentions_path} ({len(bond_mentions)} 条)")

    # 4. 摘要
    log.info("=" * 60)
    log.info("📊 因子摘要:")
    log.info(f"   市场因子: {len(market_factors.columns)} 维 × {len(market_factors)} 天")
    log.info(f"   最新 5 天 news_volume_5d: {market_factors['news_volume_5d'].tail().tolist()}")
    if bond_map:
        top_mentioned = bond_mentions.groupby("ts_code")["mention_count"].sum().nlargest(5)
        log.info(f"   最受关注转债: {top_mentioned.to_dict()}")


if __name__ == "__main__":
    main()
