"""
可转债文本数据采集器 — 多源统一收储

源:
  1. 东方财富 (eastmoney) — 搜索 API, 按关键词/股票码查询
  2. 新浪财经 (sina)      — 滚动新闻 API + 7x24 直播
  3. 证券时报 (stcn)       — RSS feeds
  4. 雪球 (xueqiu)        — 投资者社区搜索 API (需 cookie)

输出: Parquet 到 DATA_DIR / cb_news.parquet

用法:
  python scripts/fetch_cb_news.py                        # 全量采集（默认）
  python scripts/fetch_cb_news.py --source eastmoney      # 仅东财
  python scripts/fetch_cb_news.py --source sina           # 仅新浪
  python scripts/fetch_cb_news.py --source stcn           # 仅证券时报
  python scripts/fetch_cb_news.py --keyword 可转债强赎     # 指定关键词
  python scripts/fetch_cb_news.py --days 7                # 最近 N 天
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import pandas as pd
import requests

# ── 路径 ──────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = DATA_DIR / "cb_news.parquet"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── HTTP Session ──────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
})

# ══════════════════════════════════════════════════════════
# 1. 东方财富 — 搜索 API
# ══════════════════════════════════════════════════════════

EASTMONEY_SEARCH_URL = "http://search-api-web.eastmoney.com/search/jsonp"

# 默认搜索关键词（可转债相关）
DEFAULT_EM_KEYWORDS = [
    "可转债",
    "可转债 强赎",
    "可转债 下修",
    "可转债 回售",
    "转债 赎回",
    "转债 下修",
    "转债 强赎",
    "可转债 不赎回",
    "可转债 触发",
    "转债 回售",
    "可转债 公告",
    "可转债 提前赎回",
]


def _clean_html(text: str) -> str:
    """去掉 HTML 标签和高亮标记。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_eastmoney_news(
    keywords: list[str] | None = None,
    page_size: int = 100,
    max_pages: int = 15,
    sleep_sec: float = 0.5,
) -> pd.DataFrame:
    """
    从东方财富搜索 API 采集新闻。

    Args:
        keywords: 搜索关键词列表，默认用可转债相关词
        page_size: 每页条数 (max 100)
        max_pages: 最多翻页数
        sleep_sec: 请求间隔

    Returns:
        DataFrame with columns: source, fetch_time, pub_time, keyword,
                                title, content, url, media
    """
    keywords = keywords or DEFAULT_EM_KEYWORDS
    all_rows = []
    seen_urls = set()

    for kw in keywords:
        log.info(f"🔍 东方财富搜索: '{kw}'")
        for page in range(1, max_pages + 1):
            param = json.dumps({
                "uid": "",
                "keyword": kw,
                "type": ["cmsArticleWebOld"],
                "client": "web",
                "clientType": "web",
                "clientVersion": "curr",
                "param": {
                    "cmsArticleWebOld": {
                        "searchScope": "default",
                        "sort": "default",
                        "pageIndex": page,
                        "pageSize": page_size,
                        "preTag": "",
                        "postTag": "",
                    }
                },
            })
            params = {"cb": "cb", "param": param}

            try:
                resp = SESSION.get(EASTMONEY_SEARCH_URL, params=params, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                log.warning(f"  ⚠️ 请求失败 (page={page}): {e}")
                break

            # 解析 JSONP: cb({...})
            raw = resp.text
            match = re.search(r"cb\((.*)\)", raw, re.DOTALL)
            if not match:
                log.warning(f"  ⚠️ JSONP 解析失败 (page={page})")
                break

            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                log.warning(f"  ⚠️ JSON 解析失败 (page={page})")
                break

            articles = data.get("result", {}).get("cmsArticleWebOld", [])
            if not articles:
                log.info(f"  └─ 无更多结果 (page={page})")
                break

            new_count = 0
            for art in articles:
                url = art.get("url", "")
                if not url:
                    code = art.get("code", "")
                    if code:
                        url = f"http://finance.eastmoney.com/a/{code}.html"
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                all_rows.append({
                    "source": "eastmoney",
                    "fetch_time": datetime.now().isoformat(),
                    "pub_time": art.get("date", ""),
                    "keyword": kw,
                    "title": _clean_html(art.get("title", "")),
                    "content": _clean_html(art.get("content", "")),
                    "url": url,
                    "media": art.get("mediaName", ""),
                })
                new_count += 1

            log.info(f"  └─ page={page}: {new_count} 条新文章")
            time.sleep(sleep_sec)

    df = pd.DataFrame(all_rows)
    log.info(f"✅ 东方财富: 共 {len(df)} 条")
    return df


# ══════════════════════════════════════════════════════════
# 2. 新浪财经 — 滚动新闻 API
# ══════════════════════════════════════════════════════════

SINA_ROLL_URL = "https://roll.finance.sina.com.cn/api/news_list.php"
SINA_ZB_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"

# 新浪财经频道 cat_1 映射（仍在更新的）
SINA_CHANNELS = {
    "finance": {"tag": "2", "cat_1": "finance", "cat_2": ""},
}

# 直播 feed tag_id（财经相关）
SINA_ZB_TAGS = {
    "all": 0,
    "stock": 7,  # 股市
}


def fetch_sina_news(
    channels: list[str] | None = None,
    page_size: int = 50,
    max_pages: int = 3,
    sleep_sec: float = 0.8,
) -> pd.DataFrame:
    """
    从新浪财经滚动新闻 API 采集。

    NOTE: 新浪接口不稳定，部分频道已停更。优先用 7x24 直播。

    Args:
        channels: 频道列表，默认 ['finance']
        page_size: 每页条数
        max_pages: 翻页数
        sleep_sec: 请求间隔（新浪封 IP 敏感，建议 0.8s+）

    Returns:
        DataFrame
    """
    channels = channels or list(SINA_CHANNELS.keys())
    all_rows = []
    seen_urls = set()

    for ch in channels:
        ch_cfg = SINA_CHANNELS.get(ch, {})
        log.info(f"🔍 新浪财经滚动: {ch}")

        for page in range(1, max_pages + 1):
            params = {
                "tag": ch_cfg.get("tag", "2"),
                "cat_1": ch_cfg.get("cat_1", "finance"),
                "cat_2": ch_cfg.get("cat_2", ""),
                "page": page,
                "page_size": page_size,
                "_": int(time.time() * 1000),
            }

            try:
                resp = SESSION.get(SINA_ROLL_URL, params=params, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                log.warning(f"  ⚠️ 请求失败 (page={page}): {e}")
                break

            # 新浪返回 JSONP: var jsonData = {...}
            raw = resp.text
            match = re.search(r"var jsonData\s*=\s*({.*})", raw, re.DOTALL)
            if not match:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                log.warning(f"  ⚠️ 新浪 JSON 解析失败 (page={page})")
                break

            try:
                data = json.loads(match.group(1) if match.lastindex else match.group(0))
            except json.JSONDecodeError:
                log.warning(f"  ⚠️ JSON 解析失败 (page={page})")
                break

            articles = data.get("list", [])
            if not articles:
                log.info(f"  └─ 无更多结果 (page={page})")
                break

            new_count = 0
            for art in articles:
                url = art.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                # 时间戳转换
                ts = art.get("time", 0)
                try:
                    pub_time = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OSError):
                    pub_time = str(ts)

                all_rows.append({
                    "source": "sina",
                    "fetch_time": datetime.now().isoformat(),
                    "pub_time": pub_time,
                    "keyword": ch,
                    "title": _clean_html(art.get("title", "")),
                    "content": _clean_html(art.get("intro", art.get("summary", ""))),
                    "url": url,
                    "media": art.get("media", ""),
                })
                new_count += 1

            log.info(f"  └─ page={page}: {new_count} 条")
            time.sleep(sleep_sec)

    df = pd.DataFrame(all_rows)
    log.info(f"✅ 新浪财经: 共 {len(df)} 条")
    return df


def fetch_sina_zhibo(
    tag_id: int = 0,
    page_size: int = 20,
    max_pages: int = 5,
    sleep_sec: float = 0.8,
) -> pd.DataFrame:
    """
    从新浪 7x24 直播 feed 采集。

    Args:
        tag_id: 0=全部, 7=股市
        page_size: 每页条数 (max ~20)
        max_pages: 翻页数
        sleep_sec: 请求间隔

    Returns:
        DataFrame
    """
    all_rows = []
    seen_ids = set()
    log.info(f"🔍 新浪 7x24 直播 (tag={tag_id})")

    for page in range(1, max_pages + 1):
        params = {
            "callback": "cb",
            "page": page,
            "page_size": page_size,
            "zhibo_id": 152,
            "tag_id": tag_id,
            "dire": "f",
            "dpc": 1,
            "_": int(time.time() * 1000),
        }

        try:
            resp = SESSION.get(SINA_ZB_URL, params=params, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  ⚠️ 请求失败 (page={page}): {e}")
            break

        raw = resp.text
        match = re.search(r"cb\((.*)\)", raw, re.DOTALL)
        if not match:
            log.warning(f"  ⚠️ JSONP 解析失败 (page={page})")
            break

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.warning(f"  ⚠️ JSON 解析失败 (page={page})")
            break

        items = data.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
        if not items:
            log.info(f"  └─ 无更多结果 (page={page})")
            break

        new_count = 0
        for item in items:
            item_id = item.get("id", "")
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            all_rows.append({
                "source": "sina",
                "fetch_time": datetime.now().isoformat(),
                "pub_time": item.get("update_time", ""),
                "keyword": f"zhibo_tag{tag_id}",
                "title": "",  # 直播快讯无标题
                "content": _clean_html(item.get("rich_text", "")),
                "url": f"https://zhibo.sina.com.cn/news/{item_id}" if item_id else "",
                "media": "新浪7x24",
            })
            new_count += 1

        log.info(f"  └─ page={page}: {new_count} 条快讯")
        time.sleep(sleep_sec)

    df = pd.DataFrame(all_rows)
    log.info(f"✅ 新浪7x24: 共 {len(df)} 条")
    return df


# ══════════════════════════════════════════════════════════
# 3. 证券时报 — RSS feeds
# ══════════════════════════════════════════════════════════

STCN_RSS_URL = "https://app.stcn.com/rss.php"

# 最可能活跃的 catid
STCN_CATIDS = {
    "kuaixun": 29,     # 快讯
    "stock": 17,       # 股票
    "rolling": 340,    # 滚动新闻
    "stock_info": 41,  # 股票情报
}


def fetch_stcn_news(
    catids: list[int] | None = None,
    sleep_sec: float = 1.0,
) -> pd.DataFrame:
    """
    从证券时报 RSS feeds 采集。

    Args:
        catids: RSS 分类 ID，默认用快讯+股票+滚动
        sleep_sec: 请求间隔

    Returns:
        DataFrame
    """
    import xml.etree.ElementTree as ET

    catids = catids or list(STCN_CATIDS.values())
    all_rows = []
    seen_urls = set()

    for catid in catids:
        name = [k for k, v in STCN_CATIDS.items() if v == catid]
        name = name[0] if name else str(catid)
        url = f"{STCN_RSS_URL}?catid={catid}"
        log.info(f"🔍 证券时报 RSS: {name} (catid={catid})")

        try:
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  ⚠️ RSS 请求失败: {e}")
            continue

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            log.warning(f"  ⚠️ XML 解析失败 (catid={catid}): {e}")
            continue

        new_count = 0
        for item in root.iter("item"):
            link = ""
            title = ""
            pub_date = ""

            for child in item:
                tag = child.tag.lower()
                text = (child.text or "").strip()
                if tag == "link":
                    link = text
                elif tag == "title":
                    title = text
                elif tag in ("pubdate", "pub_date", "dc:date"):
                    pub_date = text

            if not link or link in seen_urls:
                continue
            seen_urls.add(link)

            all_rows.append({
                "source": "stcn",
                "fetch_time": datetime.now().isoformat(),
                "pub_time": pub_date,
                "keyword": f"catid_{catid}",
                "title": _clean_html(title),
                "content": "",  # RSS 一般不提供全文
                "url": link,
                "media": "证券时报",
            })
            new_count += 1

        log.info(f"  └─ catid={catid}: {new_count} 条")
        time.sleep(sleep_sec)

    df = pd.DataFrame(all_rows)
    log.info(f"✅ 证券时报: 共 {len(df)} 条")
    return df


# ══════════════════════════════════════════════════════════
# 4. 雪球 — 投资者社区搜索
# ══════════════════════════════════════════════════════════

XUEQIU_SEARCH_URL = "https://xueqiu.com/statuses/search.json"
XUEQIU_HOME = "https://xueqiu.com/"

# 雪球 cookie（需登录后获取 xq_a_token）
XUEQIU_COOKIE = os.environ.get(
    "XUEQIU_COOKIE",
    "5425189CC3BB0F8D867FA3CE664EA84F:FG=1"
)

# 雪球搜索关键词
DEFAULT_XQ_KEYWORDS = [
    "可转债 强赎",
    "可转债 下修",
    "可转债 回售",
    "转债 赎回",
]


def fetch_xueqiu_news(
    keywords: list[str] | None = None,
    page_size: int = 20,
    max_pages: int = 5,
    sleep_sec: float = 1.0,
) -> pd.DataFrame:
    """
    从雪球投资者社区搜索 API 采集。

    需要有效的 cookie (xq_a_token) 才能访问。
    先用 session 访问首页获取 WAF cookie，再带用户 cookie 搜。

    Args:
        keywords: 搜索关键词
        page_size: 每页条数
        max_pages: 翻页数（雪球限 ~100 页）
        sleep_sec: 请求间隔（雪球限频敏感，建议 1s+）

    Returns:
        DataFrame
    """
    keywords = keywords or DEFAULT_XQ_KEYWORDS

    # 创建独立 session（不和东财/新浪混用）
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Cookie": XUEQIU_COOKIE,
        "Referer": "https://xueqiu.com/",
    })

    # Step 1: 访问首页获取 WAF cookie (acw_tc)
    try:
        session.get(XUEQIU_HOME, timeout=15)
        time.sleep(0.5)
    except Exception as e:
        log.warning(f"  ⚠️ 雪球首页访问失败: {e}")

    all_rows = []
    seen_ids = set()

    for kw in keywords:
        log.info(f"🔍 雪球搜索: '{kw}'")
        for page in range(1, max_pages + 1):
            params = {
                "q": kw,
                "count": page_size,
                "page": page,
                "sort": "time",
                "source": "all",
            }

            try:
                resp = session.get(XUEQIU_SEARCH_URL, params=params, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                log.warning(f"  ⚠️ 雪球请求失败 (page={page}): {e}")
                break

            # 检查是否被反爬
            if "error_description" in resp.text or "error_code" in resp.text:
                log.warning(f"  ⚠️ 雪球返回错误 (page={page}): {resp.text[:200]}")
                break

            try:
                data = resp.json()
            except json.JSONDecodeError:
                log.warning(f"  ⚠️ JSON 解析失败 (page={page})")
                break

            posts = data.get("list", [])
            if not posts:
                log.info(f"  └─ 无更多结果 (page={page})")
                break

            new_count = 0
            for post in posts:
                post_id = str(post.get("id", ""))
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)

                # 提取数据
                title = _clean_html(post.get("title", post.get("text", "")))
                # 截取标题前80字
                title = title[:80] if len(title) > 80 else title
                description = _clean_html(post.get("description", post.get("text", "")))

                # 发布时间
                created_at = post.get("created_at", 0)
                try:
                    pub_time = datetime.fromtimestamp(
                        int(created_at) / 1000
                    ).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, OSError):
                    pub_time = ""

                # URL
                url = f"https://xueqiu.com{post.get('target', '')}" if post.get("target") else ""

                all_rows.append({
                    "source": "xueqiu",
                    "fetch_time": datetime.now().isoformat(),
                    "pub_time": pub_time,
                    "keyword": kw,
                    "title": title,
                    "content": description[:500] if description else "",
                    "url": url,
                    "media": "雪球",
                })
                new_count += 1

            log.info(f"  └─ page={page}: {new_count} 条")
            time.sleep(sleep_sec)

    df = pd.DataFrame(all_rows)
    log.info(f"✅ 雪球: 共 {len(df)} 条")
    return df


