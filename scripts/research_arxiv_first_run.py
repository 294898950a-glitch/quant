#!/usr/bin/env python3
"""Fetch and screen arxiv paper candidates for the quant research framework.

Uses only the Python standard library so it can run on sig without installing
packages.  Output is Markdown suitable for
``data/research_framework/paper_candidates/<date>.md``.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_KEYWORDS = (
    "convertible bond arbitrage",
    "volatility regime switching",
    "panic detection equity",
    "credit spread quant strategy",
    "mean reversion convertible",
    "quant strategy live trading",
)
NEW_STRATEGY_KEYWORDS = {
    "event-driven equity strategy": "event-driven equity",
    "corporate action arbitrage": "corporate action arbitrage",
    "momentum trend following systematic": "momentum trend following",
    "price volume divergence equity": "price-volume divergence",
    "cross-asset arbitrage quantitative": "cross-asset arbitrage",
    "statistical arbitrage stock": "statistical arbitrage stock",
}
TOP_VENUES = (
    "neurips",
    "icml",
    "kdd",
    "jfqa",
    "review of financial studies",
    "journal of finance",
    "quantitative finance",
    "rfs",
)
FINANCE_RE = re.compile(
    r"\b(arbitrage|trading|portfolio|market|volatility|credit|bond|option|derivative|spread|mean reversion|stock|asset|finance|risk)\b",
    flags=re.I,
)
HEADERS = {"User-Agent": "quant-codex-arxiv-first-run/1.0"}


def fetch(url: str, timeout: int = 25) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def tokens(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]{4,}", value)}


def arxiv_search(keyword: str, max_results: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "search_query": "cat:q-fin.* AND all:" + keyword,
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
    )
    root = ET.fromstring(fetch("https://export.arxiv.org/api/query?" + query))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rows: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ns):
        title = clean(entry.findtext("a:title", default="", namespaces=ns))
        published = clean(entry.findtext("a:published", default="", namespaces=ns))
        rows.append(
            {
                "keyword": keyword,
                "title": title,
                "published": published[:10],
                "year": int(published[:4]) if published[:4].isdigit() else 0,
                "summary": clean(entry.findtext("a:summary", default="", namespaces=ns)),
                "arxiv_id": clean(entry.findtext("a:id", default="", namespaces=ns)).rsplit("/", 1)[-1],
            }
        )
    return rows


def semantic_scholar_match(title: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"query": title, "limit": 3, "fields": "title,year,citationCount,venue,externalIds,url"}
    )
    try:
        data = json.loads(
            fetch("https://api.semanticscholar.org/graph/v1/paper/search?" + query).decode("utf-8")
        )
    except Exception as exc:
        return {"error": str(exc), "citationCount": 0, "venue": "", "url": ""}

    source_tokens = tokens(title)
    best: dict[str, Any] = {}
    best_score = -1.0
    for row in data.get("data", []):
        score = len(source_tokens & tokens(row.get("title", ""))) / max(
            1, len(source_tokens | tokens(row.get("title", "")))
        )
        if score > best_score:
            best = row
            best_score = score
    best["match_score"] = round(max(best_score, 0.0), 3)
    return best


def semantic_scholar_by_arxiv(arxiv_id: str) -> dict[str, Any]:
    if not arxiv_id:
        return {}
    base_id = arxiv_id.split("v", 1)[0]
    encoded = urllib.parse.quote("arXiv:" + base_id, safe="")
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/"
        + encoded
        + "?fields=title,year,citationCount,venue,externalIds,url,abstract"
    )
    try:
        row = json.loads(fetch(url).decode("utf-8"))
    except Exception:
        return {}
    row["match_score"] = 1.0
    return row


def semantic_scholar_keyword_search(keyword: str, limit: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "query": keyword,
            "limit": limit,
            "fields": "title,year,citationCount,venue,externalIds,url,abstract",
        }
    )
    try:
        data = json.loads(
            fetch("https://api.semanticscholar.org/graph/v1/paper/search?" + query).decode("utf-8")
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for row in data.get("data", []):
        external = row.get("externalIds") or {}
        arxiv_id = external.get("ArXiv") or external.get("ARXIV") or ""
        if not arxiv_id:
            continue
        rows.append(
            {
                "keyword": keyword,
                "title": clean(row.get("title") or ""),
                "published": str(row.get("year") or ""),
                "year": int(row.get("year") or 0),
                "summary": clean(row.get("abstract") or ""),
                "arxiv_id": arxiv_id,
                "s2": row,
            }
        )
    return rows


def read_keywords(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return DEFAULT_KEYWORDS
    keywords: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        keywords.append(value)
    return tuple(keywords)


def strategy_direction(keyword: str) -> str:
    if keyword in NEW_STRATEGY_KEYWORDS:
        return NEW_STRATEGY_KEYWORDS[keyword]
    if "convertible" in keyword or keyword.startswith("cb_arb"):
        return "cb_arb / convertible bond"
    if "regime" in keyword or "tail" in keyword:
        return "regime / tail-risk timing"
    if "panic" in keyword or "crash" in keyword:
        return "panic / crash signal"
    if "credit" in keyword:
        return "credit spread"
    return "general quantitative strategy"


def screen(row: dict[str, Any]) -> dict[str, Any] | None:
    s2 = row.get("s2", {})
    citations = int(s2.get("citationCount") or 0)
    venue = clean(s2.get("venue") or "")
    venue_lower = venue.lower()
    summary = row.get("summary", "")
    if not FINANCE_RE.search(row.get("title", "") + " " + summary):
        return None

    year = int(row.get("year") or s2.get("year") or 0)
    citation_floor = 100 if year and year < 2020 else 50
    if citations >= citation_floor:
        passed_rule = "引用"
        evidence = f"Semantic Scholar citationCount={citations}; match_score={s2.get('match_score', 'n/a')}"
    elif any(venue_name in venue_lower for venue_name in TOP_VENUES):
        passed_rule = "顶会"
        evidence = f"Semantic Scholar venue={venue}"
    elif re.search(r"(live trading|paper trading)", summary, flags=re.I) and re.search(
        r"(sharpe|drawdown|max(?:imum)? drawdown)", summary, flags=re.I
    ):
        passed_rule = "试盘"
        evidence = "arxiv abstract mentions live/paper trading plus Sharpe/drawdown metric"
    else:
        return None

    priority = "高" if year >= 2025 else ("中" if citations >= 100 else "低")
    return {
        **row,
        "citations": citations,
        "venue": venue,
        "year": year,
        "s2_url": s2.get("url") or "",
        "passed_rule": passed_rule,
        "evidence": evidence,
        "priority": priority,
        "strategy_direction": strategy_direction(str(row.get("keyword") or "")),
    }


def collect(keywords: tuple[str, ...], max_per_keyword: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for keyword in keywords:
        try:
            found = arxiv_search(keyword, max_per_keyword)
        except Exception as exc:
            rejected.append({"title": keyword, "reason": "query_error: " + str(exc), "arxiv_id": "n/a"})
            continue
        for row in found:
            key = row.get("arxiv_id") or row.get("title")
            if key in seen:
                continue
            seen.add(key)
            time.sleep(0.15)
            row["s2"] = semantic_scholar_by_arxiv(row.get("arxiv_id", "")) or semantic_scholar_match(row["title"])
            screened = screen(row)
            if screened is None:
                s2 = row.get("s2", {})
                rejected.append(
                    {
                        "title": row.get("title", ""),
                        "arxiv_id": row.get("arxiv_id", ""),
                        "reason": f"failed hard gate: citations={int(s2.get('citationCount') or 0)}, venue={clean(s2.get('venue') or 'n/a')}",
                    }
                )
            else:
                rows.append(screened)
        time.sleep(0.5)
        for row in semantic_scholar_keyword_search(keyword, max_per_keyword):
            key = row.get("arxiv_id") or row.get("title")
            if key in seen:
                continue
            seen.add(key)
            screened = screen(row)
            if screened is None:
                s2 = row.get("s2", {})
                rejected.append(
                    {
                        "title": row.get("title", ""),
                        "arxiv_id": row.get("arxiv_id", ""),
                        "reason": f"S2 arxiv fallback failed hard gate: citations={int(s2.get('citationCount') or 0)}, venue={clean(s2.get('venue') or 'n/a')}",
                    }
                )
            else:
                rows.append(screened)
        time.sleep(0.5)
    return rows, rejected


def render(rows: list[dict[str, Any]], rejected: list[dict[str, Any]], run_date: str) -> str:
    priority_rank = {"高": 0, "中": 1, "低": 2}
    rows = sorted(
        rows,
        key=lambda row: (priority_rank.get(row["priority"], 9), -int(row.get("year") or 0), -int(row.get("citations") or 0)),
    )[:5]

    lines = [
        f"# arxiv 论文候选 {run_date}",
        "",
        f"Generated: {run_date} CST",
        "Data sources: arxiv API + Semantic Scholar API",
        "Screening: same hard evidence gate for 2020+; pre-2020 citation candidates require >=100 citations. 2025+ only raises priority.",
        "",
        "## 通过筛选 (>= 1, <= 5 篇)",
        "",
    ]
    if not rows:
        lines.extend(["none", ""])
    for idx, row in enumerate(rows, 1):
        lines.extend(
            [
                f"### {idx}. {row['title']}",
                f"- priority: {row['priority']}",
                f"- passed_rule: {row['passed_rule']}",
                f"- evidence: {row['evidence']}",
                f"- year: {row.get('year')}",
                f"- 关联策略方向: {row.get('strategy_direction')}",
                f"- keyword: {row.get('keyword')}",
                f"- arxiv id / DOI: {row.get('arxiv_id')}",
                f"- Semantic Scholar: {row.get('s2_url') or 'n/a'}",
                "- Claude reject 或 accept 待审: 待审",
                f"- summary: {row.get('summary', '')[:700]}",
                "",
            ]
        )
    lines.extend(["## 未通过筛选", ""])
    for row in rejected[:30]:
        lines.append(f"- {row.get('title')} ({row.get('arxiv_id', 'n/a')}): {row.get('reason')}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run quant arxiv first-pass screening")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--keywords-file", type=Path)
    parser.add_argument("--max-per-keyword", type=int, default=6)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows, rejected = collect(read_keywords(args.keywords_file), args.max_per_keyword)
    rendered = render(rows, rejected, args.date)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
