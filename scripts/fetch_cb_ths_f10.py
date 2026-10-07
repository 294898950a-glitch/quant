#!/usr/bin/env python3
"""Download the Tonghuashun F10 pages of every convertible bond in cb_basic, as raw HTML.

Four pages per bond (basic.10jqka.com.cn/<code>/<page>.html):
  index     issue facts, current outstanding balance
  detail    ten largest holders, one table per reporting date
  grade     every rating action with its publication date
  dividend  cash-flow schedule with the outstanding balance on each payment date

This step only downloads. Parsing, and the check that a page really belongs to our bond (exchange
codes are reused: 127038 is also a 2014 enterprise bond), is scripts/build_cb_ths_tables.py.
Raw pages are kept so the tables can be rebuilt and any row traced back to what the site showed.

    python3 scripts/fetch_cb_ths_f10.py                 # resumes; skips pages already on disk
    python3 scripts/fetch_cb_ths_f10.py --refresh-live  # re-download bonds that are still listed
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
CB_BASIC = ROOT / "data" / "cb_warehouse" / "cb_basic.parquet"
RAW_DIR = ROOT / "data" / "cb_raw" / "ths_f10"
PAGES = ("index", "detail", "grade", "dividend")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
SLEEP_BASE = 0.5
SLEEP_JITTER = 0.4
MAX_RETRIES = 3


def raw_path(code: str, page: str) -> Path:
    return RAW_DIR / f"{code}_{page}.html.gz"


def fetch_page(session: requests.Session, code: str, page: str) -> bytes:
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(f"https://basic.10jqka.com.cn/{code}/{page}.html", headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refresh-live", action="store_true", help="re-download bonds with no delist_date")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--shard", default="0/1", help="i/n: take every n-th bond starting at i, to run n copies side by side")
    args = p.parse_args()
    shard_i, shard_n = (int(x) for x in args.shard.split("/"))

    basic = pd.read_parquet(CB_BASIC, columns=["ts_code", "delist_date"])
    basic = basic.sort_values("ts_code").iloc[shard_i::shard_n]
    if args.limit:
        basic = basic.head(args.limit)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RAW_DIR / f"fetch_log_{shard_i}of{shard_n}.jsonl"
    session = requests.Session()
    n_fetched = n_failed = n_skipped = 0
    with log_path.open("a", encoding="utf-8") as log:
        for i, row in enumerate(basic.itertuples(index=False)):
            code = row.ts_code[:6]
            live = pd.isna(row.delist_date)
            for page in PAGES:
                path = raw_path(code, page)
                if path.exists() and not (args.refresh_live and live):
                    n_skipped += 1
                    continue
                rec = {"ts_code": row.ts_code, "page": page, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                try:
                    body = fetch_page(session, code, page)
                    path.write_bytes(gzip.compress(body))
                    rec["bytes"] = len(body)
                    n_fetched += 1
                except Exception as exc:  # noqa: BLE001
                    rec["error"] = str(exc)[:200]
                    n_failed += 1
                log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log.flush()
                time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(basic)} bonds, fetched {n_fetched}, failed {n_failed}, skipped {n_skipped}", flush=True)
    print(f"finished: fetched {n_fetched}, failed {n_failed}, already on disk {n_skipped}", flush=True)
    return 1 if n_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
