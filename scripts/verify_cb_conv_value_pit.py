#!/usr/bin/env python3
"""独立核对 cb_conv_value_pit.parquet (东方财富) 与集思录.

两项核对:
  1. 水平: 集思录强赎表快照 (全部在市 CB 的 转股价 / 正股价) 算出的转股价值,
     对比东方财富最后一个交易日的转股价值.
  2. 历史: 集思录每一条下修记录 (下修前 / 下修后转股价, 生效日), 对比由东方财富
     转股价值反推的转股价 100 * 正股不复权收盘 / 转股价值 在生效日前后的取值.

输出:
  data/cb_warehouse/verification/cb_conv_value_pit_vs_jisilu.yaml
  data/cb_warehouse/verification/cb_conv_value_pit_revision_events.csv

跑法 (~4 min, 集思录限流):
    python scripts/verify_cb_conv_value_pit.py
"""
from __future__ import annotations

import argparse
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "cb_warehouse"
OUT_DIR = WAREHOUSE / "verification"
JSL_INTERVAL = 0.2
TOLERANCE = 0.01


def fetch_adj_log(code: str, cache_dir: Path | None) -> pd.DataFrame:
    cache = cache_dir / f"{code}.parquet" if cache_dir else None
    if cache is not None and cache.exists():
        return pd.read_parquet(cache)
    import akshare as ak

    for attempt in range(3):
        try:
            time.sleep(JSL_INTERVAL)
            df = ak.bond_cb_adj_logs_jsl(symbol=code)
            df = pd.DataFrame() if df is None else df.copy()
            df["code"] = code
            df = df.astype(str)
            if cache is not None:
                df.to_parquet(cache, index=False)
            return df
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return pd.DataFrame({"code": [code], "fetch_error": ["1"]})


def check_level(pit: pd.DataFrame) -> dict:
    import akshare as ak

    snap = ak.bond_cb_redeem_jsl()
    snap["code"] = snap["代码"].astype(str)
    snap["conv_value_jsl"] = (
        100.0 * pd.to_numeric(snap["正股价"], errors="coerce") / pd.to_numeric(snap["转股价"], errors="coerce")
    )
    last = pit[pit["conv_value"].notna()].sort_values("trade_date").groupby("code").tail(1)
    m = snap.merge(last[["code", "trade_date", "conv_value"]], on="code")
    diff = (m["conv_value_jsl"] / m["conv_value"] - 1.0).abs()
    return {
        "jisilu_listed_bonds": int(len(snap)),
        "matched_bonds": int(len(m)),
        "unmatched_not_in_cb_basic": int(len(snap) - len(m)),
        "eastmoney_dates": {str(k): int(v) for k, v in m["trade_date"].value_counts().items()},
        "max_abs_relative_diff": round(float(diff.max()), 6),
        "share_within_tolerance": round(float((diff < TOLERANCE).mean()), 4),
    }


def check_revisions(pit: pd.DataFrame, basic: pd.DataFrame, cache_dir: Path | None, workers: int) -> tuple[dict, pd.DataFrame]:
    codes = basic.loc[basic["list_date"] >= "20170101", "code"].astype(str).tolist()
    with ThreadPoolExecutor(workers) as pool:
        logs = pd.concat(list(pool.map(lambda c: fetch_adj_log(c, cache_dir), codes)), ignore_index=True)
    fetch_errors = int(logs["fetch_error"].notna().sum()) if "fetch_error" in logs else 0
    events = logs[logs["下修后转股价"].notna()].copy()
    events["effective"] = pd.to_datetime(events["新转股价生效日期"], errors="coerce").dt.strftime("%Y%m%d")
    events["jsl_before"] = pd.to_numeric(events["下修前转股价"], errors="coerce")
    events["jsl_after"] = pd.to_numeric(events["下修后转股价"], errors="coerce")
    events = events.dropna(subset=["effective", "jsl_before", "jsl_after"])

    raw = pd.read_parquet(WAREHOUSE / "stk_daily.parquet", columns=["stk_code", "trade_date", "close"])
    implied = pit.merge(basic[["code", "stk_code"]], on="code").merge(raw, on=["stk_code", "trade_date"])
    implied = implied[implied["conv_value"] > 0].sort_values(["code", "trade_date"])
    implied["conv_price"] = 100.0 * implied["close"] / implied["conv_value"]
    by_code = {code: g for code, g in implied.groupby("code")}

    rows = []
    for ev in events.itertuples():
        g = by_code.get(ev.code)
        if g is None:
            continue
        before = g[g["trade_date"] < ev.effective].tail(1)
        after = g[g["trade_date"] >= ev.effective].head(1)
        if before.empty or after.empty:
            continue
        rows.append({
            "code": ev.code, "effective": ev.effective,
            "jsl_before": ev.jsl_before, "em_before": round(float(before["conv_price"].iloc[0]), 4),
            "jsl_after": ev.jsl_after, "em_after": round(float(after["conv_price"].iloc[0]), 4),
        })
    cmp = pd.DataFrame(rows)
    cmp["diff_before"] = cmp["em_before"] / cmp["jsl_before"] - 1.0
    cmp["diff_after"] = cmp["em_after"] / cmp["jsl_after"] - 1.0
    ok_before = cmp["diff_before"].abs() < TOLERANCE
    ok_after = cmp["diff_after"].abs() < TOLERANCE
    summary = {
        "bonds_queried": len(codes),
        "fetch_errors": fetch_errors,
        "jisilu_revision_events": int(len(events)),
        "bonds_with_revisions": int(events["code"].nunique()),
        "events_compared": int(len(cmp)),
        "share_before_within_tolerance": round(float(ok_before.mean()), 4),
        "share_after_within_tolerance": round(float(ok_after.mean()), 4),
        "mismatched_events": cmp.loc[~(ok_before & ok_after), ["code", "effective", "jsl_before", "em_before",
                                                                 "jsl_after", "em_after"]].to_dict("records"),
    }
    return summary, cmp


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pit-path", type=Path, default=WAREHOUSE / "cb_conv_value_pit.parquet")
    p.add_argument("--cache-dir", type=Path, default=None, help="per-bond Jisilu log cache; reused when present")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)

    pit = pd.read_parquet(args.pit_path)
    basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet")
    level = check_level(pit)
    revisions, events = check_revisions(pit, basic, args.cache_dir, args.workers)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events.round(6).to_csv(OUT_DIR / "cb_conv_value_pit_revision_events.csv", index=False)
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "subject": str(args.pit_path.relative_to(ROOT) if args.pit_path.is_absolute() else args.pit_path),
        "reference": "jisilu via akshare (bond_cb_redeem_jsl, bond_cb_adj_logs_jsl)",
        "tolerance": TOLERANCE,
        "level_check": level,
        "revision_history_check": revisions,
    }
    (OUT_DIR / "cb_conv_value_pit_vs_jisilu.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
