#!/usr/bin/env python3
"""并发拉正股日线 (raw + qfq).

akshare stock_zh_a_daily 单线程 ~1.8s/call. 用线程池并发 8x 加速到 ~12 min.

输入: data/cb_warehouse/cb_basic.parquet (取 stk_code 唯一列表)
输出:
  data/cb_warehouse/stk_daily.parquet      不复权
  data/cb_warehouse/stk_daily_qfq.parquet  前复权 (BS 公式用)
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "cb_warehouse"


def fetch_one(stk_code: str, adjust: str = "qfq") -> Optional[pd.DataFrame]:
    """单只正股日线."""
    import akshare as ak
    if not stk_code or not stk_code.isdigit() or len(stk_code) != 6:
        return None
    sym = f"sh{stk_code}" if stk_code.startswith("6") else f"sz{stk_code}"
    try:
        df = ak.stock_zh_a_daily(symbol=sym, adjust=adjust)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["stk_code"] = stk_code
        df["ts_code"] = f"{stk_code}.SH" if stk_code.startswith("6") else f"{stk_code}.SZ"
        df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
        cols = ["ts_code", "stk_code", "trade_date", "open", "high", "low", "close", "volume"]
        cols = [c for c in cols if c in df.columns]
        return df[cols]
    except Exception:
        return None


def build_parallel(codes: list[str], adjust: str, max_workers: int = 8) -> pd.DataFrame:
    """并发拉, 失败容忍."""
    label = "qfq" if adjust == "qfq" else "raw"
    n = len(codes)
    print(f"[{label}] 并发拉 {n} 只 (workers={max_workers})...", flush=True)
    t0 = time.time()
    n_ok = 0
    n_fail = 0
    all_dfs = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_one, c, adjust): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            df = fut.result()
            if df is None or df.empty:
                n_fail += 1
            else:
                all_dfs.append(df)
                n_ok += 1

            if i % 100 == 0:
                elapsed = time.time() - t0
                eta = elapsed / i * (n - i)
                print(
                    f"  [{label}][{i}/{n}] ok={n_ok} fail={n_fail} "
                    f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                    flush=True,
                )

    if not all_dfs:
        return pd.DataFrame()
    merged = pd.concat(all_dfs, ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_code", "trade_date"]).sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)
    elapsed = time.time() - t0
    print(
        f"[{label}] 完成: ok={n_ok} fail={n_fail} 总行 {len(merged):,}, "
        f"耗时 {elapsed:.0f}s",
        flush=True,
    )
    return merged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--qfq-only", action="store_true", help="只拉 qfq, 跳过 raw")
    parser.add_argument("--raw-only", action="store_true", help="只拉 raw")
    args = parser.parse_args()

    basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet")
    codes = (
        basic["stk_code"]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
        .tolist()
    )
    codes = [c for c in codes if c.isdigit() and len(c) == 6]
    print(f"唯一正股: {len(codes)}", flush=True)

    if not args.raw_only:
        df_qfq = build_parallel(codes, adjust="qfq", max_workers=args.workers)
        if not df_qfq.empty:
            df_qfq.to_parquet(WAREHOUSE / "stk_daily_qfq.parquet", index=False)
            print(f"  -> stk_daily_qfq.parquet ({len(df_qfq):,} 条)", flush=True)

    if not args.qfq_only:
        df_raw = build_parallel(codes, adjust="", max_workers=args.workers)
        if not df_raw.empty:
            df_raw.to_parquet(WAREHOUSE / "stk_daily.parquet", index=False)
            print(f"  -> stk_daily.parquet ({len(df_raw):,} 条)", flush=True)


if __name__ == "__main__":
    main()
