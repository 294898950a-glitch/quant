#!/usr/bin/env python3
"""Extend the cninfo convertible-bond announcement archive from a given date.

The full fetch on hkvm (~/projects/cb_announcement_fetch/fetch_cb_announcements.py) records a
(stock, keyword) pair as done once and never asks again, so its archive stops on the day it ran
(2026-08-29). This script asks again, only for a date range and only for stocks that still have a
bond listed in that range, and writes the same record format to a separate file. Downstream
builders read both files and de-duplicate.

The full fetch ran on hkvm. On 2026-10-07 no Python there had akshare installed any more, so the first
incremental run was made from a workstation at the same request pace:

    python3 fetch_cb_announcements_incremental.py --cb-basic data/cb_basic.parquet \
        --since 20260825 --output out/cb_announcements_incr.jsonl

Re-running with the same --output skips (stock, keyword, since) triples already completed.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import akshare as ak  # imported up front: a missing library must stop the run, not be logged per request
import pandas as pd

KEYWORDS = ["赎回", "回售", "下修"]
SLEEP_BASE = 1.2
SLEEP_JITTER = 0.8
MAX_RETRIES = 3


def fetch_one(stk_code: str, keyword: str, start_date: str, end_date: str) -> pd.DataFrame:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return ak.stock_zh_a_disclosure_report_cninfo(
                symbol=stk_code, market="沪深京", keyword=keyword, category="可转债",
                start_date=start_date, end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2.0 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def stocks_to_ask(cb_basic: pd.DataFrame, since: str) -> pd.DataFrame:
    """Stocks with at least one bond that had not left the market before `since`."""
    b = cb_basic.dropna(subset=["stk_code", "list_date"])
    still_listed = b["delist_date"].isna() | (b["delist_date"].astype(str) >= since)
    b = b[still_listed]
    return (
        b.groupby("stk_code")
        .agg(ts_codes=("ts_code", lambda s: sorted(set(s))),
             bond_names=("bond_short_name", lambda s: sorted(set(s.dropna()))))
        .reset_index()
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cb-basic", type=Path, required=True)
    p.add_argument("--since", required=True, help="YYYYMMDD, first announcement date to ask for")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    groups = stocks_to_ask(pd.read_parquet(args.cb_basic), args.since)
    if args.limit:
        groups = groups.head(args.limit)
    done: set[tuple[str, str, str]] = set()
    if args.output.exists():
        with args.output.open(encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("record_type") == "batch_marker":
                    done.add((rec["stk_code"], rec["keyword"], rec.get("since", "")))
    today = time.strftime("%Y%m%d")
    print(f"{len(groups)} stocks x {len(KEYWORDS)} keywords since {args.since}; {len(done)} done -> {args.output}", flush=True)

    n_records = n_failed = n_done = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as out:
        for i, row in enumerate(groups.itertuples(index=False)):
            for kw in KEYWORDS:
                if (row.stk_code, kw, args.since) in done:
                    continue
                try:
                    df = fetch_one(row.stk_code, kw, args.since, today)
                except Exception as exc:  # noqa: BLE001
                    n_failed += 1
                    out.write(json.dumps({"record_type": "fetch_failure", "stk_code": row.stk_code, "keyword": kw,
                                          "since": args.since, "error": str(exc)[:300],
                                          "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")
                    out.flush()
                    time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))
                    continue
                for _, r in df.iterrows():
                    out.write(json.dumps({
                        "record_type": "announcement", "stk_code": row.stk_code,
                        "candidate_ts_codes": list(row.ts_codes), "candidate_bond_names": list(row.bond_names),
                        "keyword": kw, "ann_title": r.get("公告标题"), "ann_datetime": str(r.get("公告时间")),
                        "ann_url": r.get("公告链接"),
                    }, ensure_ascii=False) + "\n")
                    n_records += 1
                out.write(json.dumps({"record_type": "batch_marker", "stk_code": row.stk_code, "keyword": kw,
                                      "since": args.since, "until": today, "n_rows": int(len(df)),
                                      "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")
                out.flush()
                n_done += 1
                if n_done % 50 == 0:
                    print(f"  {n_done} pairs done ({i + 1}/{len(groups)} stocks), {n_records} records, {n_failed} failed", flush=True)
                time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))
    print(f"finished: {n_done} pairs, {n_records} records, {n_failed} failed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
