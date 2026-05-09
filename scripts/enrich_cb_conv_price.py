#!/usr/bin/env python3
"""Enrich cb_basic.parquet with correct conv_price.

eastmoney 单只 cov_info 字段:
  CONVERT_STOCK_PRICE   -> 实际是当前正股价 (字段命名误导, 不是转股价!)
  TRANSFER_PRICE        -> 当前转股价 (经过 下修 调整). 已退市 CB 此字段为 None
  INITIAL_TRANSFER_PRICE -> 发行时的初始转股价

正确优先级 (3 步 fallback):
  1. eastmoney TRANSFER_PRICE (在跑的 CB 都有, 含下修)
  2. jisilu bond_cb_adj_logs_jsl 下修历史最新一行 "下修后转股价"
     (适用 已退市 + 有下修过 的 CB; 接口列名:
      ['转债名称','股东大会日','下修前转股价','下修后转股价','新转股价生效日期','下修底价'],
      行序按生效日降序, iloc[0] 即最新)
  3. INITIAL_TRANSFER_PRICE (已退市 + 没下修过 的 CB, 恒等于初始)

跑法 (只对 step 1 失败的 CB 调 jsl, ~1-3 min):
    python scripts/enrich_cb_conv_price.py
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "cb_warehouse"

# jsl 反爬限流: 每次调用前 sleep
JSL_REQUEST_INTERVAL = 0.15


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v or v in ("-", "None", "nan", "NaN"):
            return None
    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def fetch_em_step1(code: str) -> Tuple[Optional[float], Optional[float]]:
    """Step 1+3 from eastmoney: 返回 (transfer_price, initial_transfer_price).

    都可能是 None. 单只接口返回 1 行, 已退市 CB 的 TRANSFER_PRICE 为 None
    但 INITIAL_TRANSFER_PRICE 通常有值.
    """
    import akshare as ak
    try:
        df = ak.bond_zh_cov_info(symbol=code)
        if df is None or df.empty:
            return None, None
        row = df.iloc[0]
        tp = _to_float(row.get("TRANSFER_PRICE"))
        itp = _to_float(row.get("INITIAL_TRANSFER_PRICE"))
        return tp, itp
    except Exception:
        return None, None


def fetch_jsl_latest(code: str) -> Optional[float]:
    """Step 2: jisilu 下修历史最新一行 "下修后转股价".

    接口返回列: ['转债名称','股东大会日','下修前转股价','下修后转股价','新转股价生效日期','下修底价']
    行序按生效日降序 (iloc[0] 即最新). 无下修则 df.empty.
    """
    import akshare as ak
    time.sleep(JSL_REQUEST_INTERVAL)  # jsl 反爬
    try:
        df = ak.bond_cb_adj_logs_jsl(symbol=code)
        if df is None or df.empty:
            return None
        # 防御性: 如果列名变了, 抛 KeyError 让外层 except 兜
        col = "下修后转股价"
        if col not in df.columns:
            return None
        # 行序通常已经是降序, 但保险起见再排一次
        if "新转股价生效日期" in df.columns:
            df = df.copy()
            df["_eff"] = pd.to_datetime(df["新转股价生效日期"], errors="coerce")
            df = df.sort_values("_eff", ascending=False)
        return _to_float(df.iloc[0][col])
    except Exception:
        return None


def main():
    path = WAREHOUSE / "cb_basic.parquet"
    if not path.exists():
        print("FATAL: cb_basic.parquet 不存在")
        sys.exit(1)

    df = pd.read_parquet(path)
    total = len(df)
    print(f"loaded {total} CBs")

    # ------------------------------------------------------------------
    # 第 1 轮: 并发拉 eastmoney (TRANSFER_PRICE + INITIAL_TRANSFER_PRICE)
    # ------------------------------------------------------------------
    print(f"step 1: fetch eastmoney TRANSFER_PRICE for {total} CBs (workers=8)...")
    t0 = time.time()
    em_tp: dict[str, Optional[float]] = {}
    em_itp: dict[str, Optional[float]] = {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_em_step1, code): code for code in df["code"]}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            tp, itp = fut.result()
            em_tp[code] = tp
            em_itp[code] = itp
            if i % 100 == 0 or i == total:
                elapsed = time.time() - t0
                print(
                    f"  [{i}/{total}] step1 elapsed={elapsed:.0f}s",
                    flush=True,
                )

    n1 = sum(1 for v in em_tp.values() if v is not None)
    step1_failed = [c for c, v in em_tp.items() if v is None]
    print(f"step 1 完成: TRANSFER_PRICE 拿到 {n1}/{total}, 失败 {len(step1_failed)}, "
          f"耗时 {time.time()-t0:.0f}s")

    # ------------------------------------------------------------------
    # 第 2 轮: 只对 step 1 失败的 CB 串行调 jsl (反爬限流, 不并发)
    # ------------------------------------------------------------------
    print(f"step 2: query jisilu adj_logs for {len(step1_failed)} CBs (sequential, "
          f"interval={JSL_REQUEST_INTERVAL}s)...")
    t1 = time.time()
    jsl_price: dict[str, Optional[float]] = {}
    n2 = 0
    initial_only_codes: list[str] = []

    for i, code in enumerate(step1_failed, 1):
        v = fetch_jsl_latest(code)
        jsl_price[code] = v
        if v is not None:
            n2 += 1
            tag = "jsl_filled"
        else:
            initial_only_codes.append(code)
            tag = "initial_only"
        if i % 25 == 0 or i == len(step1_failed) or i <= 5:
            elapsed = time.time() - t1
            eta = elapsed / i * (len(step1_failed) - i) if i > 0 else 0
            print(
                f"  [{i}/{len(step1_failed)}] code={code} {tag} "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                flush=True,
            )

    print(f"step 2 完成: jsl 救活 {n2}/{len(step1_failed)}, 耗时 {time.time()-t1:.0f}s")

    # ------------------------------------------------------------------
    # 第 3 轮: 仍失败的, 用 INITIAL_TRANSFER_PRICE
    # ------------------------------------------------------------------
    n3 = sum(1 for c in initial_only_codes if em_itp.get(c) is not None)
    n_unfilled = sum(1 for c in initial_only_codes if em_itp.get(c) is None)

    # ------------------------------------------------------------------
    # 合并: 逐行写入 conv_price
    # ------------------------------------------------------------------
    new_prices: dict[str, float] = {}
    for code in df["code"]:
        v = em_tp.get(code)
        if v is None:
            v = jsl_price.get(code)
        if v is None:
            v = em_itp.get(code)
        if v is not None:
            new_prices[code] = float(v)

    # 如果仍 None, 保留原 conv_price
    df["conv_price_new"] = df["code"].map(new_prices)
    df["conv_price"] = df["conv_price_new"].fillna(df["conv_price"])
    df = df.drop(columns=["conv_price_new"])

    # ------------------------------------------------------------------
    # 总结
    # ------------------------------------------------------------------
    print("=" * 60)
    print(f"全部 {total} 只")
    print(f"  step 1 (TRANSFER_PRICE) 拿到: {n1} 只")
    print(f"  step 1 失败 -> step 2 (jsl 下修历史) 救活: {n2} 只 (这些是之前错的)")
    print(f"  step 1+2 都失败 -> step 3 (用初始值): {n3} 只 (这些可能仍错, 但没办法)")
    if n_unfilled:
        print(f"  3 步全失败 (保留原值): {n_unfilled} 只")
    print(f"NaN conv_price: {df['conv_price'].isna().sum()}/{len(df)}")
    print("=" * 60)

    df.to_parquet(path, index=False)
    print(f"saved -> {path}")


if __name__ == "__main__":
    main()
