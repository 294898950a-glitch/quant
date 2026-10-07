#!/usr/bin/env python3
"""Rebuild cb_call.parquet so it holds real forced calls only, and keep every
redemption-related announcement, dated, in cb_redemption_notices.parquet.

Why: eastmoney exposes one "latest notice" per bond. The old cb_call stored it
whatever it was (put, maturity, "will not redeem", forced call), and the
backtest treated each as a call from the notice date to maturity. By 2026-05
that excluded 58% of listed bonds, and for bonds whose latest notice was a put
the real call was missing.

Sources:
  eastmoney RPT_BOND_CB_LIST   EXECUTE_REASON 4 = forced call (notice -> delist
                               in 0-19 days for all 468 delisted cases);
                               1/3 = put, 5 = maturity, 6 = will not redeem
  cninfo announcement archive  cb_announcements.jsonl collected on hkvm
                               (~/.hermes/shared/cb_announcement/), keyword 赎回

A bond is a forced call when eastmoney says reason 4, or when cninfo has an
explicit call announcement naming the bond and the bond stopped trading within
60 days of it (or the notice is in the last 60 days and the bond is still
listed). ann_date is the earliest such announcement; it is the date the call
became public, so a backtest that reads it knows nothing early. A bond that
stopped trading more than 90 days before contract maturity with neither is
recorded with source "inferred" and ann_date = last trade - 30 days, provided
its last close is at least 100 (a bond delisted far below par was not called).

跑法:
    python scripts/build_cb_call_history.py --announcements <cb_announcements.jsonl> [<incremental.jsonl> ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_cb_warehouse import WAREHOUSE_DIR, code_to_ts_code, fetch_cb_universe_em  # noqa: E402

EARLY_DAYS = 90
NOTICE_TO_LAST_TRADE_DAYS = 60
INFERRED_LEAD_DAYS = 30


def classify_title(title: str) -> str:
    if re.search("不提前赎回|不行使|不赎回|暂不", title):
        return "no_call"
    if re.search("可能满足|预计满足|预计触发|可能触发|有可能|或将|是否满足|重新计算", title):
        return "may_trigger"
    if re.search("到期|兑付|回售|现金管理|理财", title):
        return "other"
    if re.search("结果|摘牌|停止交易|停止转股|最后.*交易日", title):
        return "call_late"
    if re.search("提前赎回|赎回实施|实施.*赎回|赎回.*公告", title):
        return "call"
    return "other"


def load_notices(
    paths: list[Path], life: pd.DataFrame, keyword: str = "赎回", classify=classify_title
) -> pd.DataFrame:
    """Announcements of one search keyword, attributed to a bond and classified by title.

    paths: the full archive and any incremental files (same record format); duplicates are dropped.
    life: ts_code, first_trade, last_trade, bond_short_name.
    """
    short_name = dict(zip(life["ts_code"], life["bond_short_name"]))
    rows = []
    for path in paths:
        with path.open(encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            r = json.loads(line)
            if r.get("record_type") != "announcement" or r.get("keyword") != keyword:
                continue
            title = str(r.get("ann_title") or "").replace("<em>", "").replace("</em>", "")
            day = str(r.get("ann_datetime") or "")[:10].replace("-", "")
            codes = r.get("candidate_ts_codes") or []
            # The archive is collected per stock: candidate codes and candidate names are two separately
            # ordered lists, so pairing them by position gives one bond's notice to its sibling. Use each
            # bond's own short name, and only the full name ("特发转" would also match "特发转2").
            named = [ts for ts in codes if short_name.get(ts) and short_name[ts] in title]
            for ts in named or codes:
                rows.append({"ts_code": ts, "ann_date": day, "kind": classify(title),
                             "named_in_title": bool(named), "title": title, "url": r.get("ann_url")})
    notices = pd.DataFrame(rows).merge(life[["ts_code", "first_trade", "last_trade"]], on="ts_code", how="inner")
    # An issuer's archive covers all of its bonds; keep a notice only inside the bond's own listed life.
    alive = (notices["ann_date"] >= notices["first_trade"]) & (
        pd.to_datetime(notices["ann_date"]) <= pd.to_datetime(notices["last_trade"]) + pd.Timedelta(days=10)
    )
    notices = notices[alive].drop_duplicates(["ts_code", "ann_date", "title"])
    return notices.sort_values(["ts_code", "ann_date"]).reset_index(drop=True)


def _ymd(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y%m%d")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--announcements", type=Path, nargs="+", required=True,
                   help="cninfo archive, plus incremental files from fetch_cb_announcements_incremental.py")
    args = p.parse_args()

    daily = pd.read_parquet(WAREHOUSE_DIR / "cb_daily.parquet", columns=["ts_code", "trade_date", "close"])
    daily = daily.sort_values(["ts_code", "trade_date"])
    warehouse_end = str(daily["trade_date"].max())
    life = daily.groupby("ts_code").agg(
        first_trade=("trade_date", "min"), last_trade=("trade_date", "max"), last_close=("close", "last")
    ).reset_index()
    basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")
    life = life.merge(basic[["ts_code", "bond_short_name"]], on="ts_code", how="left")

    em = pd.DataFrame(fetch_cb_universe_em())
    em["ts_code"] = em["SECURITY_CODE"].astype(str).map(code_to_ts_code)
    em["reason"] = em["EXECUTE_REASON_HS"].fillna(em["EXECUTE_REASON_SH"])
    em["em_ann"] = _ymd(em["NOTICE_DATE_HS"].fillna(em["NOTICE_DATE_SH"]))
    em["em_call_date"] = _ymd(em["EXECUTE_START_DATEHS"].fillna(em["EXECUTE_START_DATESH"]))
    em["em_call_price"] = pd.to_numeric(em["EXECUTE_PRICE_HS"].fillna(em["EXECUTE_PRICE_SH"]), errors="coerce")

    notices = load_notices(args.announcements, life)
    # an explicit call announcement that names this bond; "call_late" (result / delisting notices) is not a start
    call_notices = notices[(notices["kind"] == "call") & notices["named_in_title"]]
    first_notice = call_notices.groupby("ts_code")["ann_date"].min()

    b = basic.merge(life.drop(columns="bond_short_name"), on="ts_code", how="inner").merge(
        em[["ts_code", "reason", "em_ann", "em_call_date", "em_call_price"]], on="ts_code", how="left"
    )
    scheduled = pd.to_datetime(b["contract_maturity_date"])  # NaT where unresolved: such a bond is never "early"
    b["stopped"] = b["last_trade"] < warehouse_end
    b["early"] = b["stopped"] & ((scheduled - pd.to_datetime(b["last_trade"])).dt.days > EARLY_DAYS)
    b["cn_ann"] = b["ts_code"].map(first_notice)
    # the notice counts when trading ended soon after it; a still-listed bond is measured against the data end
    days_after_notice = (pd.to_datetime(b["last_trade"]) - pd.to_datetime(b["cn_ann"])).dt.days
    followed = days_after_notice.between(0, NOTICE_TO_LAST_TRADE_DAYS)

    is_em = (b["reason"] == "4") & b["em_ann"].notna()
    b.loc[~followed & ~is_em, "cn_ann"] = pd.NA
    is_cn = ~is_em & b["cn_ann"].notna()
    is_inferred = ~is_em & ~is_cn & b["early"] & (b["last_close"] >= 100.0)
    b["source"] = np.select([is_em & b["cn_ann"].notna(), is_em, is_cn, is_inferred],
                            ["eastmoney+cninfo", "eastmoney", "cninfo", "inferred"], default="")
    calls = b[b["source"] != ""].copy()
    inferred_ann = (pd.to_datetime(calls["last_trade"]) - pd.Timedelta(days=INFERRED_LEAD_DAYS)).dt.strftime("%Y%m%d")
    candidates = pd.concat([calls["em_ann"].where(calls["source"].str.startswith("eastmoney")), calls["cn_ann"]], axis=1)
    calls["ann_date"] = candidates.min(axis=1).fillna(inferred_ann)
    calls["ann_date"] = calls[["ann_date", "first_trade"]].max(axis=1)
    from_em = calls["source"].str.startswith("eastmoney")
    out = pd.DataFrame({
        "ts_code": calls["ts_code"], "code": calls["code"], "ann_date": calls["ann_date"],
        "call_date": calls["em_call_date"].where(from_em), "call_price": calls["em_call_price"].where(from_em),
        "is_call": "公告实施强赎",
        # consumers read [ann_date, expire_date] as the called interval; it ends when trading ends
        "expire_date": np.where(calls["stopped"], calls["last_trade"], calls["contract_maturity_date"]),
        "call_type": "forced_call", "source": calls["source"],
    }).sort_values("ts_code").reset_index(drop=True)

    out.to_parquet(WAREHOUSE_DIR / "cb_call.parquet", index=False)
    notices[["ts_code", "ann_date", "kind", "named_in_title", "title", "url"]].to_parquet(
        WAREHOUSE_DIR / "cb_redemption_notices.parquet", index=False
    )
    lead = (pd.to_datetime(calls["last_trade"]) - pd.to_datetime(calls["ann_date"])).dt.days[calls["stopped"]]
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "warehouse_end": warehouse_end,
        "bonds": int(len(b)),
        "forced_calls": int(len(out)),
        "by_source": {k: int(v) for k, v in out["source"].value_counts().items()},
        "stopped_early_bonds": int(b["early"].sum()),
        "pending_calls_on_listed_bonds": int((~calls["stopped"]).sum()),
        "days_from_notice_to_last_trade": {k: float(v) for k, v in lead.describe(percentiles=[0.05, 0.5, 0.95]).round(1).items()},
        "notices": {k: int(v) for k, v in notices["kind"].value_counts().items()},
        "notice_archive_end": str(notices["ann_date"].max()),
    }
    (WAREHOUSE_DIR / "cb_call.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
