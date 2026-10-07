#!/usr/bin/env python3
"""Keep 1-minute and 5-minute bars of every listed convertible bond and its stock, one file per trading day.

Free sources serve intraday bars only for a short trailing window (Tencent: the last 320 bars, i.e.
about 1.3 trading days of 1-minute bars and 6.7 days of 5-minute bars; Eastmoney serves longer 5-minute
windows but drops connections), so intraday history cannot be fetched later: it exists only if this
runs every trading day after the close.

    python3 fetch_cb_intraday_bars.py --cb-basic data/cb_basic.parquet --out-dir out/intraday

Writes <out-dir>/<1min|5min>/<YYYYMMDD>.csv.gz, all securities of a day in one file. A day already on
disk is never rewritten, so a missed run loses 1-minute bars for that day, and 5-minute bars only once
the gap exceeds five trading days. <out-dir>/runs.jsonl records each run: asked / answered / empty /
failed, and which day files it wrote.

Volume is in lots as the source gives it; the source has no turnover amount.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import pandas as pd
import requests

URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
PERIODS = {"1min": "m1", "5min": "m5"}
WINDOW = 320
COLUMNS = ["bar_time", "open", "close", "high", "low", "volume"]
SLEEP_BASE = 0.15
SLEEP_JITTER = 0.15
MAX_RETRIES = 3


def symbol(code: str, exchange: str | None = None) -> str:
    """Tencent symbol. Bonds carry their exchange; a stock's is read from its code."""
    if exchange is not None:
        return exchange.lower() + code
    if code.startswith("6"):
        return "sh" + code
    return ("bj" if code.startswith(("4", "8", "92")) else "sz") + code


def fetch_bars(session: requests.Session, sym: str, period: str) -> pd.DataFrame:
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(URL, params={"param": f"{sym},{period},,{WINDOW}"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            node = (r.json().get("data") or {}).get(sym) or {}
            return pd.DataFrame([k[:6] for k in node.get(period) or []], columns=COLUMNS)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def securities(cb_basic: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(kind, code, symbol) for bonds not known to be delisted, and their stocks."""
    live = cb_basic[cb_basic["delist_date"].isna() & cb_basic["list_date"].notna()]
    out = [("bond", ts, symbol(ts[:6], ts[-2:])) for ts in sorted(live["ts_code"])]
    out += [("stock", c, symbol(c)) for c in sorted(set(live["stk_code"].dropna().astype(str).str[:6]))]
    return out


def complete_days(bars: pd.DataFrame) -> pd.DataFrame:
    """Drop the oldest day of a full window: the window cuts it off part-way through."""
    bars = bars.assign(trade_date=bars["bar_time"].str[:8])
    if len(bars) >= WINDOW:
        bars = bars[bars["trade_date"] != bars["trade_date"].min()]
    return bars


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cb-basic", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    todo = securities(pd.read_parquet(args.cb_basic))
    if args.limit:
        todo = todo[: args.limit]
    session = requests.Session()
    today, before_close = time.strftime("%Y%m%d"), time.strftime("%H%M") < "1530"
    run: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "asked": len(todo)}
    n_answered_total = n_failed_total = 0
    for name, period in PERIODS.items():
        frames, n_empty, failed = [], 0, []
        for i, (kind, code, sym) in enumerate(todo):
            try:
                bars = fetch_bars(session, sym, period)
            except Exception as exc:  # noqa: BLE001
                failed.append({"code": code, "error": str(exc)[:120]})
                continue
            if bars.empty:
                n_empty += 1
            else:
                bars = complete_days(bars)
                bars.insert(0, "code", code)
                bars.insert(0, "kind", kind)
                frames.append(bars)
            if (i + 1) % 200 == 0:
                print(f"  {name}: {i + 1}/{len(todo)}, empty {n_empty}, failed {len(failed)}", flush=True)
            time.sleep(SLEEP_BASE + random.uniform(0, SLEEP_JITTER))
        written: dict[str, int] = {}
        out_dir = args.out_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        if frames:
            allbars = pd.concat(frames, ignore_index=True)
            # A day file must be the whole market's day. A security that was halted, or stopped trading
            # months ago, answers with a window reaching further back than everyone else's; days that
            # only such securities cover are not written.
            per_day = allbars.groupby("trade_date")["code"].nunique()
            for day, g in allbars.groupby("trade_date"):
                path = out_dir / f"{day}.csv.gz"
                if per_day[day] < per_day.max() / 2 or path.exists() or (day == today and before_close):
                    continue
                g.drop(columns="trade_date").to_csv(path, index=False, compression="gzip")
                written[day] = int(len(g))
        run[name] = {"answered": len(frames), "empty": n_empty, "failed": len(failed),
                     "failures": failed[:10], "days_written": written}
        n_answered_total += len(frames)
        n_failed_total += len(failed)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(run, ensure_ascii=False) + "\n")
    print(json.dumps(run, ensure_ascii=False)[:1500], flush=True)
    # an outage must be visible to cron: nothing answered, or most requests failing, is a failed run
    return 1 if not n_answered_total or n_failed_total > len(todo) else 0


if __name__ == "__main__":
    raise SystemExit(main())
