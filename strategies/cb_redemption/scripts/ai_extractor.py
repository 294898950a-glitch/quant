"""
AI-powered convertible bond holder table extractor.

Pipeline:
  PDF → Candidate Locator (rule-based) → AI Extraction (DeepSeek) → Validation → Bond Lifecycle Filter → Output
"""
import json, os, re, time, warnings
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import fitz
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ── Configuration ──

DEEPSEEK_API_KEY = None
_loaded = False


def _load_api_key():
    global DEEPSEEK_API_KEY, _loaded
    if _loaded:
        return
    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    DEEPSEEK_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
    _loaded = True


# ── Data Structures ──

@dataclass
class Candidate:
    stock_code: str
    pdf_path: str
    page_start: int
    page_end: int
    candidate_text: str
    locator_score: int
    anchor: str

@dataclass
class HolderRecord:
    rank: Optional[int] = None
    holder_name: str = ""
    holder_type: Optional[str] = None
    quantity: Optional[float] = None
    amount: Optional[float] = None
    ratio: Optional[float] = None

@dataclass
class ExtractionResult:
    stock_code: str
    pdf_path: str
    bond_code: Optional[str] = None
    bond_name: Optional[str] = None
    holders: list[HolderRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_ai_response: str = ""


# ═══════════════════════════════════════════════════════════════
# PHASE 1: Candidate Locator (Rule-based, High Recall)
# ═══════════════════════════════════════════════════════════════

STRONG_KW = ["前十名可转债持有人", "前十名转债持有人", "可转债持有人情况", "转债持有人情况", "前十名债券持有人"]
MID_KW = ["可转换公司债券", "可转债", "债券持有人", "持有数量", "持有金额", "占发行总量比例"]
NEG_KW = ["前十名股东", "普通股股东", "无限售条件股东", "优先股股东", "主要会计数据", "财务报表", "资产负债表", "利润表", "现金流量表"]

HEADER_RE = [re.compile(r"前十名.{0,30}?持有人"), re.compile(r"可转债.{0,20}?持有人"), re.compile(r"转债.{0,20}?持有人"), re.compile(r"债券持有人.{0,20}?情况")]
STOP_RE = [re.compile(r"前十名普通股股东"), re.compile(r"前十名股东"), re.compile(r"股东信息"), re.compile(r"财务报表"), re.compile(r"资产负债表"), re.compile(r"利润表"), re.compile(r"现金流量表"), re.compile(r"管理层讨论"), re.compile(r"董事会报告"), re.compile(r"重要事项")]


def score_page(text: str) -> int:
    s = 0
    for kw in STRONG_KW:
        if kw in text: s += 10
    for kw in MID_KW:
        if kw in text: s += 2
    for kw in NEG_KW:
        if kw in text: s -= 5
    return s


def locate_candidates(pdf_path: Path, stock_code: str = "", threshold: int = 8) -> list[Candidate]:
    if not pdf_path.exists() or pdf_path.stat().st_size < 100:
        return []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []

    pages_text = [doc[i].get_text("text") for i in range(len(doc))]
    candidates = []

    for pi, text in enumerate(pages_text):
        if score_page(text) < threshold:
            continue
        for pat in HEADER_RE:
            m = pat.search(text)
            if m:
                anchor = m.group(0)
                buf = text.split("\n")
                end_page = pi
                for pg in range(pi + 1, min(pi + 5, len(doc))):
                    nt = pages_text[pg]
                    if any(sp.search(nt) for sp in STOP_RE):
                        break
                    buf.extend(nt.split("\n"))
                    end_page = pg
                candidates.append(Candidate(
                    stock_code=stock_code,
                    pdf_path=str(pdf_path),
                    page_start=pi, page_end=end_page,
                    candidate_text="\n".join(buf),
                    locator_score=score_page(text),
                    anchor=anchor,
                ))
                break
    doc.close()
    return candidates


# ═══════════════════════════════════════════════════════════════
# PHASE 2: AI Extraction (DeepSeek)
# ═══════════════════════════════════════════════════════════════

AI_PROMPT = """从下面的文本中识别可转债/转债持有人表格，输出 JSON。

要求：
1. 只抽取可转债持有人，不抽取普通股股东
2. holder_name 必须是持有人全名——不得包含表头碎片（如"持有数量""持有比例""期末持债""可转换公司债券持有人名称"等），不得截断
3. 如果文本中名称跨行，必须自动拼接完整
4. 如果名称后紧跟性质（如"境内非国有法人""其他""境内自然人"），性质放在 holder_type，不要混入 holder_name
5. 只有金额(元)没有数量(张)的，quantity 填 null
6. 不存在的数据填 null，不编造
7. 只输出 JSON，不输出代码块标记或解释

输出格式: {{"tables":[{{"bond_code":null,"bond_name":null,"holders":[{{"rank":null,"holder_name":"","holder_type":null,"quantity":null,"amount":null,"ratio":null}}]}}],"warnings":[]}}

文本:
---
{candidate_text}
---"""


def ai_extract(candidate: Candidate, max_retries: int = 2) -> ExtractionResult:
    _load_api_key()
    if not DEEPSEEK_API_KEY:
        return ExtractionResult(stock_code=candidate.stock_code, pdf_path=candidate.pdf_path, warnings=["No API key"])

    prompt = AI_PROMPT.format(candidate_text=candidate.candidate_text[:8000])

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 3000,
                },
                timeout=45,
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # Extract JSON
            m = re.search(r'\{[\s\S]*\}', content)
            if not m:
                return ExtractionResult(stock_code=candidate.stock_code, pdf_path=candidate.pdf_path, warnings=["AI returned non-JSON"], raw_ai_response=content[:500])

            ai_data = json.loads(m.group(0))

            # Parse holders
            tables = ai_data.get("tables", [])
            if not tables:
                return ExtractionResult(stock_code=candidate.stock_code, pdf_path=candidate.pdf_path, warnings=["No tables extracted"], raw_ai_response=content[:500])

            table = tables[0]
            holders = []
            for h in table.get("holders", []):
                holders.append(HolderRecord(
                    rank=h.get("rank"),
                    holder_name=h.get("holder_name", "").strip(),
                    holder_type=h.get("holder_type"),
                    quantity=h.get("quantity"),
                    amount=h.get("amount"),
                    ratio=h.get("ratio"),
                ))

            return ExtractionResult(
                stock_code=candidate.stock_code,
                pdf_path=candidate.pdf_path,
                bond_code=table.get("bond_code"),
                bond_name=table.get("bond_name"),
                holders=holders,
                warnings=ai_data.get("warnings", []),
                raw_ai_response=content[:500],
            )

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return ExtractionResult(stock_code=candidate.stock_code, pdf_path=candidate.pdf_path, warnings=[f"AI error: {e}"])

    return ExtractionResult(stock_code=candidate.stock_code, pdf_path=candidate.pdf_path, warnings=["Max retries exceeded"])


