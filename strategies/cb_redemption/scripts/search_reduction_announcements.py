#!/usr/bin/env python3
"""
巨潮CNINFO - 大股东减持可转债公告搜刮器

在巨潮上搜"减持"+"可转债"关键词的公告，确认减持事件。

用法:
  python3 scripts/search_reduction_announcements.py
  python3 scripts/search_reduction_announcements.py --stocks 000651 000761
  python3 scripts/search_reduction_announcements.py --days-back 365
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reduction_search")

WAREHOUSE_DIR = Path.home() / "projects" / "quant" / "data" / "cb_warehouse"
STRATEGY_DIR = Path.home() / "projects" / "quant" / "strategies" / "cb_redemption"
OUTPUT_DIR = STRATEGY_DIR / "output"

CNINFO_API = "https://www.cninfo.com.cn"
API_DELAY = 0.3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

# 减持相关关键词
REDUCTION_KEYWORDS = [
    "减持可转债",
    "减持可转换公司债券",
    "减持可转换债券",
    "转让可转债",
    "转让可转换公司债券",
]


def search_reduction_announcements(
    stock_code: str,
    org_id: str,
    days_back: int = 365 * 5,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """搜索某正股的减持可转债公告"""
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
    
    results = []
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    se_date = f"{start_date.strftime('%Y-%m-%d')}~{end_date.strftime('%Y-%m-%d')}"
    
    for keyword in REDUCTION_KEYWORDS:
        for page_num in range(1, 5):  # 最多查4页
            payload = {
                "pageNum": str(page_num),
                "pageSize": "30",
                "column": "szse",
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{stock_code},{org_id}",
                "searchkey": keyword,
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": se_date,
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            
            try:
                time.sleep(API_DELAY)
                resp = session.post(
                    f"{CNINFO_API}/new/hisAnnouncement/query",
                    data=payload,
                    timeout=15,
                )
                data = resp.json()
                anns = data.get("announcements", [])
                for a in anns:
                    title = a.get("announcementTitle", "")
                    if any(kw in title for kw in ["摘要", "英文版"]):
                        continue
                    # 确认是否真正相关
                    if not any(kw in title for kw in REDUCTION_KEYWORDS):
                        continue
                    
                    results.append({
                        "stock_code": stock_code,
                        "announcement_id": str(a["announcementId"]),
                        "title": title,
                        "adjunct_url": a.get("adjunctUrl", ""),
                        "announcement_time": a.get("announcementTime", 0),
                        "search_keyword": keyword,
                    })
                
                total = data.get("totalRecordNum", 0)
                if page_num * 30 >= total:
                    break
                    
            except Exception as e:
                log.warning(f"[{stock_code}] search '{keyword}' page {page_num}: {e}")
                break
    
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="大股东减持可转债公告搜刮器")
    parser.add_argument("--stocks", nargs="+", default=[], help="指定正股代码")
    parser.add_argument("--days-back", type=int, default=365*5, help="向前搜索天数")
    parser.add_argument("--batch", type=int, default=50, help="批次大小")
    parser.add_argument("--dry-run", action="store_true", help="仅统计")
    args = parser.parse_args()
    
    # 加载正股列表
    cb_basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")
    
    if args.stocks:
        stock_list = args.stocks
    else:
        active = cb_basic[cb_basic["remain_size"] > 0].dropna(subset=["stk_code"])
        active["stk_num"] = active["stk_code"].str.extract(r"(\d{6})")
        stock_list = active["stk_num"].unique().tolist()
    
    log.info(f"Stock list: {len(stock_list)} stocks")
    
    if args.dry_run:
        print(json.dumps({"stock_count": len(stock_list), "status": "dry_run"}))
        return
    
    # 获取orgId并搜索
    session = requests.Session()
    session.headers.update(HEADERS)
    
    all_results = []
    failed = 0
    
    for i, stk_num in enumerate(stock_list):
        # 获取orgId
        try:
            time.sleep(API_DELAY)
            resp = session.post(
                f"{CNINFO_API}/new/information/topSearch/query",
                data={"keyWord": stk_num, "maxNum": "1"},
                timeout=10,
            )
            data = resp.json()
            if not data or len(data) == 0:
                failed += 1
                continue
            org_id = data[0]["orgId"]
        except Exception as e:
            log.warning(f"[{stk_num}] get_org_id failed: {e}")
            failed += 1
            continue
        
        # 搜索减持公告
        anns = search_reduction_announcements(stk_num, org_id, args.days_back, session)
        all_results.extend(anns)
        
        if (i + 1) % args.batch == 0:
            log.info(f"Progress: {i+1}/{len(stock_list)}, found={len(all_results)}")
    
    log.info(f"Search done: {len(stock_list)} stocks, {len(all_results)} reduction announcements, {failed} failed")
    
    if all_results:
        df = pd.DataFrame(all_results)
        out_path = OUTPUT_DIR / "reduction_announcements.parquet"
        df.to_parquet(out_path, index=False)
        log.info(f"Saved {len(df)} records to {out_path}")
        
        # 统计
        stocks_with_anns = df["stock_code"].nunique()
        log.info(f"Stocks with reduction announcements: {stocks_with_anns}")
    
    summary = {
        "status": "ok",
        "stocks_searched": len(stock_list),
        "announcements_found": len(all_results),
        "stocks_with_anns": len(set(r["stock_code"] for r in all_results)),
        "failed": failed,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
