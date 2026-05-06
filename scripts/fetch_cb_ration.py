#!/usr/bin/env python3
"""Fetch JSL pre_list data and save cb_ration.parquet."""
import json
import time
import urllib.request
import ssl
import pandas as pd
from pathlib import Path

OUTPUT_PATH = Path("/home/jay/projects/quant/data/cb_warehouse/cb_ration.parquet")
MAX_PAGES = 30
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "https://www.jisilu.cn/data/cbnew/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.jisilu.cn",
}

def fetch_page(page: int) -> dict:
    """Fetch one page of pre_list data from JSL API."""
    ts = int(time.time() * 1000)
    data = f"___jsl=LST___t={ts}"
    url = f"https://www.jisilu.cn/data/cbnew/pre_list/?___jsl=LST___t={ts}&rp={PAGE_SIZE}&page={page}"

    req = urllib.request.Request(url, data=data.encode(), headers=HEADERS, method="POST")
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        print(f"  [ERROR] page={page}: {e}")
        return None

def extract_records(result: dict) -> list[dict]:
    """Extract relevant fields from API response."""
    records = []
    rows = result.get("rows", [])
    for row in rows:
        cell = row.get("cell", {})
        ration_rt_str = cell.get("ration_rt") or "0"
        try:
            # Values like "88.130" or None
            ration_rt = float(str(ration_rt_str)) if ration_rt_str else 0.0
        except (ValueError, TypeError):
            ration_rt = 0.0

        if ration_rt <= 0:
            continue

        records.append({
            "bond_id": str(cell.get("bond_id", "")).strip(),
            "bond_nm": str(cell.get("bond_nm", "")).strip(),
            "stock_nm": str(cell.get("stock_nm", "")).strip(),
            "stock_id": str(cell.get("stock_id", "")).strip(),
            "ration_rt": ration_rt,
            "amount": float(cell.get("amount", 0) or 0),
            "online_amount": float(cell.get("online_amount", 0) or 0),
            "lucky_draw_rt": float(cell.get("lucky_draw_rt", 0) or 0),
            "apply_date": str(cell.get("apply_date", "")).strip(),
            "list_date": str(cell.get("list_date", "")).strip(),
        })
    return records

def main():
    all_records = []

    print(f"Fetching JSL pre_list data (max {MAX_PAGES} pages, {PAGE_SIZE}/page)...")

    for page in range(1, MAX_PAGES + 1):
        time.sleep(0.6)  # Be polite to the server
        result = fetch_page(page)
        if result is None:
            print(f"  Page {page}: FAILED, stopping pagination")
            break

        raw_count = len(result.get("rows", []))
        recs = extract_records(result)
        all_records.extend(recs)
        print(f"  Page {page}: {raw_count} raw rows, {len(recs)} with ration_rt > 0")

        # Stop if fewer rows than page size (last page)
        if raw_count < PAGE_SIZE:
            print(f"  -> Last page detected (rows < {PAGE_SIZE})")
            break

    if not all_records:
        print("\nNo records with ration_rt > 0 found!")
        return

    # Build new DataFrame
    new_df = pd.DataFrame(all_records)
    print(f"\nTotal fetched (ration_rt > 0): {len(new_df)}")

    # Deduplicate: keep max ration_rt per bond_id
    new_df = new_df.sort_values("ration_rt", ascending=False).drop_duplicates(subset="bond_id", keep="first")
    print(f"After dedup (max ration_rt per bond_id): {len(new_df)}")

    # Merge with existing if present
    if OUTPUT_PATH.exists():
        old_df = pd.read_parquet(OUTPUT_PATH)
        print(f"Existing records: {len(old_df)}")

        merged = pd.concat([old_df, new_df], ignore_index=True)
        merged = merged.sort_values("ration_rt", ascending=False).drop_duplicates(subset="bond_id", keep="first")
        merged = merged.sort_values("bond_id").reset_index(drop=True)

        added_ids = set(new_df["bond_id"]) - set(old_df["bond_id"])
        updated_ids = set(new_df["bond_id"]) & set(old_df["bond_id"])
    else:
        merged = new_df.sort_values("bond_id").reset_index(drop=True)
        added_ids = set(new_df["bond_id"])
        updated_ids = set()

    # Save
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(OUTPUT_PATH, index=False)

    # Stats
    print(f"\n{'='*50}")
    print(f"cb_ration PARQUET SAVED")
    print(f"{'='*50}")
    print(f"Total records in warehouse: {len(merged)}")
    print(f"Newly added bond_ids:        {len(added_ids)}")
    print(f"Updated (overwritten):       {len(updated_ids)}")
    print(f"Ration_rt range:             {merged['ration_rt'].min():.2f}% ~ {merged['ration_rt'].max():.2f}%")
    print(f"Ration_rt mean/median:        {merged['ration_rt'].mean():.2f}% / {merged['ration_rt'].median():.2f}%")

    # Show latest apply_dates
    print(f"Apply_date range:            {merged['apply_date'].min()} ~ {merged['apply_date'].max()}")
    list_dates = merged[merged['list_date'].notna() & (merged['list_date'] != '')]
    if len(list_dates):
        print(f"List_date range:             {list_dates['list_date'].min()} ~ {list_dates['list_date'].max()}")

    print(f"\nTop 10 by ration_rt:")
    top = merged.nlargest(10, "ration_rt")[["bond_id", "bond_nm", "stock_nm", "ration_rt", "apply_date", "list_date"]]
    for _, row in top.iterrows():
        ld = row['list_date'] or 'N/A'
        print(f"  {row['bond_id']} {row['bond_nm']:10s} ({row['stock_nm']:8s}) 配售{row['ration_rt']:.2f}%  申购{row['apply_date']}  上市{ld}")

    print(f"\nFile: {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes)")

if __name__ == "__main__":
    main()
