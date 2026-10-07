#!/usr/bin/env python3
"""Collect the wording of each bond's down-revision clause from the issuer's own announcements.

No structured source carries the down-revision trigger (window, days, ratio). But every notice an
issuer publishes about a possible or declined revision restates the clause from the prospectus. This
step downloads such notices (PDFs on static.cninfo.com.cn), and keeps the sentence that states the
clause. Reading numbers out of the sentence is cb_market.contracts.parse_down_revision_clause, applied
in scripts/build_cb_contract_terms.py.

Two routes, both writing data/cb_warehouse/cb_revision_clause_text.jsonl (append-only; the reader takes,
per bond, a record that has a sentence):
  --announcements      issuer notices about a possible or declined revision (fast; only bonds that had one)
  --listing-documents  for the bonds still missing, the listing announcement published just before the
                       bond listed, found through the cninfo search by stock code and date

Input of the first route: the cninfo announcement archive (the files scripts/build_cb_events.py reads).
A notice counts for a bond only if its title contains that bond's own short name and it falls inside
the bond's listed life.

    python3 scripts/fetch_cb_revision_clause_text.py --announcements cb_announcements.jsonl cb_announcements_incr.jsonl
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cb_market.contracts import find_down_revision_sentence  # noqa: E402
from scripts.build_cb_call_history import load_notices  # noqa: E402
from scripts.build_cb_warehouse import WAREHOUSE_DIR  # noqa: E402

OUTPUT = WAREHOUSE_DIR / "cb_revision_clause_text.jsonl"
MAX_NOTICES_PER_BOND = 4
LISTING_DOC_LEAD_DAYS = 20
SEARCH_SLEEP = 1.0
SLEEP_BASE = 0.4
SLEEP_JITTER = 0.3
# notices that restate the clause in full come first
PREFERRED = ("预计", "可能", "不向下修正", "不下修", "提议", "董事会")


def pdf_text(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
    r.raise_for_status()
    pages = PdfReader(io.BytesIO(r.content)).pages
    return re.sub(r"\s+", "", "".join(p.extract_text() or "" for p in pages))


def pdf_url_of(ann_url: str) -> str | None:
    """cninfo detail link -> the PDF it shows (static.cninfo.com.cn/finalpage/<date>/<announcementId>.PDF)."""
    aid = re.search(r"announcementId=(\d+)", ann_url or "")
    day = re.search(r"announcementTime=(\d{4}-\d{2}-\d{2})", ann_url or "")
    return f"https://static.cninfo.com.cn/finalpage/{day.group(1)}/{aid.group(1)}.PDF" if aid and day else None


def candidates(announcements: list[Path]) -> dict[str, list[dict]]:
    """Per bond, the down-revision notices that name it, most useful first.

    Attribution (own short name in the title, inside the bond's listed life) is load_notices, the same
    function the event table uses.
    """
    daily = pd.read_parquet(WAREHOUSE_DIR / "cb_daily.parquet", columns=["ts_code", "trade_date"])
    life = daily.groupby("ts_code")["trade_date"].agg(first_trade="min", last_trade="max").reset_index()
    basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet", columns=["ts_code", "bond_short_name"])
    notices = load_notices(announcements, life.merge(basic, on="ts_code"), keyword="下修", classify=lambda t: "")
    notices = notices[notices["named_in_title"]]
    out: dict[str, list[dict]] = {}
    for r in notices.itertuples(index=False):
        url = pdf_url_of(r.url)
        if url:
            out.setdefault(r.ts_code, []).append({"title": r.title, "pdf_url": url, "ann_url": r.url, "ann_date": r.ann_date})
    for items in out.values():
        # preferred first, and within a group the newest first (two stable sorts)
        items.sort(key=lambda x: x["ann_date"], reverse=True)
        items.sort(key=lambda x: not any(k in x["title"] for k in PREFERRED))
    return out


def listing_document(stk_code: str, list_date: str) -> dict | None:
    """The bond's listing announcement (上市公告书): published by the issuer in the two weeks before listing."""
    import akshare as ak  # only this route needs the cninfo search

    found = ak.stock_zh_a_disclosure_report_cninfo(symbol=stk_code, market="沪深京", keyword="上市公告书", category="",
                                                    start_date="20060101", end_date=time.strftime("%Y%m%d"))
    listed = pd.Timestamp(list_date)
    best = None
    for r in found.itertuples(index=False):
        title = re.sub(r"</?em>", "", str(r.公告标题))
        day = pd.Timestamp(str(r.公告时间)[:10])
        if re.search("转换公司债券|可转债|转债", title) and not re.search("更正|提示性", title) and pd.Timedelta(0) <= listed - day <= pd.Timedelta(days=LISTING_DOC_LEAD_DAYS):
            url = pdf_url_of(str(r.公告链接))
            if url and (best is None or day > pd.Timestamp(best["ann_date"])):
                best = {"title": title, "pdf_url": url, "ann_url": str(r.公告链接), "ann_date": day.strftime("%Y%m%d")}
    return best


def read_done(require_sentence: bool) -> set[str]:
    done: set[str] = set()
    if OUTPUT.exists():
        with OUTPUT.open(encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                # a failed search is not an answer; ask again next run
                if rec.get("sentence") or (not require_sentence) or (
                        rec.get("origin") == "listing_document" and not rec.get("search_error")):
                    done.add(rec["ts_code"])
    return done


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--announcements", type=Path, nargs="+",
                   help="cninfo archive files (cb_announcements.jsonl and incremental files)")
    p.add_argument("--listing-documents", action="store_true",
                   help="for bonds still without a clause, read it from the listing announcement instead")
    p.add_argument("--shard", default="0/1", help="i/n, to run n copies side by side")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    if bool(args.announcements) == args.listing_documents:
        p.error("give either --announcements or --listing-documents")
    shard_i, shard_n = (int(x) for x in args.shard.split("/"))

    if args.listing_documents:
        basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet", columns=["ts_code", "stk_code", "list_date"]).dropna()
        done = read_done(require_sentence=True)
        todo = {b.ts_code: (str(b.stk_code)[:6], str(b.list_date)[:8]) for b in basic.itertuples(index=False)}
    else:
        done = read_done(require_sentence=False)
        todo = candidates(args.announcements)
    codes = [ts for ts in sorted(todo) if ts not in done][shard_i::shard_n]
    if args.limit:
        codes = codes[: args.limit]
    print(f"{len(todo)} bonds in scope; {len(done)} done; fetching {len(codes)}", flush=True)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    n_found = n_missing = 0
    with OUTPUT.open("a", encoding="utf-8") as out:
        for i, ts in enumerate(codes):
            origin = "listing_document" if args.listing_documents else "revision_notice"
            rec: dict = {"ts_code": ts, "origin": origin, "sentence": None, "tried": []}
            if args.listing_documents:
                try:
                    doc = listing_document(*todo[ts])
                except Exception as exc:  # noqa: BLE001
                    doc, rec["search_error"] = None, str(exc)[:200]
                items = [doc] if doc else []
                time.sleep(SEARCH_SLEEP + random.uniform(0, SLEEP_JITTER))
            else:
                items = todo[ts][:MAX_NOTICES_PER_BOND]
            for item in items:
                attempt = {"pdf_url": item["pdf_url"], "ann_date": item["ann_date"], "title": item["title"]}
                try:
                    sentence = find_down_revision_sentence(pdf_text(item["pdf_url"]))
                except Exception as exc:  # noqa: BLE001
                    attempt["error"] = str(exc)[:200]
                    sentence = None
                rec["tried"].append(attempt)
                time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))
                if sentence:
                    rec.update(sentence=sentence, pdf_url=item["pdf_url"], ann_url=item["ann_url"],
                               ann_date=item["ann_date"], title=item["title"])
                    break
            n_found += bool(rec["sentence"])
            n_missing += not rec["sentence"]
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(codes)} bonds, clause found {n_found}, not found {n_missing}", flush=True)
    print(f"finished: clause found {n_found}, not found {n_missing}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