# ═══════════════════════════════════════════════════════════════
# PHASE 3: Validation
# ═══════════════════════════════════════════════════════════════

BAD_NAME_PATTERNS = [
    "债券名称", "担保人", "单位", "持有人名称", "前十名", "占发行总量比例", "合计", "注：",
    "持有数量", "持有比例", "期末持债", "可转换公司债券持有人名称",
    "报告期末持有", "转债数量", "转债金额", "转债占比", "（元）", "(%)",
]


def validate_result(result: ExtractionResult) -> ExtractionResult:
    warnings = list(result.warnings)

    # 1. Name validation
    valid_holders = []
    for h in result.holders:
        name = h.holder_name
        bad = False
        for pat in BAD_NAME_PATTERNS:
            if pat in name:
                warnings.append(f"Name validation failed for '{name[:30]}': contains '{pat}'")
                bad = True
                break
        if bad:
            continue
        if len(name) < 2:
            warnings.append(f"Name too short: '{name}'")
            continue
        valid_holders.append(h)

    # 2. Ratio validation
    for h in valid_holders:
        if h.ratio is not None:
            if h.ratio < 0 or h.ratio > 100:
                warnings.append(f"Ratio out of range: {h.ratio} for '{h.holder_name[:20]}'")
            h.ratio = round(h.ratio, 2)

    # 3. Rank validation
    for h in valid_holders:
        if h.rank is not None and h.rank > 10:
            warnings.append(f"Rank > 10: {h.rank} for '{h.holder_name[:20]}'")

    # 4. Dedup by name (keep first)
    seen = set()
    deduped = []
    for h in valid_holders:
        if h.holder_name in seen:
            warnings.append(f"Duplicate holder: '{h.holder_name[:20]}'")
            continue
        seen.add(h.holder_name)
        deduped.append(h)

    result.holders = deduped
    result.warnings = warnings
    return result


