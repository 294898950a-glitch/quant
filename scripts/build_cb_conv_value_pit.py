#!/usr/bin/env python3
"""Build cb_conv_value_pit.parquet: point-in-time daily conversion value per CB.

cb_basic.conv_price holds only the LATEST conversion price (after every later
down-revision / dividend adjustment). Applying it to history is look-ahead.
eastmoney 可转债-价值分析 publishes the conversion value that was true on each
trading day, for listed and delisted bonds alike. This script snapshots it.

Output columns:
  ts_code, code, trade_date (YYYYMMDD), em_close, bond_value, conv_value,
  bond_prem, conv_prem

跑法 (~3 min, 6 threads):
    python scripts/build_cb_conv_value_pit.py
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "cb_warehouse"
COLUMNS = ["trade_date", "em_close", "bond_value", "conv_value", "bond_prem", "conv_prem"]


def fetch_one(code: str, cache_dir: Path | None, retries: int = 4) -> tuple[str, pd.DataFrame | None, str]:
    cache = cache_dir / f"{code}.parquet" if cache_dir else None
    if cache is not None and cache.exists():
        df = pd.read_parquet(cache)
        df = df.drop(columns=[c for c in ("code",) if c in df.columns])
        df.columns = COLUMNS
        return code, df, "cached"
    import akshare as ak

    err = ""
    for attempt in range(retries):
        try:
            df = ak.bond_zh_cov_value_analysis(symbol=code)
            df.columns = COLUMNS
            if cache is not None:
                df.to_parquet(cache, index=False)
            return code, df, "fetched"
        except Exception as exc:  # eastmoney returns None for pre-2010 separable bonds
            err = repr(exc)[:160]
            time.sleep(1.5 * (attempt + 1))
    return code, None, err


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--basic", type=Path, default=WAREHOUSE / "cb_basic.parquet")
    p.add_argument("--output", type=Path, default=WAREHOUSE / "cb_conv_value_pit.parquet")
    p.add_argument("--cache-dir", type=Path, default=None, help="per-bond parquet cache; reused when present")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    basic = pd.read_parquet(args.basic)
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    ts_by_code = dict(zip(basic["code"].astype(str), basic["ts_code"].astype(str)))
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda c: fetch_one(c, args.cache_dir), list(ts_by_code)))

    frames, missing = [], {}
    for code, df, status in results:
        if df is None or df.empty:
            missing[code] = status
            continue
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        df.insert(0, "code", code)
        df.insert(0, "ts_code", ts_by_code[code])
        frames.append(df)
    out = pd.concat(frames, ignore_index=True).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    out.to_parquet(args.output, index=False)
    meta = {
        "name": "cb_conv_value_pit",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "eastmoney bond_zh_cov_value_analysis via akshare",
        "rows": int(len(out)),
        "bonds": int(out["ts_code"].nunique()),
        "start": str(out["trade_date"].min()),
        "end": str(out["trade_date"].max()),
        "missing_codes": missing,
    }
    args.output.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[conv_value_pit] rows={len(out)} bonds={meta['bonds']} missing={len(missing)} -> {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