# ══════════════════════════════════════════════════════════
# 5. 合并 & 去重 & 持久化
# ══════════════════════════════════════════════════════════

SCHEMA_COLUMNS = [
    "source", "fetch_time", "pub_time", "keyword",
    "title", "content", "url", "media",
]


def load_existing() -> pd.DataFrame:
    """加载已有数据。"""
    if OUTPUT_FILE.exists():
        return pd.read_parquet(OUTPUT_FILE)
    return pd.DataFrame(columns=SCHEMA_COLUMNS)


def merge_and_save(new_df: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    """按 url 去重合并并保存。"""
    if new_df.empty:
        log.info("⚠️ 无新数据，跳过保存")
        return existing

    # 规范化列
    for col in SCHEMA_COLUMNS:
        if col not in new_df.columns:
            new_df[col] = ""

    new_df = new_df[SCHEMA_COLUMNS].copy()

    if existing.empty:
        combined = new_df
    else:
        existing_urls = set(existing["url"].dropna())
        truly_new = new_df[~new_df["url"].isin(existing_urls)]
        log.info(f"📊 新增 {len(truly_new)} / 总计 {len(new_df)} 条（去重后）")
        combined = pd.concat([existing, truly_new], ignore_index=True)

    combined = combined.drop_duplicates(subset=["url"], keep="first")
    combined = combined.sort_values("pub_time", ascending=False).reset_index(drop=True)
    combined.to_parquet(OUTPUT_FILE, index=False)
    log.info(f"💾 已保存: {OUTPUT_FILE} ({len(combined)} 行)")
    return combined


# ══════════════════════════════════════════════════════════
# 5. CLI 入口
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="可转债文本数据采集器")
    parser.add_argument(
        "--source", choices=["eastmoney", "sina", "stcn", "xueqiu", "all"],
        default="all", help="数据源 (default: all)"
    )
    parser.add_argument(
        "--keyword", nargs="*", default=None,
        help="东方财富搜索关键词（覆盖默认）"
    )
    parser.add_argument(
        "--pages", type=int, default=15,
        help="每源最大翻页数 (default: 15)"
    )
    parser.add_argument(
        "--sleep", type=float, default=0.5,
        help="请求间隔秒数 (default: 0.5)"
    )
    args = parser.parse_args()

    existing = load_existing()
    log.info(f"📦 已有数据: {len(existing)} 条")

    dfs: list[pd.DataFrame] = []

    if args.source in ("eastmoney", "all"):
        kw = args.keyword if args.keyword else None
        df_em = fetch_eastmoney_news(
            keywords=kw, max_pages=args.pages, sleep_sec=args.sleep
        )
        dfs.append(df_em)

    if args.source in ("sina", "all"):
        # 新浪滚动 + 7x24 直播双路
        df_sina_roll = fetch_sina_news(
            max_pages=args.pages, sleep_sec=max(args.sleep, 0.8)
        )
        df_sina_zb = fetch_sina_zhibo(
            tag_id=0, max_pages=args.pages, sleep_sec=max(args.sleep, 0.8)
        )
        dfs.extend([df_sina_roll, df_sina_zb])

    if args.source in ("stcn", "all"):
        df_stcn = fetch_stcn_news(sleep_sec=max(args.sleep, 1.0))
        dfs.append(df_stcn)

    if args.source in ("xueqiu", "all"):
        df_xq = fetch_xueqiu_news(
            max_pages=args.pages, sleep_sec=max(args.sleep, 1.0)
        )
        dfs.append(df_xq)

    # 合并 & 保存
    all_new = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
    merge_and_save(all_new, existing)


if __name__ == "__main__":
    main()
