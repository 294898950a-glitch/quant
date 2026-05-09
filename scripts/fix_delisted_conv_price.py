#!/usr/bin/env python3
"""精简版:只对已退市的 CB 用 jsl 下修历史修 conv_price.

跟 enrich_cb_conv_price.py 不同:
- 不重新拉 eastmoney(在跑的 338 只信任原 parquet)
- 只对 674 只已退市 CB 调 jsl
- 串行但不限 timeout(自己写进度, 不被 tail 吞输出)

跑法: .venv/bin/python scripts/fix_delisted_conv_price.py
"""
import sys
import time
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "data" / "cb_warehouse" / "cb_basic.parquet"

JSL_INTERVAL = 0.15


def fetch_jsl_latest(code_6digit: str):
    """jsl 下修历史最新一行 '下修后转股价'."""
    import akshare as ak
    try:
        df = ak.bond_cb_adj_logs_jsl(symbol=code_6digit)
        if df is None or df.empty:
            return None
        if "下修后转股价" not in df.columns:
            return None
        if "新转股价生效日期" in df.columns:
            df = df.copy()
            df["_eff"] = pd.to_datetime(df["新转股价生效日期"], errors="coerce")
            df = df.sort_values("_eff", ascending=False)
        v = df.iloc[0]["下修后转股价"]
        if pd.isna(v):
            return None
        return float(v)
    except Exception as exc:
        return None


def main():
    df = pd.read_parquet(PARQUET)
    delisted = df[df["delist_date"].notna()].copy()
    n = len(delisted)
    print(f"已退市 CB 数: {n} (跳过 {len(df) - n} 在跑的)", flush=True)

    t0 = time.time()
    updated = 0
    no_record = 0
    same_as_original = 0

    for i, row in enumerate(delisted.itertuples(), 1):
        code_6 = str(row.code).zfill(6) if hasattr(row, "code") else str(row.ts_code).split(".")[0]
        time.sleep(JSL_INTERVAL)
        jsl_price = fetch_jsl_latest(code_6)
        if jsl_price is None:
            no_record += 1
        else:
            old = float(row.conv_price) if pd.notna(row.conv_price) else None
            if old is None or abs(old - jsl_price) > 0.01:
                # 真的改了
                df.loc[df["ts_code"] == row.ts_code, "conv_price"] = jsl_price
                updated += 1
            else:
                same_as_original += 1
        if i % 50 == 0 or i == n or i <= 3:
            elapsed = time.time() - t0
            eta = elapsed / i * (n - i) if i > 0 else 0
            print(
                f"  [{i}/{n}] elapsed={elapsed:.0f}s eta={eta:.0f}s "
                f"updated={updated} no_record={no_record} same={same_as_original}",
                flush=True,
            )

    df.to_parquet(PARQUET, index=False)
    print("=" * 60, flush=True)
    print(f"完成. 总耗时 {time.time()-t0:.0f}s", flush=True)
    print(f"  调 jsl 次数:   {n}", flush=True)
    print(f"  jsl 救活并修改: {updated}  ← 这些是之前 conv_price 错的", flush=True)
    print(f"  jsl 有记录但跟原值一致: {same_as_original}", flush=True)
    print(f"  jsl 无记录(没下修过, 用原值): {no_record}", flush=True)
    print("=" * 60, flush=True)
    print(f"saved -> {PARQUET}", flush=True)


if __name__ == "__main__":
    main()
