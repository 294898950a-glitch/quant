"""
雪球文本采集 — Playwright 浏览器自动化

雪球搜索 API 有阿里云 WAF，必须真浏览器执行 JS 才能过。
用 Playwright + 用户 cookie 采集可转债相关讨论帖。

用法:
  python scripts/fetch_xueqiu.py
  python scripts/fetch_xueqiu.py --pages 3
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
OUTPUT_FILE = DATA_DIR / "cb_news.parquet"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── 配置 ─────────────────────────────────────────────────
XQ_KEYWORDS = [
    "可转债 强赎",
    "可转债 下修",
    "可转债 回售",
    "转债 赎回",
]

XQ_COOKIES = {
    "xq_a_token": os.environ.get("XQ_A_TOKEN", "a96fe78cce2beec0ea3ae33faea8ca1e34214176"),
    "xq_r_token": os.environ.get("XQ_R_TOKEN", "e85c57e6291fde67b918378ab111312fbb0a19ff"),
    "u": os.environ.get("XQ_U", "6219494355"),
}


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _find_chromium() -> str:
    """自动找 Chromium 路径。"""
    candidates = sorted(
        Path("/home/jay/.cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"),
        reverse=True,
    )
    if candidates:
        return str(candidates[0])
    # fallback to system chromium
    for cmd in ["chromium", "chromium-browser", "google-chrome-stable"]:
        import shutil
        found = shutil.which(cmd)
        if found:
            return found
    raise FileNotFoundError("未找到 Chromium，请运行: npx playwright install chromium")


async def _fetch_keyword(page, keyword: str, max_pages: int, seen_ids: set) -> list[dict]:
    """搜索单个关键词，滚动翻页采集。"""
    rows = []
    encoded = keyword.replace(" ", "%20")

    for pg in range(1, max_pages + 1):
        url = (
            f"https://xueqiu.com/statuses/search.json"
            f"?q={encoded}&count=20&page={pg}&sort=time&source=all"
        )

        try:
            resp = await page.evaluate(f"""
                fetch('{url}')
                    .then(r => r.json())
            """)
        except Exception as e:
            log.warning(f"  ⚠️ 请求失败 (page={pg}): {e}")
            break

        # 检查错误
        if isinstance(resp, dict) and resp.get("error_code"):
            log.warning(f"  ⚠️ API 错误 (page={pg}): {resp.get('error_description', resp)}")
            break

        posts = resp.get("list", []) if isinstance(resp, dict) else []
        if not posts:
            log.info(f"  └─ 无更多结果 (page={pg})")
            break

        new_count = 0
        for post in posts:
            pid = str(post.get("id", ""))
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            title = _clean_html(post.get("title", "") or post.get("text", ""))[:120]
            desc = _clean_html(post.get("description", "") or post.get("text", ""))[:500]

            created = post.get("created_at", 0)
            try:
                pub_time = datetime.fromtimestamp(int(created) / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (ValueError, OSError):
                pub_time = ""

            url = f"https://xueqiu.com{post.get('target', '')}" if post.get("target") else ""

            rows.append({
                "source": "xueqiu",
                "fetch_time": datetime.now().isoformat(),
                "pub_time": pub_time,
                "keyword": keyword,
                "title": title,
                "content": desc,
                "url": url,
                "media": "雪球",
            })
            new_count += 1

        log.info(f"  └─ page={pg}: {new_count} 条")
        await asyncio.sleep(1.5)

    return rows


async def run(keywords: list[str] | None = None, max_pages: int = 5):
    """主采集流程。"""
    from playwright.async_api import async_playwright

    keywords = keywords or XQ_KEYWORDS
    chrome_path = _find_chromium()
    log.info(f"🔑 关键词: {keywords}")
    log.info(f"📄 每词翻页: {max_pages}")
    log.info(f"🌐 Chrome: {chrome_path}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=chrome_path,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )

        # 注入 cookie
        cookies_to_set = []
        for name, value in XQ_COOKIES.items():
            cookies_to_set.append({
                "name": name,
                "value": value,
                "domain": ".xueqiu.com",
                "path": "/",
            })
        await context.add_cookies(cookies_to_set)

        page = await context.new_page()

        # 先访问首页让 WAF JS 执行
        log.info("🌐 访问雪球首页...")
        await page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        # 逐关键词采集
        all_rows = []
        seen_ids = set()

        for kw in keywords:
            log.info(f"🔍 搜索: '{kw}'")
            rows = await _fetch_keyword(page, kw, max_pages, seen_ids)
            all_rows.extend(rows)
            await asyncio.sleep(2)

        await browser.close()

    df = pd.DataFrame(all_rows)
    log.info(f"✅ 雪球采集完成: {len(df)} 条")

    _merge_save(df)
    return df


def _merge_save(new_df: pd.DataFrame):
    """与现有 cb_news.parquet 合并去重。"""
    schema = ["source", "fetch_time", "pub_time", "keyword", "title", "content", "url", "media"]

    if OUTPUT_FILE.exists():
        existing = pd.read_parquet(OUTPUT_FILE)
    else:
        existing = pd.DataFrame(columns=schema)

    if new_df.empty:
        log.info("⚠️ 无新数据")
        return

    for col in schema:
        if col not in new_df.columns:
            new_df[col] = ""

    new_df = new_df[schema].copy()
    existing_urls = set(existing["url"].dropna())
    truly_new = new_df[~new_df["url"].isin(existing_urls)]
    log.info(f"📊 新增 {len(truly_new)} / 总计 {len(new_df)} 条")

    combined = pd.concat([existing, truly_new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["url"], keep="first")
    combined = combined.sort_values("pub_time", ascending=False).reset_index(drop=True)
    combined.to_parquet(OUTPUT_FILE, index=False)
    log.info(f"💾 已保存: {OUTPUT_FILE} ({len(combined)} 行)")


def main():
    parser = argparse.ArgumentParser(description="雪球可转债讨论采集")
    parser.add_argument("--pages", type=int, default=3, help="每关键词翻页数 (default: 3)")
    parser.add_argument("--keyword", nargs="*", default=None, help="自定义关键词")
    args = parser.parse_args()

    asyncio.run(run(keywords=args.keyword, max_pages=args.pages))


if __name__ == "__main__":
    main()
