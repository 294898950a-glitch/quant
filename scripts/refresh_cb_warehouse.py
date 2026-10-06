#!/usr/bin/env python3
"""Extend the CB warehouse to the latest trading day without rewriting history.

build_cb_warehouse.py rebuilds everything and overwrites cb_basic, including
the conv_price repairs made afterwards. This script only appends:

  cb_basic   existing rows kept as they are; newly listed bonds appended;
             contract_maturity_date recomputed for every row
  cb_call    not touched here; rebuild it with scripts/build_cb_call_history.py
  cb_daily   rows after the current last date, for bonds still trading then,
             plus full history of newly listed bonds
  stk_daily  raw rows after the current last date
  stk_daily_qfq  refreshed stocks are replaced whole, because a new dividend
             rescales the entire forward-adjusted series

跑法 (~10 min):
    python scripts/refresh_cb_warehouse.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_cb_warehouse import (  # noqa: E402
    WAREHOUSE_DIR,
    build_cb_basic_and_call,
    contract_maturity_date,
    collect_cb_universe,
    fetch_cb_daily_one,
    fetch_stock_daily_one,
)


def _fetch_stock_raw(code: str) -> pd.DataFrame | None:
    return fetch_stock_daily_one(code, "")


def _fetch_stock_qfq(code: str) -> pd.DataFrame | None:
    return fetch_stock_daily_one(code, "qfq")


def _fetch_all(fn, keys: list[str], workers: int, label: str) -> tuple[dict[str, pd.DataFrame], list[str]]:
    # Processes, not threads: the sina endpoints decode through an embedded JS engine that aborts under threads.
    with get_context("spawn").Pool(workers) as pool:
        frames = pool.map(fn, keys, chunksize=4)
    ok = {k: df for k, df in zip(keys, frames) if df is not None and not df.empty}
    failed = [k for k in keys if k not in ok]
    print(f"[refresh] {label}: ok={len(ok)} failed={len(failed)}", flush=True)
    return ok, failed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")
    daily = pd.read_parquet(WAREHOUSE_DIR / "cb_daily.parquet")
    raw = pd.read_parquet(WAREHOUSE_DIR / "stk_daily.parquet")
    qfq = pd.read_parquet(WAREHOUSE_DIR / "stk_daily_qfq.parquet")
    old_end = str(daily["trade_date"].max())

    universe = collect_cb_universe()
    if universe.empty:
        print("FATAL: empty universe", flush=True)
        return 1
    fresh_basic, _ = build_cb_basic_and_call(universe)
    em_rows = dict(zip(universe["code"], universe["_em_row"]))

    new_basic = fresh_basic[~fresh_basic["code"].isin(basic["code"])].copy()
    # build_cb_basic_and_call reads CONVERT_STOCK_PRICE, which is the stock price.
    new_basic["conv_price"] = [
        pd.to_numeric((em_rows.get(c) or {}).get("TRANSFER_PRICE"), errors="coerce")
        if (em_rows.get(c) or {}).get("TRANSFER_PRICE") is not None
        else pd.to_numeric((em_rows.get(c) or {}).get("INITIAL_TRANSFER_PRICE"), errors="coerce")
        for c in new_basic["code"]
    ]
    new_basic = new_basic[new_basic["list_date"].notna()]
    for frame in (basic, new_basic):
        frame["contract_maturity_date"] = [
            contract_maturity_date(v, t) for v, t in zip(frame["value_date"], frame["interest_rate_explain"])
        ]
    basic_out = pd.concat([basic, new_basic[basic.columns]], ignore_index=True)
    last_by_bond = daily.groupby("ts_code")["trade_date"].max()
    alive = set(last_by_bond[last_by_bond >= old_end].index)
    # A bond can be in cb_basic before it lists (the basic table carries future listing dates). It has no
    # daily rows yet, so it is neither "alive at the last date" nor "new to cb_basic": ask for it explicitly.
    never_priced = basic.loc[~basic["ts_code"].isin(set(daily["ts_code"])) & (basic["list_date"] > old_end), "code"]
    bond_codes = sorted(
        set(basic.loc[basic["ts_code"].isin(alive), "code"].astype(str))
        | set(never_priced.astype(str)) | set(new_basic["code"].astype(str))
    )
    cb_frames, cb_failed = _fetch_all(fetch_cb_daily_one, bond_codes, args.workers, "cb_daily")
    known = set(daily["ts_code"])
    added = []
    for df in cb_frames.values():
        ts = df["ts_code"].iloc[0]
        added.append(df[df["trade_date"] > old_end] if ts in known else df)
    daily_out = pd.concat([daily] + added, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
    daily_out = daily_out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    daily_out["vol"] = daily_out["vol"].astype("int64")

    stk_codes = sorted(set(
        basic_out.loc[basic_out["code"].astype(str).isin(bond_codes), "stk_code"].dropna().astype(str)
    ))
    raw_frames, raw_failed = _fetch_all(_fetch_stock_raw, stk_codes, args.workers, "stk_daily")
    qfq_frames, qfq_failed = _fetch_all(_fetch_stock_qfq, stk_codes, args.workers, "stk_daily_qfq")
    raw_known = set(raw["stk_code"])
    raw_end = str(raw["trade_date"].max())
    raw_added = [df[df["trade_date"] > raw_end] if code in raw_known else df for code, df in raw_frames.items()]
    raw_out = pd.concat([raw] + raw_added, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
    raw_out = raw_out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    qfq_out = pd.concat([qfq[~qfq["stk_code"].isin(qfq_frames)]] + list(qfq_frames.values()), ignore_index=True)
    qfq_out = qfq_out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    basic_out.to_parquet(WAREHOUSE_DIR / "cb_basic.parquet", index=False)
    daily_out.to_parquet(WAREHOUSE_DIR / "cb_daily.parquet", index=False)
    raw_out.to_parquet(WAREHOUSE_DIR / "stk_daily.parquet", index=False)
    qfq_out.to_parquet(WAREHOUSE_DIR / "stk_daily_qfq.parquet", index=False)

    new_end = str(daily_out["trade_date"].max())
    reached = daily_out[daily_out["trade_date"] > old_end].groupby("ts_code")["trade_date"].max()
    meta = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "previous_end": old_end,
        "new_end": new_end,
        "new_bonds": int(len(new_basic)),
        "bonds_requested": len(bond_codes),
        "bonds_with_new_rows": int(len(reached)),
        "cb_daily_rows_added": int(len(daily_out) - len(daily)),
        "stocks_requested": len(stk_codes),
        "failed": {"cb_daily": cb_failed, "stk_daily": raw_failed, "stk_daily_qfq": qfq_failed},
    }
    (WAREHOUSE_DIR / "refresh_log.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in meta.items() if k != "failed"}, ensure_ascii=False), flush=True)
    print({k: len(v) for k, v in meta["failed"].items()}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
