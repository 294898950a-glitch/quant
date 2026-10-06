#!/usr/bin/env python3
"""Build cb_events.parquet: every dated down-revision and call event per bond.

Sources:
  --announcements  cninfo archive collected on hkvm (cb_announcements.jsonl), keywords 下修 and 赎回
  --adj-logs       Jisilu down-revision log collected daily on hkvm (cb_conv_price_adj_jisilu.jsonl)
  cb_call.parquet, cb_redemption_notices.parquet   forced calls and every dated redemption notice,
                   both written by scripts/build_cb_call_history.py (run it first)

Announcements whose title cannot be classified are kept as "unclassified".

跑法:
    python scripts/build_cb_events.py --announcements <jsonl> --adj-logs <jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cb_market import events as ev  # noqa: E402
from scripts.build_cb_call_history import load_notices  # noqa: E402
from scripts.build_cb_warehouse import WAREHOUSE_DIR, code_to_ts_code  # noqa: E402

_REDEMPTION_KIND = {"no_call": ev.NO_CALL, "may_trigger": ev.CALL_MAY_TRIGGER}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--announcements", type=Path, required=True)
    p.add_argument("--adj-logs", type=Path, required=True)
    args = p.parse_args()

    daily = pd.read_parquet(WAREHOUSE_DIR / "cb_daily.parquet", columns=["ts_code", "trade_date"])
    life = daily.groupby("ts_code")["trade_date"].agg(first_trade="min", last_trade="max").reset_index()
    frames = []

    revision = load_notices(args.announcements, life, keyword="下修", classify=ev.classify_revision_title)
    frames.append(pd.DataFrame({
        "ts_code": revision["ts_code"], "event_date": revision["ann_date"], "event_type": revision["kind"],
        "source": "cninfo", "detail": revision["title"],
    }))
    # redemption notices were attributed and classified once, by build_cb_call_history.py; read that result
    redemption = pd.read_parquet(WAREHOUSE_DIR / "cb_redemption_notices.parquet")
    redemption = redemption[redemption["kind"].isin(_REDEMPTION_KIND)]
    frames.append(pd.DataFrame({
        "ts_code": redemption["ts_code"], "event_date": redemption["ann_date"],
        "event_type": redemption["kind"].map(_REDEMPTION_KIND), "source": "cninfo", "detail": redemption["title"],
    }))

    latest: dict[str, dict] = {}
    with args.adj_logs.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("status") == "ok":
                latest[rec["bond_id"]] = rec  # append-only file: the last ok line per bond wins
    rows = []
    for code, rec in latest.items():
        ts = code_to_ts_code(code)
        for r in rec["records"]:
            outcome = r.get("outcome") or ("approved" if r.get("approved") else "rejected")
            if r.get("meeting_date"):
                rows.append((ts, r["meeting_date"].replace("-", ""), ev.REVISION_MEETING, outcome))
            if outcome == "approved" and r.get("effective_date"):
                rows.append((ts, r["effective_date"].replace("-", ""), ev.REVISION_EFFECTIVE,
                             f"{r.get('old_conv_price')}->{r.get('new_conv_price')}"))
    frames.append(pd.DataFrame(rows, columns=["ts_code", "event_date", "event_type", "detail"]).assign(source="jisilu"))

    calls = pd.read_parquet(WAREHOUSE_DIR / "cb_call.parquet")
    frames.append(pd.DataFrame({
        "ts_code": calls["ts_code"], "event_date": calls["ann_date"], "event_type": ev.CALL_ANNOUNCED,
        "source": "cb_call:" + calls["source"], "detail": "",
    }))

    out = pd.concat(frames, ignore_index=True)[["ts_code", "event_date", "event_type", "source", "detail"]]
    out = out[out["ts_code"].isin(set(life["ts_code"]))]
    out = out.drop_duplicates(["ts_code", "event_date", "event_type"]).sort_values(["ts_code", "event_date"]).reset_index(drop=True)
    out.to_parquet(ev.EVENTS_PARQUET, index=False)
    cninfo = out[out["source"] == "cninfo"]
    meta = {
        "name": "cb_events", "built_at": datetime.now(timezone.utc).isoformat(),
        "events": int(len(out)), "bonds": int(out["ts_code"].nunique()),
        "by_type": {k: int(v) for k, v in out["event_type"].value_counts().items()},
        "cninfo_archive_range": [str(cninfo["event_date"].min()), str(cninfo["event_date"].max())],
        "cninfo_events_by_year": {k: int(v) for k, v in cninfo["event_date"].str[:4].value_counts().sort_index().items()},
        "unclassified_share_of_revision_notices": round(float((revision["kind"] == ev.UNCLASSIFIED).mean()), 4),
    }
    ev.EVENTS_PARQUET.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
