"""
可转债正股年报/半年报 PDF 批量下载器 + 解析器（v2 - 支持历史全量 + 按call事件倒推）

数据流:
  stk_code → topSearch/query → orgId
  orgId → hisAnnouncement/query → 公告列表
  adjunctUrl → static.cninfo.com.cn → PDF 下载
  PDF → PyMuPDF → 前十名持有人表格解析

模式:
  --mode active        存续转债全量（上市以来全部年报/半年报）  [默认]
  --mode call-backfill  退市转债按call事件倒推需要的报告年份
  --mode all            上面两者合并运行

Usage:
  python3 cninfo_pdf_crawler.py                    # 全量存续
  python3 cninfo_pdf_crawler.py --mode call-backfill
  python3 cninfo_pdf_crawler.py --mode all
  python3 cninfo_pdf_crawler.py --parse-only       # 仅解析已有PDF
  python3 cninfo_pdf_crawler.py --stocks 000651    # 指定单只
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# ── AI 解析器 ────────────────────────────────────────────────────────
try:
    from holder_ai.pipeline import extract_from_pdf as _ai_extract
    from holder_ai.ai_extract import load_deepseek_client
    _ai_client = None

    def _get_ai_client():
        global _ai_client
        if _ai_client is None:
            _ai_client = load_deepseek_client()
        return _ai_client
except ImportError:
    _ai_extract = None
    _get_ai_client = None

# ── Suppress MuPDF/fitz internal error spam ──
warnings.filterwarnings("ignore")

# ── 配置 ──────────────────────────────────────────────────────────────

WAREHOUSE_DIR = Path("/home/jay/projects/quant/data/cb_warehouse")
STRATEGY_DIR = Path("/home/jay/projects/quant/strategies/cb_redemption")
PDF_DIR = STRATEGY_DIR / "pdfs"
OUTPUT_DIR = STRATEGY_DIR / "output"

MAX_WORKERS = 5         # 下载并发
API_DELAY = 0.3         # API 间隔（秒）

CNINFO_API = "https://www.cninfo.com.cn"
CNINFO_STATIC = "https://static.cninfo.com.cn"

SEARCH_START = "2019-01-01"
SEARCH_END = datetime.now().strftime("%Y-%m-%d")

CATEGORIES = {
    "annual": "category_ndbg_szsh",
    "semi": "category_bndbg_szsh",
}

# 可选报告年份过滤
MIN_REPORT_YEAR = 2018   # 只爬 2018 年及以后的报告（巨潮API可覆盖+格式稳定）
MAX_REPORT_YEAR = datetime.now().year

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cninfo_crawler")


# ── 数据结构 ──────────────────────────────────────────────────────────

@dataclass
class StockInfo:
    stk_code: str
    stk_name: str
    org_id: str
    exchange: str


@dataclass
class Announcement:
    announcement_id: str
    title: str
    adjunct_url: str
    adjunct_size: Optional[int]
    announcement_time: int          # 毫秒时间戳
    category: str                   # annual / semi
    stock_code: str
    org_id: str


@dataclass
class HoldingRecord:
    bond_code: str
    bond_name: str
    stk_code: str
    stk_name: str
    report_date: str                # YYYY-MM-DD
    report_type: str                # annual / semi
    announcement_date: str          # 公告日
    holders: list                   # [{name, nature, quantity, amount, ratio}]
    total_ratio: float
    source_pdf: str


# ── API 客户端 ───────────────────────────────────────────────────────

class CNINFOClient:
    def __init__(self, delay: float = API_DELAY):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.cninfo.com.cn/new/disclosure/stock",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        })
        self._last_req = 0.0
        self.delay = delay

    def _throttle(self):
        elapsed = time.time() - self._last_req
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_req = time.time()

    def get_org_id(self, stk_code: str) -> Optional[StockInfo]:
        self._throttle()
        try:
            resp = self.session.post(
                f"{CNINFO_API}/new/information/topSearch/query",
                data={"keyWord": stk_code, "maxNum": "1"},
                timeout=10,
            )
            data = resp.json()
            if data and len(data) > 0:
                info = data[0]
                code = info["code"]
                return StockInfo(
                    stk_code=code,
                    stk_name=info["zwjc"],
                    org_id=info["orgId"],
                    exchange="SH" if info.get("type") == "shj" and code[:3] in ("600","601","603","605","688","689") else "SZ",
                )
        except Exception as e:
            log.warning(f"[{stk_code}] get_org_id failed: {e}")
        return None

    def search_announcements(
        self,
        stock_code: str,
        org_id: str,
        category_key: str,
        page_num: int = 1,
        page_size: int = 30,
        se_date: str = "",
    ) -> Optional[dict]:
        self._throttle()
        category = CATEGORIES[category_key]
        column = "sse" if stock_code[:3] in ("600", "601", "603", "605", "688", "689") else "szse"

        sd = se_date if se_date else f"{SEARCH_START}~{SEARCH_END}"
        payload = {
            "pageNum": str(page_num),
            "pageSize": str(page_size),
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{stock_code},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": category,
            "trade": "",
            "seDate": sd,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        try:
            resp = self.session.post(
                f"{CNINFO_API}/new/hisAnnouncement/query",
                data=payload,
                timeout=15,
            )
            return resp.json()
        except Exception as e:
            log.warning(f"[{stock_code}] search failed: {e}")
        return None

    def get_all_announcements(
        self, stock_code: str, org_id: str,
        year_start: int = MIN_REPORT_YEAR, year_end: int = MAX_REPORT_YEAR,
    ) -> list[Announcement]:
        """获取某只股票指定年份范围内的年报+半年报"""
        results = []
        page_size = 30

        # 按年份分段搜索（巨潮API支持按年过滤）
        for year in range(year_start, year_end + 1):
            se_date = f"{year}-01-01~{min(year, MAX_REPORT_YEAR)}-12-31"
            for cat_key in ["annual", "semi"]:
                page = 1
                while True:
                    data = self.search_announcements(stock_code, org_id, cat_key, page, se_date=se_date)
                    if not data:
                        break
                    anns = data.get("announcements", [])
                    if not anns:
                        break

                    for a in anns:
                        title = a.get("announcementTitle", "")
                        if "摘要" in title or "英文版" in title:
                            continue
                        if "年度报告" not in title and "半年度报告" not in title:
                            continue

                        # 安全类型判断：检查标题+类别
                        resolved_cat = cat_key
                        if "半年度" in title:
                            resolved_cat = "semi"
                        elif "年度" in title:
                            resolved_cat = "annual"

                        results.append(Announcement(
                            announcement_id=str(a["announcementId"]),
                            title=title,
                            adjunct_url=a.get("adjunctUrl", ""),
                            adjunct_size=a.get("adjunctSize"),
                            announcement_time=a.get("announcementTime", 0),
                            category=resolved_cat,
                            stock_code=stock_code,
                            org_id=org_id,
                        ))

                    total = data.get("totalRecordNum", 0)
                    if page * page_size >= total:
                        break
                    page += 1

        return results

    def download_pdf(self, adjunct_url: str, target_path: Path) -> bool:
        url = f"{CNINFO_STATIC}/{adjunct_url}"
        try:
            resp = self.session.get(url, timeout=60)
            if resp.status_code == 200 and len(resp.content) > 1000:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(resp.content)
                return True
        except Exception as e:
            log.warning(f"Download failed {url[:80]}: {e}")
        return False


# ── PDF 解析器 ────────────────────────────────────────────────────────

# ── Name cleaning patterns (post-processing) ──

# Header fragments that may bleed into holder names (exact string replacements)
_HEADER_FRAGMENTS = [
    # Column headers
    "期末持债数量（元）",
    "期末持债数量（张）",
    "期末持债数量(元)",
    "期末持债数量(张)",
    "期末持债金额（元）",
    "期末持债金额(元)",
    "持有比例(%)",
    "持有比例（%）",
    "持有比例(％)",
    "持有比例(%)",
    "报告期末持有可转债数量（张）",
    "报告期末持有可转债数量(张)",
    "报告期末持有可转债金额（元）",
    "报告期末持有可转债金额(元)",
    "报告期末持有可转债占比（%）",
    "报告期末持有可转债占比(%)",
    "可转换公司债券持有人名称",
    "可转债持有人名称",
    "可转债持有人性质",
    "债券持有人名称",
    "前十名转债持有人情况如下：",
    "前十名可转债持有人情况如下：",
    # Additional fragments
    "转债数量（张）",
    "转债金额（元）",
    "转债占比(%)",
    "转债占比（%）",
    "持债数量（元）",
    "持债数量（张）",
]

# Regex-based header fragment patterns (more flexible, match anywhere in string)
_HEADER_FRAGMENT_REGEXES = [
    # "持有比例(%)" / "持有比例（%）" / "持有比例(％)" — with optional spaces
    re.compile(r"持有比例[\(\（]\s*%?\s*[\)\）]\s*"),
    # "期末持债数量（元）" / "期末持债数量（人民币元）"
    re.compile(r"期末持债数量[\(\（]\s*人民币?元\s*[\)\）]\s*"),
    # "期末持债数（元）" — shorter variant
    re.compile(r"期末持债数[\(\（]\s*元\s*[\)\）]\s*"),
    # "期末持债数量（张）" / "期末持债数量(张)"
    re.compile(r"期末持债数量[\(\（]\s*张\s*[\)\）]\s*"),
    # "期末持债金额（元）" / "期末持债金额(元)"
    re.compile(r"期末持债金额[\(\（]\s*元\s*[\)\）]\s*"),
    # "报告期末持有可转债数量（张）"
    re.compile(r"报告期末持有可转债数量[\(\（]\s*张\s*[\)\）]\s*"),
    # "报告期末持有可转债金额（元）"
    re.compile(r"报告期末持有可转债金额[\(\（]\s*元\s*[\)\）]\s*"),
    # "报告期末持有可转债占比（%）"
    re.compile(r"报告期末持有可转债占比[\(\（]\s*%?\s*[\)\）]\s*"),
]

# Page header patterns that bleed into names
_PAGE_HEADER_PATTERNS = [
    # "2021 年年度报告42 / 169" style page headers
    re.compile(r"^\d{4}\s*年年度报告\s*\d+\s*/\s*\d+"),
    re.compile(r"^\d{4}\s*年半年度报告\s*\d+\s*/\s*\d+"),
    # "2021 年年度报告" alone
    re.compile(r"^\d{4}\s*年年度报告\s*"),
    re.compile(r"^\d{4}\s*年半年度报告\s*"),
    # Page number only at start "58 / 198"
    re.compile(r"^\d+\s*/\s*\d+\s+"),
]

# Table description fragments that can bleed in
_TABLE_DESC_PATTERNS = [
    re.compile(r"可转换公司债券名称\S*转债.*?前十名.*?持有人情况如下[：:]"),
    re.compile(r"前十名转债持有人情况如下[：:]\s*"),
    re.compile(r"前十名可转债持有人情况如下[：:]\s*"),
    re.compile(r"期末转债持有人数\s*\d+\S*"),
    re.compile(r"本公司转债的担保人\S*"),
    re.compile(r"担保人盈利能力.*?不适用"),
]


def clean_holder_name(name: str) -> str:
    """
    Post-process a holder name to remove header fragments, page numbers,
    and other artifacts that bleed into names during PDF text extraction.
    Returns cleaned name, or empty string if nothing meaningful remains.
    """
    if not name or not name.strip():
        return ""

    name = name.strip()

    # Step 1: Remove known header fragments (they can appear in any order)
    for fragment in _HEADER_FRAGMENTS:
        # Use plain string replace for exact fragments
        name = name.replace(fragment, "")

    # Step 1b: Remove header fragments via regex (more flexible patterns)
    for pat in _HEADER_FRAGMENT_REGEXES:
        name = pat.sub("", name)

    # Step 2: Remove table description patterns
    for pat in _TABLE_DESC_PATTERNS:
        name = pat.sub("", name)

    # Step 3: Remove page header patterns
    for pat in _PAGE_HEADER_PATTERNS:
        name = pat.sub("", name)

    # Step 4: Remove "有限公司" split artifacts like "份有限公司" at start
    # (this happens when company name splits across lines and the back half bleeds)
    name = re.sub(r"^份有限公司\s*", "", name)
    name = re.sub(r"^有限公司\s*", "", name)
    name = re.sub(r"^公司\s*", "", name)

    # Step 5: Remove trailing numbers that aren't part of names
    # (e.g. quantities that bled into name field)
    # But be careful: some names end with numbers like "林园投资恒泰88 号"
    # Only strip if it looks like a standalone number at the very end
    name = re.sub(r"\s+\d{3,}(?:,\d{3})*(?:\.\d+)?$", "", name)
    name = re.sub(r"\s+\d{1,2}$", "", name)

    # Step 6: Clean up whitespace and punctuation artifacts
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" ,，;；:：.。、")

    # Step 7: Remove any remaining pure header-like content
    if re.match(r"^[\s\d,.%（）()]+$", name):
        return ""

    return name


# ── Header detection helpers ──

def _is_header_line(line: str) -> bool:
    """Check if a line is a table header/column title line."""
    line = line.strip()
    if not line:
        return True

    # Exact match patterns
    exact_patterns = [
        "序号", "可转债持有人名称", "可转债持有人性质",
        "可转债持有人性", "质",
        "报告期末持有可", "转债数量（张）",
        "报告期末持有可", "转债金额（元）",
        "报告期末持有可", "转债占比",
        "可转换公司债券持有人名称",
        "可转债持有人名称",
        "债券持有人名称",
        "持有人名称",
        "期末持债数量（元）",
        "期末持债数量（张）",
        "期末持债金额（元）",
        "持有比例(%)",
        "持有比例（%）",
        "持有比例(％)",
        "报告期末持有可转债数量（张）",
        "报告期末持有可转债金额（元）",
        "报告期末持有可转债占比（%）",
        "转债数量（张）",
        "转债金额（元）",
        "转债占比",
        "持债数量（元）",
        "持债数量（张）",
        "持债金额（元）",
        "持有比例",
        "可转换公司债券持有人性",
        "质",
    ]
    if line in exact_patterns:
        return True

    # Regex patterns for combined header lines
    header_regexes = [
        # Exact match (whole line is just the header)
        r"^期末持债数量[（(]元[）)]\s*持有比例[（(]%[）)]$",
        r"^期末持债数量[（(]张[）)]\s*持有比例[（(]%[）)]$",
        r"^报告期末持有可转债数量[（(]张[）)]\s*报告期末持有可转债金额[（(]元[）)]\s*报告期末持有可转债占比[（(]%[）)]$",
        r"^报告期末持有可转债数量[（(]张[）)]\s*报告期末持有可转债占比[（(]%[）)]$",
    ]
    for pat in header_regexes:
        if re.match(pat, line):
            return True

    # Prefix match (header followed by more content, e.g. header+name concatenated)
    header_prefix_regexes = [
        r"^期末持债数量[（(]元[）)]\s*持有比例[（(]%?\s*[）)]",
        r"^期末持债数量[（(]张[）)]\s*持有比例[（(]%?\s*[）)]",
        r"^期末持债数量[（(]元[）)]",
        r"^期末持债数量[（(]张[）)]",
        r"^期末持债金额[（(]元[）)]",
        r"^持有比例[（(]%?\s*[）)]",
        r"^报告期末持有可转债数量[（(]张[）)]",
        r"^报告期末持有可转债金额[（(]元[）)]",
        r"^报告期末持有可转债占比[（(]%?\s*[）)]",
    ]
    for pat in header_prefix_regexes:
        if re.match(pat, line):
            return True

    return False


# ── Main parser (AI-powered) ──

def parse_holder_table(pdf_path: Path) -> Optional[list[dict]]:
    """
    AI-powered holder table extraction.
    三阶段：规则定位 → DeepSeek 识别 → 程序校验。
    返回 [{name, nature, quantity, amount, ratio}] 或 None。
    """
    if _ai_extract is None:
        log.warning("AI parser not available, skipping")
        return None

    if not pdf_path.exists() or pdf_path.stat().st_size < 100:
        return None

    try:
        result = _ai_extract(Path(pdf_path), client=_get_ai_client())
    except Exception as e:
        log.warning(f"  AI parse failed for {pdf_path.name}: {e}")
        return None

    if not result or not result.get("holders"):
        return None

    # Convert AI format to legacy format (compatible with downstream consumers)
    return [{
        "name": h["holder_name"],
        "nature": h.get("holder_type") or "",
        "quantity": h.get("quantity") or 0,
        "amount": h.get("amount") or 0,
        "ratio": h.get("ratio") or 0,
    } for h in result["holders"]]


# ── 核心流程 ─────────────────────────────────────────────────────────

def load_cb_data() -> pd.DataFrame:
    """加载可转债基础数据"""
    return pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")


def load_call_events() -> pd.DataFrame:
    """加载强赎事件"""
    return pd.read_parquet(WAREHOUSE_DIR / "cb_call.parquet")


def get_active_stocks(cb_basic: pd.DataFrame) -> list[str]:
    """获取存续转债的正股列表"""
    active = cb_basic[cb_basic["remain_size"] > 0].dropna(subset=["stk_code"]).copy()
    active["stk_num"] = active["stk_code"].str.extract(r"(\d{6})")
    return active["stk_num"].unique().tolist()


def get_call_backfill_targets(cb_basic: pd.DataFrame, cb_call: pd.DataFrame) -> dict:
    """
    按call事件倒推每个正股需要的报告年份。
    返回: {stk_num: [(year, report_type), ...]}
    对每个call事件:
      - 公告日在5月及以后 → 需要前一年的年报
      - 公告日在9月及以后 → 还需要当年的半年报（如果已出）
    """
    merged = cb_call.merge(
        cb_basic[["ts_code", "stk_code"]], on="ts_code", how="inner"
    )
    merged = merged.dropna(subset=["stk_code"])
    merged["stk_num"] = merged["stk_code"].str.extract(r"(\d{6})")
    # 过滤掉存续中的（存续的用主动模式全量爬）
    active_stocks = set(get_active_stocks(cb_basic))

    targets: dict[str, set] = {}

    for _, row in merged.iterrows():
        stk = row["stk_num"]
        if stk in active_stocks:
            continue

        ann_date = pd.Timestamp(row["ann_date"])
        year = ann_date.year
        month = ann_date.month

        if stk not in targets:
            targets[stk] = set()

        # 年报：前一年的年报
        if month >= 5:
            targets[stk].add((year - 1, "annual"))
        else:
            targets[stk].add((max(year - 2, MIN_REPORT_YEAR), "annual"))

        # 半年报：如果公告日在9月后，当年半年报已出
        if month >= 9:
            targets[stk].add((year, "semi"))

    # 按年份范围压缩
    result = {}
    for stk, yrs in targets.items():
        years_list = sorted(yrs)
        if years_list:
            min_y = min(y for y, t in years_list)
            max_y = max(y for y, t in years_list)
            has_semi = any(t == "semi" for y, t in years_list)
            # 如果某年既有annual又有semi，都覆盖
            # 实际get_all_announcements会按年+分类查询
            year_set = sorted(set(y for y, t in years_list))
            result[stk] = (min(year_set), max(year_set), has_semi)
        else:
            result[stk] = (MIN_REPORT_YEAR, MAX_REPORT_YEAR, False)

    return result


def lookup_all_org_ids(client: CNINFOClient, stocks: list[str]) -> list[StockInfo]:
    all_stocks = []
    failed = []
    for i, stk_num in enumerate(stocks):
        info = client.get_org_id(stk_num)
        if info:
            all_stocks.append(info)
            if len(all_stocks) % 50 == 0:
                log.info(f"  orgId progress: {len(all_stocks)}/{len(stocks)}")
        else:
            failed.append(stk_num)
    log.info(f"orgId done: {len(all_stocks)} OK, {len(failed)} failed")
    if failed:
        log.warning(f"  Failed: {failed[:20]}...")
    return all_stocks


def fetch_all_announcements(
    client: CNINFOClient,
    stock_infos: list[StockInfo],
    backfill_targets: Optional[dict] = None,
) -> list[Announcement]:
    all_anns = []
    for i, info in enumerate(stock_infos):
        if backfill_targets and info.stk_code in backfill_targets:
            ymin, ymax, need_semi = backfill_targets[info.stk_code]
            # 如果只需要几个年份，缩小范围加速
            anns = client.get_all_announcements(
                info.stk_code, info.org_id,
                year_start=max(ymin, MIN_REPORT_YEAR),
                year_end=min(ymax, MAX_REPORT_YEAR),
            )
        else:
            # 存续全量
            anns = client.get_all_announcements(info.stk_code, info.org_id)
        all_anns.extend(anns)
        if (i + 1) % 50 == 0:
            log.info(f"  Announcement progress: {i+1}/{len(stock_infos)}, total={len(all_anns)}")
    return all_anns


def paginate_batch(items, batch_size=100):
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]


def download_pdfs_batch(
    client: CNINFOClient,
    announcements: list[Announcement],
    max_workers: int = MAX_WORKERS,
) -> tuple[int, list[Announcement]]:
    downloaded = []
    for batch in paginate_batch(announcements, batch_size=max_workers * 2):
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for ann in batch:
                pdf_path = PDF_DIR / ann.stock_code / f"{ann.announcement_id}.pdf"
                if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                    downloaded.append(ann)
                    continue
                futures[executor.submit(client.download_pdf, ann.adjunct_url, pdf_path)] = ann

            for future in as_completed(futures):
                ann = futures[future]
                if future.result():
                    downloaded.append(ann)

    log.info(f"PDF downloaded: {len(downloaded)}/{len(announcements)}")
    return len(downloaded), downloaded


def save_metadata(announcements: list[Announcement], output_path: Path):
    records = []
    for a in announcements:
        records.append({
            "announcement_id": a.announcement_id,
            "title": a.title,
            "adjunct_url": a.adjunct_url,
            "category": a.category,
            "stock_code": a.stock_code,
            "org_id": a.org_id,
            "announcement_time": a.announcement_time,
            "pdf_path": str(PDF_DIR / a.stock_code / f"{a.announcement_id}.pdf"),
        })
    df = pd.DataFrame(records)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    log.info(f"Metadata saved: {output_path} ({len(df)} records)")
    return df


def parse_all_pdfs(metadata: pd.DataFrame) -> pd.DataFrame:
    """解析所有已下载PDF的持有人表格"""
    holders_list = []
    total = len(metadata)
    for i, row in metadata.iterrows():
        pdf_path = Path(row["pdf_path"])
        if not pdf_path.exists():
            continue

        table = parse_holder_table(pdf_path)
        if table is not None:
            holders_list.append({
                "announcement_id": row["announcement_id"],
                "stock_code": row["stock_code"],
                "title": row["title"],
                "category": row["category"],
                "pdf_path": str(pdf_path),
                "num_holders": len(table),
                "holders": json.dumps(table, ensure_ascii=False),
                "top1_ratio": table[0]["ratio"] if len(table) > 0 else 0,
                "top3_ratio": sum(h["ratio"] for h in table[:3]) if len(table) >= 3 else 0,
                "top5_ratio": sum(h["ratio"] for h in table[:5]) if len(table) >= 5 else 0,
                "top10_ratio": sum(h["ratio"] for h in table),
            })

        if (i + 1) % 100 == 0:
            log.info(f"  Parse progress: {i+1}/{total}, found={len(holders_list)}")

    df = pd.DataFrame(holders_list)
    log.info(f"PDF parsing done: {len(df)}/{total} contain holder tables")
    return df


def merge_holders_to_events(holders_df: pd.DataFrame) -> pd.DataFrame:
    """将持有人数据与call事件对齐（以防前视偏差的merge_asof）"""
    cb_call = load_call_events()
    cb_basic = load_cb_data()

    # 从holder数据中提取announcement_time（公告日）
    meta = pd.read_parquet(OUTPUT_DIR / "announcement_metadata.parquet")
    holders_meta = holders_df.merge(meta[["announcement_id", "announcement_time"]], on="announcement_id", how="left")
    holders_meta["declare_date"] = pd.to_datetime(holders_meta["announcement_time"], unit="ms")
    holders_meta["ts_code"] = None

    # 从cb_basic找到ts_code
    stock_ts_map = {}
    for _, row in cb_basic.iterrows():
        stk = row["stk_code"]
        if pd.notna(stk):
            num = re.search(r"(\d{6})", str(stk))
            if num:
                stock_ts_map[num.group(1)] = row["ts_code"]

    holders_meta["stk_num"] = holders_meta["stock_code"].astype(str).str[:6]
    holders_meta["ts_code"] = holders_meta["stk_num"].map(stock_ts_map)

    # merge_asof 对齐
    events = cb_call.copy()
    events["event_date"] = pd.to_datetime(events["ann_date"], format="%Y%m%d")

    holders_aligned = holders_meta.dropna(subset=["ts_code"]).sort_values("declare_date")
    events_sorted = events.sort_values("event_date")

    merged = pd.merge_asof(
        events_sorted,
        holders_aligned[["ts_code", "declare_date", "top1_ratio", "top3_ratio", "top5_ratio", "top10_ratio", "holders"]],
        on="declare_date",
        by="ts_code",
        direction="backward",
        tolerance=pd.Timedelta(days=365 * 2),  # 最多往前看2年
    )

    return merged


# ── 主入口 ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="可转债年报PDF爬虫+解析器 v2")
    parser.add_argument("--stocks", nargs="+", default=[], help="指定正股代码")
    parser.add_argument("--mode", choices=["active", "call-backfill", "all"], default="active",
                        help="爬取模式 (默认=%default)")
    parser.add_argument("--parse-only", action="store_true", help="仅解析已有PDF")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载")
    parser.add_argument("--skip-parse", action="store_true", help="跳过PDF解析")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计不执行")
    args = parser.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = CNINFOClient()
    cb_basic = load_cb_data()

    # ── Step 0: 确定正股列表 ──
    if args.stocks:
        stock_list = args.stocks
        log.info(f"Manual stock list: {stock_list} ({len(stock_list)} stocks)")
        backfill_targets = None
    elif args.mode == "active":
        stock_list = get_active_stocks(cb_basic)
        backfill_targets = None
        log.info(f"Active mode: {len(stock_list)} active CB stocks")
    elif args.mode == "call-backfill":
        cb_call = load_call_events()
        backfill_targets = get_call_backfill_targets(cb_basic, cb_call)
        stock_list = list(backfill_targets.keys())
        log.info(f"Call-backfill mode: {len(stock_list)} stocks with historical call events")
        total_years = sum(
            (1 + (ymax - ymin)) for ymin, ymax, _ in backfill_targets.values()
        )
        log.info(f"  Estimated report-years to fetch: {total_years}")
    elif args.mode == "all":
        # 存续全量 + 退市倒推
        active_list = set(get_active_stocks(cb_basic))
        cb_call = load_call_events()
        backfill_map = get_call_backfill_targets(cb_basic, cb_call)
        stock_list = list(active_list | set(backfill_map.keys()))
        # backfill_map只对退市股票生效
        backfill_targets = backfill_map
        log.info(f"All mode: {len(stock_list)} stocks total "
                 f"({len(active_list)} active + {len(set(backfill_map.keys()))} backfill)")
    else:
        stock_list = get_active_stocks(cb_basic)
        backfill_targets = None

    if args.dry_run:
        log.info(f"[DRY RUN] Stock count: {len(stock_list)}")
        if backfill_targets:
            log.info(f"[DRY RUN] Backfill targets count: {len(backfill_targets)}")
        return

    if args.parse_only:
        log.info("Parse-only mode")
        meta_path = OUTPUT_DIR / "announcement_metadata.parquet"
        if not meta_path.exists():
            log.error(f"No metadata at {meta_path}")
            sys.exit(1)
        meta_df = pd.read_parquet(meta_path)
        log.info(f"Loaded {len(meta_df)} announcements from metadata")

        # 解析PDF
        results = parse_all_pdfs(meta_df)
        if len(results) > 0:
            out_path = OUTPUT_DIR / "holder_records_v2.parquet"
            results.to_parquet(out_path, index=False)
            log.info(f"Holder records saved: {out_path} ({len(results)} rows, "
                     f"{results['num_holders'].sum():.0f} holders total)")
            # 统计
            if "top1_ratio" in results.columns:
                log.info(f"  top1_ratio mean={results['top1_ratio'].mean():.2f}%, "
                         f"top5_ratio mean={results['top5_ratio'].mean():.2f}%")
        else:
            log.warning("No holder tables found in any PDF!")
        return

    # ── Step 1: 查询 orgId ──
    log.info("Step 1/4: Querying orgIds...")
    all_infos = lookup_all_org_ids(client, stock_list)

    # ── Step 2: 搜索公告 ──
    log.info("Step 2/4: Searching announcements...")
    all_anns = fetch_all_announcements(client, all_infos, backfill_targets)
    log.info(f"  Found {len(all_anns)} annual/semi-annual reports")

    # 保存元数据
    meta_path = OUTPUT_DIR / "announcement_metadata.parquet"
    meta_df = save_metadata(all_anns, meta_path)

    # 统计：覆盖了多少只股票、多少年份
    if len(all_anns) > 0:
        ann_df = pd.DataFrame([asdict(a) for a in all_anns])
        log.info(f"  Stocks covered: {ann_df['stock_code'].nunique()}")
        log.info(f"  Annual reports: {(ann_df['category'] == 'annual').sum()}")
        log.info(f"  Semi-annual reports: {(ann_df['category'] == 'semi').sum()}")

    # ── Step 3: 下载 PDF ──
    if not args.skip_download:
        log.info(f"Step 3/4: Downloading {len(all_anns)} PDFs...")
        ok_count, downloaded = download_pdfs_batch(client, all_anns)
        log.info(f"  Downloaded: {ok_count}/{len(all_anns)}")
    else:
        downloaded = all_anns

    # ── Step 4: 解析 PDF ──
    if not args.skip_parse:
        log.info("Step 4/4: Parsing holder tables from PDFs...")
        results = parse_all_pdfs(meta_df)
        if len(results) > 0:
            out_path = OUTPUT_DIR / "holder_records_v2.parquet"
            results.to_parquet(out_path, index=False)
            log.info(f"Holder records saved: {out_path} ({len(results)} rows)")
            if "top1_ratio" in results.columns:
                log.info(f"  top1_ratio mean={results['top1_ratio'].mean():.2f}%")
        else:
            log.warning("No holder tables found!")

    log.info("Done!")


if __name__ == "__main__":
    main()
