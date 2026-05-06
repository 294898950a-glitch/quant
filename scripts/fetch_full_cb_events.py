#!/usr/bin/env python3
"""
Full-scan CB downward-revision event fetcher.

- Pulls live + delisted CB lists from akshare (bond_zh_cov + bond_cb_redeem_jsl)
- For each 6-digit code calls ak.bond_cb_adj_logs_jsl(symbol=code) (JSL has anti-scrape, 0.15s sleep)
- Up to 3 retries per bond, then logs failure and moves on
- Writes consolidated events to data/cb_pead/raw/cb_down_events_full.csv
- Failure list to logs/fetch_full_cb_events_failures.txt
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import pandas as pd
import akshare as ak

REPO = Path(__file__).resolve().parents[1]
OUT_CSV = REPO / "data" / "cb_pead" / "raw" / "cb_down_events_full.csv"
FAIL_LOG = REPO / "logs" / "fetch_full_cb_events_failures.txt"

SLEEP_SEC = 0.15
MAX_RETRIES = 3


def collect_universe() -> pd.DataFrame:
    """Union live (bond_zh_cov) + delisted/redeemed (bond_cb_redeem_jsl) lists.

    Returns DataFrame with columns ['code', 'name'] (6-digit codes, dedup).
    """
    rows = []

    try:
        live = ak.bond_zh_cov()
        for _, r in live.iterrows():
            code = str(r.get("债券代码", "")).strip()
            name = str(r.get("债券简称", "")).strip()
            if code and code.isdigit() and len(code) == 6:
                rows.append((code, name))
        print(f"[universe] bond_zh_cov: {len(live)} live bonds", flush=True)
    except Exception as e:
        print(f"[universe] bond_zh_cov FAILED: {e}", flush=True)

    try:
        redeem = ak.bond_cb_redeem_jsl()
        for _, r in redeem.iterrows():
            code = str(r.get("代码", "")).strip()
            name = str(r.get("名称", "")).strip()
            if code and code.isdigit() and len(code) == 6:
                rows.append((code, name))
        print(f"[universe] bond_cb_redeem_jsl: {len(redeem)} delisted/redeemed", flush=True)
    except Exception as e:
        print(f"[universe] bond_cb_redeem_jsl FAILED: {e}", flush=True)

    df = pd.DataFrame(rows, columns=["code", "name"]).drop_duplicates(subset=["code"])
    df = df.sort_values("code").reset_index(drop=True)
    print(f"[universe] union total unique codes: {len(df)}", flush=True)
    return df


def fetch_one(code: str) -> pd.DataFrame | None:
    """Fetch adj-logs for one bond with retries. Returns None on permanent failure."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = ak.bond_cb_adj_logs_jsl(symbol=code)
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(SLEEP_SEC * attempt)  # backoff
    # all retries failed
    raise last_err if last_err is not None else RuntimeError("unknown")


def parse_events(code: str, name: str, df: pd.DataFrame) -> list[dict]:
    """Convert JSL adj-log DataFrame to event dicts.

    JSL columns: 转债名称 / 股东大会日 / 下修前转股价 / 下修后转股价 / 新转股价生效日期 / 下修底价
    Note: JSL only provides 股东大会日 (meeting_date); board_announce_date stays empty.
    """
    out: list[dict] = []
    if df is None or df.empty:
        return out

    for _, r in df.iterrows():
        try:
            meeting = str(r.get("股东大会日", "")).strip()
            if not meeting or meeting in ("None", "nan", "NaT"):
                continue
            # Normalize date
            try:
                meeting = pd.to_datetime(meeting).strftime("%Y-%m-%d")
            except Exception:
                pass

            before = r.get("下修前转股价", None)
            after = r.get("下修后转股价", None)
            floor = r.get("下修底价", None)

            try:
                before_f = float(before) if before not in (None, "", "nan") else None
            except Exception:
                before_f = None
            try:
                after_f = float(after) if after not in (None, "", "nan") else None
            except Exception:
                after_f = None
            try:
                floor_f = float(floor) if floor not in (None, "", "nan") else None
            except Exception:
                floor_f = None

            ratio = None
            if before_f and after_f and before_f > 0:
                ratio = round(after_f / before_f, 6)

            # Skip rows missing the core revision data
            if before_f is None or after_f is None:
                continue

            jsl_name = str(r.get("转债名称", "")).strip() or name

            out.append(
                {
                    "bond_id": code,
                    "name": jsl_name,
                    "board_announce_date": "",  # JSL doesn't expose board-proposal date
                    "meeting_date": meeting,
                    "before_price": before_f,
                    "after_price": after_f,
                    "down_floor_price": floor_f,
                    "ratio": ratio,
                }
            )
        except Exception:
            continue
    return out


def main() -> int:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)

    universe = collect_universe()
    total = len(universe)
    if total == 0:
        print("[fatal] empty universe", flush=True)
        return 2

    all_events: list[dict] = []
    failures: list[tuple[str, str, str]] = []  # (code, name, err)

    t_start = time.time()
    for i, row in enumerate(universe.itertuples(index=False), start=1):
        code, name = row.code, row.name
        try:
            df = fetch_one(code)
            evs = parse_events(code, name, df)
            all_events.extend(evs)
            n_ev = len(evs)
        except Exception as e:  # noqa: BLE001
            failures.append((code, name, repr(e)[:200]))
            n_ev = -1  # marker

        time.sleep(SLEEP_SEC)

        if i % 50 == 0 or i == total:
            elapsed = time.time() - t_start
            print(
                f"[{i}/{total}] code={code} events_so_far={len(all_events)} "
                f"failures={len(failures)} elapsed={elapsed:.1f}s",
                flush=True,
            )

    # Write events
    if all_events:
        ev_df = pd.DataFrame(all_events).sort_values(["bond_id", "meeting_date"]).reset_index(drop=True)
        ev_df.to_csv(OUT_CSV, index=False, encoding="utf-8")
        print(f"[done] wrote {len(ev_df)} events -> {OUT_CSV}", flush=True)
    else:
        print("[done] no events collected!", flush=True)

    # Write failures
    with FAIL_LOG.open("w", encoding="utf-8") as fh:
        fh.write(f"# fetch_full_cb_events failures (total {len(failures)})\n")
        fh.write("# code\tname\terror\n")
        for code, name, err in failures:
            fh.write(f"{code}\t{name}\t{err}\n")
    print(f"[done] wrote {len(failures)} failures -> {FAIL_LOG}", flush=True)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[interrupted]", flush=True)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