# ═══════════════════════════════════════════════════════════════
# PHASE 4: Bond Lifecycle Filter
# ═══════════════════════════════════════════════════════════════

def extract_report_year(title: str) -> Optional[int]:
    m = re.search(r"(20\d{2})\s*年\s*(年度|半年度)", str(title))
    return int(m.group(1)) if m else None


def in_cb_lifecycle(stock_code: str, year: int, cb_basic: pd.DataFrame) -> bool:
    """Check if any CB was active for this stock in this year."""
    stk_num = str(stock_code).strip()
    matches = cb_basic[cb_basic["stk_code"].str.contains(stk_num)]
    for _, row in matches.iterrows():
        try:
            ly = int(str(row["list_date"])[:4])
            my = int(str(row["maturity_date"])[:4])
            if ly <= year <= my:
                return True
        except:
            pass
    return False


def get_active_bonds(stock_code: str, year: int, cb_basic: pd.DataFrame) -> list[str]:
    """Get names of active bonds for stock_code in given year."""
    stk_num = str(stock_code).strip()
    matches = cb_basic[cb_basic["stk_code"].str.contains(stk_num)]
    active = []
    for _, row in matches.iterrows():
        try:
            ly = int(str(row["list_date"])[:4])
            my = int(str(row["maturity_date"])[:4])
            if ly <= year <= my:
                active.append(row.get("bond_short_name", ""))
        except:
            pass
    return active


# ═══════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════

def extract_holders_from_pdf(
    pdf_path: str,
    stock_code: str = "",
    title: str = "",
    cb_basic: Optional[pd.DataFrame] = None,
) -> list[ExtractionResult]:
    """
    Full pipeline: locate → AI extract → validate → lifecycle filter.
    Returns list of ExtractionResult (one per candidate).
    """
    # Lifetime filter
    year = extract_report_year(title)
    if year and cb_basic is not None:
        if not in_cb_lifecycle(stock_code, year, cb_basic):
            return []  # Skip — no active CB this year

    # Phase 1: Locate
    candidates = locate_candidates(Path(pdf_path), stock_code=stock_code)
    if not candidates:
        return []

    # Phase 2: AI extract + Phase 3: validate
    results = []
    for cand in candidates:
        result = ai_extract(cand)
        result = validate_result(result)
        if result.holders:
            results.append(result)

    return results


# ── Quick test ──
if __name__ == "__main__":
    import sys
    _load_api_key()
    print(f"API key loaded: {bool(DEEPSEEK_API_KEY)}")

    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        stock = sys.argv[2] if len(sys.argv) > 2 else ""
        results = extract_holders_from_pdf(pdf_path, stock_code=stock)
        for r in results:
            print(f"\nStock: {r.stock_code}, Holders: {len(r.holders)}, Warnings: {len(r.warnings)}")
            for h in r.holders:
                print(f"  {h.holder_name[:40]:40s} | ratio={h.ratio} | type={h.holder_type}")
            if r.warnings:
                print(f"  Warnings: {r.warnings[:3]}")
