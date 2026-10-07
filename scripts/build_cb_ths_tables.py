#!/usr/bin/env python3
"""Parse the downloaded Tonghuashun F10 pages into three warehouse tables.

  cb_holders_top10.parquet    ten largest holders per reporting date
  cb_rating_history.parquet   every rating action with its publication date (issuer and bond)
  cb_balance_history.parquet  outstanding balance on each coupon date, plus the value on the fetch date

Reads data/cb_raw/ths_f10/ (scripts/fetch_cb_ths_f10.py). Exchange codes are reused, so a code's pages
are used in full only if the summary page shows a convertible bond whose interest start date or short
name is ours. The site keys pages by the six digits alone, so a Shanghai bond and an older Shenzhen bond
with the same digits share one page (118004 is both a 2022 convertible and a 2012 private placement).
Such a page is still used, for rows dated after our interest start date only, when the other bond had
matured before ours began and the coupon rows after that date equal our own coupon schedule; issuer
ratings and the page's current balance are then not taken. Anything else is listed under
identity_failed in cb_ths_tables.json and contributes no rows.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_cb_warehouse import WAREHOUSE_DIR  # noqa: E402
from scripts.fetch_cb_ths_f10 import PAGES, RAW_DIR, raw_path  # noqa: E402

RATING_LEAD_DAYS = 550  # an issue is rated before it is sold; earlier bond ratings belong to a previous user of the code


def read_page(code: str, page: str) -> tuple[str | None, str | None]:
    """(html, fetched_at 'YYYYMMDD') or (None, None) when the page was not downloaded."""
    path = raw_path(code, page)
    if not path.exists():
        return None, None
    raw = gzip.decompress(path.read_bytes())
    fetched = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), fetched
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", "replace"), fetched


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip()


def table_rows(html: str) -> list[list[str]]:
    """Every <tr> that has <td> cells, as cell texts."""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)
        if cells:
            rows.append([_text(c) for c in cells])
    return rows


def summary_fields(html: str) -> dict[str, str]:
    """'label：' -> value from the two-column summary tables."""
    out: dict[str, str] = {}
    for row in table_rows(html):
        for label, value in zip(row[0::2], row[1::2]):
            out[label.rstrip("：:").strip()] = value
    return out


def _ymd(text: str) -> str | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text.strip())
    return "".join(m.groups()) if m else None


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def identity(fields: dict[str, str], short_name: str, value_date: str | None) -> str | None:
    """None when the page is our bond, else the reason it is not."""
    if not fields:
        return "empty summary page"
    if "转" not in fields.get("债券类型", ""):
        return f"page shows {fields.get('债券类型') or '?'} {fields.get('债券简称') or ''}".strip()
    if fields.get("债券简称") == short_name or (value_date and _ymd(fields.get("起息日期", "")) == value_date):
        return None
    return f"page shows {fields.get('债券简称')} from {fields.get('起息日期')}"


def coupons_match(dividend_html: str, value_date: str, coupons: list[float] | None) -> bool:
    """Do the page's coupon rows after value_date reproduce our contract's coupon schedule?"""
    if coupons is None or len(coupons) == 0:
        return False
    start, hits = pd.Timestamp(value_date), 0
    for cells in table_rows(dividend_html):
        day, rate = (_ymd(cells[0]), _num(cells[5])) if len(cells) == 7 else (None, None)
        if not day or day <= value_date or rate is None:
            continue
        year = round((pd.Timestamp(day) - start).days / 365.25)
        if not 1 <= year <= len(coupons) or abs(rate - float(coupons[year - 1])) > 1e-6:
            return False
        hits += 1
    return hits >= 2


def parse_holders(html: str, ts_code: str) -> list[dict]:
    rows = []
    for day, block in re.findall(r'id="t_(\d{4}-\d{2}-\d{2})"(.*?)</table>', html, flags=re.S):
        for rank, cells in enumerate((c for c in table_rows(block) if len(c) == 4), start=1):
            rows.append({"ts_code": ts_code, "report_date": day.replace("-", ""), "rank": rank,
                         "holder_name": cells[0], "holder_type": None if cells[1] in ("--", "") else cells[1],
                         "hold_wan": _num(cells[2]), "hold_pct": _num(cells[3])})
    return rows


def parse_ratings(html: str, ts_code: str) -> list[dict]:
    rows = []
    for cells in table_rows(html):
        if len(cells) == 5 and cells[0] in ("主体评级", "债券评级") and _ymd(cells[1]):
            rows.append({"ts_code": ts_code, "ann_date": _ymd(cells[1]),
                         "scope": "issuer" if cells[0] == "主体评级" else "bond",
                         "rating": cells[2], "rating_type": cells[3], "agency": cells[4]})
    return rows


def parse_balances(html: str, ts_code: str, fetched: str) -> list[dict]:
    """Balance on past coupon dates. Future rows of the schedule and the final repayment row carry no observation."""
    rows = []
    for cells in table_rows(html):
        if len(cells) == 7 and _ymd(cells[0]) and cells[4] == "付息" and _ymd(cells[0]) <= fetched:
            rows.append({"ts_code": ts_code, "date": _ymd(cells[0]), "balance_wan_yuan": _num(cells[6]),
                         "source": "ths_coupon_date"})
    return rows


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")
    coupons = pd.read_parquet(WAREHOUSE_DIR / "cb_contract_terms.parquet", columns=["ts_code", "coupons_pct"])
    coupons = dict(zip(coupons["ts_code"], coupons["coupons_pct"]))
    holders, ratings, balances = [], [], []
    identity_failed, shared_page, not_downloaded, fetch_days = {}, [], [], []
    for b in basic.itertuples(index=False):
        code = b.ts_code[:6]
        pages = {page: read_page(code, page) for page in PAGES}
        if any(html is None for html, _ in pages.values()):
            not_downloaded.append(b.ts_code)
            continue
        fetched = pages["index"][1]
        fetch_days.append(fetched)
        value_date = None if pd.isna(b.value_date) else str(b.value_date)[:10].replace("-", "")
        fields = summary_fields(pages["index"][0])
        reason = identity(fields, b.bond_short_name, value_date)
        shared = False
        if reason:
            other_end = _ymd(fields.get("到期日期", ""))
            shared = bool(value_date and other_end and other_end < value_date
                          and coupons_match(pages["dividend"][0], value_date, coupons.get(b.ts_code)))
            if not shared:
                identity_failed[b.ts_code] = reason
                continue
            shared_page.append(b.ts_code)
        start = value_date or (None if pd.isna(b.list_date) else str(b.list_date)[:8])
        holders += [r for r in parse_holders(pages["detail"][0], b.ts_code) if not start or r["report_date"] >= start]
        rating_from = (pd.Timestamp(start) - pd.Timedelta(days=RATING_LEAD_DAYS)).strftime("%Y%m%d") if start else "0"
        ratings += [r for r in parse_ratings(pages["grade"][0], b.ts_code)
                    if (r["scope"] == "issuer" and not shared) or (r["scope"] == "bond" and r["ann_date"] >= rating_from)]
        balances += [r for r in parse_balances(pages["dividend"][0], b.ts_code, fetched) if not start or r["date"] >= start]
        current = _num(fields.get("债券余额(万元)", ""))
        if current is not None and pd.isna(b.delist_date) and not shared:
            balances.append({"ts_code": b.ts_code, "date": fetched, "balance_wan_yuan": current, "source": "ths_fetch_date"})

    tables = {
        "cb_holders_top10": (pd.DataFrame(holders), ["ts_code", "report_date", "rank"]),
        "cb_rating_history": (pd.DataFrame(ratings).drop_duplicates(), ["ts_code", "scope", "ann_date"]),
        "cb_balance_history": (pd.DataFrame(balances).dropna(subset=["balance_wan_yuan"]), ["ts_code", "date"]),
    }
    meta: dict = {
        "name": "cb_ths_tables", "built_at": datetime.now(timezone.utc).isoformat(),
        "source": "basic.10jqka.com.cn/<code>/{index,detail,grade,dividend}.html, raw pages in data/cb_raw/ths_f10/",
        "pages_fetched_between": [min(fetch_days), max(fetch_days)] if fetch_days else None,
        "bonds_in_cb_basic": int(len(basic)), "bonds_used": int(len(basic) - len(not_downloaded) - len(identity_failed)),
        "used_from_a_shared_page": len(shared_page),
        "not_downloaded": not_downloaded, "identity_failed": identity_failed, "tables": {},
        "limits": [
            "holders: the ten largest only, on the dates issuers report them (listing, half-year, year-end)",
            "balance: one observation per coupon date plus the fetch date; nothing in between",
            "ratings: issuer rows include actions from before the bond existed",
        ],
    }
    for name, (df, order) in tables.items():
        df = df.sort_values(order).reset_index(drop=True)
        df.to_parquet(WAREHOUSE_DIR / f"{name}.parquet", index=False)
        date_col = order[-1] if name != "cb_holders_top10" else "report_date"
        meta["tables"][name] = {"rows": int(len(df)), "bonds": int(df["ts_code"].nunique()),
                                "first": str(df[date_col].min()), "last": str(df[date_col].max())}
    (WAREHOUSE_DIR / "cb_ths_tables.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: meta[k] for k in ("bonds_used", "tables")}, ensure_ascii=False, indent=1))
    print(f"shared page {len(shared_page)}, not downloaded {len(not_downloaded)}, identity failed {len(identity_failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
