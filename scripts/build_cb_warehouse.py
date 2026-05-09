#!/usr/bin/env python3
"""可转债数据仓库构建脚本 (akshare 版).

原版用 tushare 第三方代理, token 已过期. 改用 akshare 免费源.

数据源:
  - bond_zh_cov               活跃 CB 列表 (~1000 只, 含评级/转股价/上市日等基础)
  - bond_cb_redeem_jsl        已强赎 CB 列表 (历史延伸用)
  - bond_zh_cov_info          单只 CB 详细 (含 IS_REDEEM/NOTICE_DATE_HS/EXPIRE_DATE 等)
  - bond_zh_hs_cov_daily      CB 日线
  - stock_zh_a_daily          正股日线 (含 qfq 前复权)

输出 (data/cb_warehouse/):
  - cb_basic.parquet            CB 基础 (面值/利率/到期/转股价/评级/上市日/正股代码)
  - cb_daily.parquet            CB 日线
  - cb_call.parquet             强赎公告 (从 cov_info 反推)
  - cb_price_chg.parquet        转股价调整历史 (合并自 bond_cb_adj_logs_jsl)
  - stk_daily.parquet           正股日线 (不复权)
  - stk_daily_qfq.parquet       正股前复权日线 (BS 公式用)

用法:
  python scripts/build_cb_warehouse.py             # 全量
  python scripts/build_cb_warehouse.py --update    # 增量更新最近一周
  python scripts/build_cb_warehouse.py --summary   # 只看仓库状态
  python scripts/build_cb_warehouse.py --max-bonds 100   # 限制数量 (调试用)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# 默认每次请求间隔 (akshare 节流)
RATE_LIMIT_SEC = 0.20
# 单次失败重试
MAX_RETRIES = 2

ROOT_DIR = Path(__file__).resolve().parent.parent
WAREHOUSE_DIR = ROOT_DIR / "data" / "cb_warehouse"
WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# 解析工具
# =============================================================================

def parse_coupon_explain(text: Optional[str]) -> Optional[float]:
    """从 INTEREST_RATE_EXPLAIN 文本解析平均年化利率.

    例: '第一年0.40%、第二年0.60%、第三年1.00%、第四年1.50%、第五年1.80%、第六年2.00%。'
    -> 平均 = 0.012167 (年化 ~1.22%)
    """
    if text is None or not isinstance(text, str):
        return None
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
    if not nums:
        return None
    vals = [float(x) / 100 for x in nums]
    return sum(vals) / len(vals) if vals else None


def to_ymd(dt) -> Optional[str]:
    """各种日期格式 -> YYYYMMDD 字符串."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = dt.strip()
        if not dt or dt.lower() == "none":
            return None
        try:
            return pd.to_datetime(dt).strftime("%Y%m%d")
        except Exception:
            return None
    try:
        return pd.to_datetime(dt).strftime("%Y%m%d")
    except Exception:
        return None


def code_to_symbol(code: str) -> str:
    """6 位 CB 代码 -> akshare 'shXXXXXX' / 'szXXXXXX' 格式.

    沪市 CB: 110xxx, 113xxx
    深市 CB: 123xxx, 127xxx, 128xxx
    """
    code = str(code).strip()
    if code.startswith("11"):
        return f"sh{code}"
    return f"sz{code}"


def code_to_ts_code(code: str) -> str:
    """6 位 -> 'XXXXXX.SH/SZ' (与原 tushare 一致)."""
    code = str(code).strip()
    if code.startswith("11"):
        return f"{code}.SH"
    return f"{code}.SZ"


# =============================================================================
# 阶段 1: 列出 CB 全宇宙 (活跃 + 已强赎)
# =============================================================================

def fetch_cb_universe_em() -> list[dict]:
    """直接调 eastmoney RPT_BOND_CB_LIST, 一次拿到全宇宙(含 delisted).

    返回: list of dict, 每条是 cov_info 等价字段 (SECURITY_CODE, RATING, ...).
    """
    import requests

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    all_rows = []
    page = 1
    while True:
        r = requests.get(
            url,
            params={
                "reportName": "RPT_BOND_CB_LIST",
                "columns": "ALL",
                "pageSize": 500,
                "pageNumber": page,
            },
            timeout=30,
        )
        d = r.json()
        if not d.get("result"):
            break
        rows = d["result"].get("data") or []
        all_rows.extend(rows)
        if len(rows) < 500:
            break
        page += 1
        if page > 20:
            break
    return all_rows


def collect_cb_universe() -> pd.DataFrame:
    """eastmoney RPT_BOND_CB_LIST -> 全宇宙 (1012 含 delisted/active)."""
    print("[universe] 调 eastmoney RPT_BOND_CB_LIST...", flush=True)
    raw = []
    try:
        raw = fetch_cb_universe_em()
        print(f"[universe] eastmoney 返回 {len(raw)} 只", flush=True)
    except Exception as e:
        print(f"[universe] eastmoney FAILED: {e}", flush=True)

    rows = []
    for r in raw:
        code = str(r.get("SECURITY_CODE", "")).strip()
        if code.isdigit() and len(code) == 6:
            rows.append({
                "code": code,
                "name": str(r.get("SECURITY_NAME_ABBR", "")).strip(),
                "stk_code": str(r.get("CONVERT_STOCK_CODE", "")).strip(),
                "stk_name": str(r.get("SECURITY_SHORT_NAME", "")).strip(),
                # 顺手把 cov_info 全字段带过去, 阶段 2 就不必再请求
                "_em_row": r,
            })

    df = pd.DataFrame(rows).drop_duplicates(subset=["code"], keep="first")
    df = df.sort_values("code").reset_index(drop=True)
    print(f"[universe] 合并去重 总数 = {len(df)}", flush=True)
    return df


# =============================================================================
# 阶段 2: 单 CB 详细 cov_info -> cb_basic + cb_call
# =============================================================================

def build_cb_basic_and_call(
    universe: pd.DataFrame, max_bonds: Optional[int] = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从 universe 内嵌的 eastmoney 全字段构造 cb_basic + cb_call (无网络).

    eastmoney RPT_BOND_CB_LIST 一次返回了 70+ 字段, 对每只 CB 都齐全, 阶段 2
    无需再调 cov_info per-bond. 大幅省时.
    """
    basic_rows = []
    call_rows = []

    df = universe
    if max_bonds:
        df = df.head(max_bonds)

    print(f"[basic] 从 universe 内嵌 eastmoney 字段构造 ({len(df)} 只)...", flush=True)
    n_ok = 0
    n_redeem = 0

    for _, u in df.iterrows():
        info = u.get("_em_row") or {}
        if not info:
            continue
        n_ok += 1

        code = u["code"]
        ts_code = code_to_ts_code(code)
        coupon_avg = parse_coupon_explain(info.get("INTEREST_RATE_EXPLAIN"))
        rating = (info.get("RATING") or "").strip().rstrip("sti")

        basic_rows.append({
            "ts_code": ts_code,
            "code": code,
            "bond_short_name": info.get("SECURITY_NAME_ABBR"),
            "stk_code": info.get("CONVERT_STOCK_CODE"),
            "issue_size": pd.to_numeric(info.get("ACTUAL_ISSUE_SCALE"), errors="coerce"),
            "remain_size": None,
            "conv_price": (
                pd.to_numeric(info.get("CONVERT_STOCK_PRICE"), errors="coerce")
                if info.get("CONVERT_STOCK_PRICE") is not None
                else pd.to_numeric(info.get("TRANSFER_PRICE"), errors="coerce")
                if info.get("TRANSFER_PRICE") is not None
                else pd.to_numeric(info.get("INITIAL_TRANSFER_PRICE"), errors="coerce")
            ),
            "value_date": to_ymd(info.get("VALUE_DATE")),
            "list_date": to_ymd(info.get("LISTING_DATE")),
            "delist_date": to_ymd(info.get("DELIST_DATE")),
            "maturity_date": to_ymd(info.get("EXPIRE_DATE")),
            "transfer_start_date": to_ymd(info.get("TRANSFER_START_DATE")),
            "transfer_end_date": to_ymd(info.get("TRANSFER_END_DATE")),
            "rating": rating or "AA",
            "coupon_rate": coupon_avg if coupon_avg is not None else 0.01,
            "interest_rate_explain": info.get("INTEREST_RATE_EXPLAIN"),
            "par_value": pd.to_numeric(info.get("PAR_VALUE"), errors="coerce") or 100.0,
            "issue_price": pd.to_numeric(info.get("ISSUE_PRICE"), errors="coerce"),
        })

        # 强赎事件: 优先 NOTICE_DATE_HS (沪市定义), 备用 NOTICE_DATE_SH (深市)
        is_redeem = info.get("IS_REDEEM")
        if is_redeem == "是":
            n_redeem += 1
            ann_date = to_ymd(info.get("NOTICE_DATE_HS")) or to_ymd(info.get("NOTICE_DATE_SH"))
            call_date = to_ymd(info.get("EXECUTE_START_DATEHS")) or to_ymd(info.get("EXECUTE_START_DATESH"))
            call_price = (
                pd.to_numeric(info.get("EXECUTE_PRICE_HS"), errors="coerce")
                or pd.to_numeric(info.get("EXECUTE_PRICE_SH"), errors="coerce")
            )
            call_rows.append({
                "ts_code": ts_code,
                "code": code,
                "ann_date": ann_date,
                "call_date": call_date,
                "call_price": call_price,
                "is_call": "公告实施强赎",
                "expire_date": to_ymd(info.get("EXPIRE_DATE")),
            })

    print(f"[basic] 完成: 转 {n_ok} 只, 其中强赎事件 {n_redeem} 只", flush=True)
    return pd.DataFrame(basic_rows), pd.DataFrame(call_rows)


# =============================================================================
# 阶段 3: cb_daily 每只 CB 的日线
# =============================================================================

def fetch_cb_daily_one(code: str) -> Optional[pd.DataFrame]:
    """拿一只 CB 的日线 (akshare bond_zh_hs_cov_daily 返回全历史)."""
    import akshare as ak
    sym = code_to_symbol(code)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = ak.bond_zh_hs_cov_daily(symbol=sym)
            time.sleep(RATE_LIMIT_SEC)
            if df is None or df.empty:
                return None
            df = df.copy()
            df["ts_code"] = code_to_ts_code(code)
            df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
            df = df.rename(columns={
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "vol",
            })
            return df[["ts_code", "trade_date", "open", "high", "low", "close", "vol"]]
        except Exception as e:
            last_err = e
            time.sleep(RATE_LIMIT_SEC * (attempt + 1))
    return None


def build_cb_daily(codes: list[str], max_bonds: Optional[int] = None) -> pd.DataFrame:
    """对每只 CB 拉日线."""
    if max_bonds:
        codes = codes[:max_bonds]

    print(f"[daily] 拉 {len(codes)} 只 CB 日线... (~{len(codes)*0.5:.0f}s)", flush=True)
    n_ok = 0
    n_fail = 0
    all_dfs = []
    t0 = time.time()

    for i, code in enumerate(codes):
        df = fetch_cb_daily_one(code)
        if df is None or df.empty:
            n_fail += 1
        else:
            all_dfs.append(df)
            n_ok += 1

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (len(codes) - i - 1)
            total = sum(len(d) for d in all_dfs)
            print(
                f"  [{i+1}/{len(codes)}] ok={n_ok} fail={n_fail} rows={total} "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                flush=True,
            )

    if not all_dfs:
        return pd.DataFrame()

    merged = pd.concat(all_dfs, ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_code", "trade_date"]).sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)
    elapsed = time.time() - t0
    print(f"[daily] 完成: ok={n_ok} fail={n_fail} 总行 {len(merged):,}, 耗时 {elapsed:.0f}s", flush=True)
    return merged


# =============================================================================
# 阶段 4: 转股价调整历史
# =============================================================================

def fetch_price_chg_one(code: str) -> Optional[pd.DataFrame]:
    """拿一只 CB 的转股价调整记录."""
    import akshare as ak
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = ak.bond_cb_adj_logs_jsl(symbol=code)
            time.sleep(RATE_LIMIT_SEC)
            if df is None or df.empty:
                return None
            return df
        except Exception as e:
            last_err = e
            time.sleep(RATE_LIMIT_SEC * (attempt + 1))
    return None


def build_price_chg(codes: list[str], max_bonds: Optional[int] = None) -> pd.DataFrame:
    """转股价变动 - 这个 API 经常被反爬, 失败容忍."""
    if max_bonds:
        codes = codes[:max_bonds]

    print(f"[price_chg] 拉 {len(codes)} 只 CB 转股价调整 (有反爬, 失败容忍)...", flush=True)
    rows = []
    n_ok = 0
    n_fail = 0
    t0 = time.time()

    for i, code in enumerate(codes):
        df = fetch_price_chg_one(code)
        if df is None or df.empty:
            n_fail += 1
        else:
            df = df.copy()
            df["ts_code"] = code_to_ts_code(code)
            rows.append(df)
            n_ok += 1

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            print(
                f"  [{i+1}/{len(codes)}] ok={n_ok} fail={n_fail} elapsed={elapsed:.0f}s",
                flush=True,
            )

    if not rows:
        print("[price_chg] 全失败 (JSL 反爬), 返回空", flush=True)
        return pd.DataFrame()

    merged = pd.concat(rows, ignore_index=True)
    print(f"[price_chg] 完成: ok={n_ok} fail={n_fail} 总行 {len(merged):,}", flush=True)
    return merged


# =============================================================================
# 阶段 5: 正股日线 (不复权 + 前复权)
# =============================================================================

def fetch_stock_daily_one(stk_code: str, adjust: str = "") -> Optional[pd.DataFrame]:
    """正股日线. adjust='' 不复权, 'qfq' 前复权."""
    import akshare as ak

    if not stk_code or not stk_code.isdigit() or len(stk_code) != 6:
        return None
    sym = f"sh{stk_code}" if stk_code.startswith("6") else f"sz{stk_code}"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust=adjust)
            time.sleep(RATE_LIMIT_SEC)
            if df is None or df.empty:
                return None
            df = df.copy()
            df["stk_code"] = stk_code
            df["ts_code"] = f"{stk_code}.SH" if stk_code.startswith("6") else f"{stk_code}.SZ"
            df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
            cols = ["ts_code", "stk_code", "trade_date", "open", "high", "low", "close", "volume"]
            cols = [c for c in cols if c in df.columns]
            return df[cols]
        except Exception as e:
            last_err = e
            time.sleep(RATE_LIMIT_SEC * (attempt + 1))
    return None


def build_stock_daily(stk_codes: list[str], adjust: str = "") -> pd.DataFrame:
    """正股日线全量."""
    label = "qfq" if adjust == "qfq" else "raw"
    print(f"[stock_daily/{label}] 拉 {len(stk_codes)} 只正股日线...", flush=True)
    n_ok = 0
    n_fail = 0
    all_dfs = []
    t0 = time.time()

    for i, code in enumerate(stk_codes):
        df = fetch_stock_daily_one(code, adjust=adjust)
        if df is None or df.empty:
            n_fail += 1
        else:
            all_dfs.append(df)
            n_ok += 1

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (len(stk_codes) - i - 1)
            print(
                f"  [{label}][{i+1}/{len(stk_codes)}] ok={n_ok} fail={n_fail} "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                flush=True,
            )

    if not all_dfs:
        return pd.DataFrame()

    merged = pd.concat(all_dfs, ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_code", "trade_date"]).sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)
    elapsed = time.time() - t0
    print(
        f"[stock_daily/{label}] 完成: ok={n_ok} fail={n_fail} 总行 {len(merged):,}, "
        f"耗时 {elapsed:.0f}s",
        flush=True,
    )
    return merged


# =============================================================================
# Summary
# =============================================================================

def summary():
    print("\n" + "=" * 70)
    print("数据仓库状态")
    print("=" * 70)
    files = [
        "cb_basic", "cb_daily", "cb_call", "cb_price_chg", "stk_daily", "stk_daily_qfq",
    ]
    for name in files:
        path = WAREHOUSE_DIR / f"{name}.parquet"
        if path.exists():
            size_mb = path.stat().st_size / (1024 * 1024)
            df = pd.read_parquet(path)
            line = f"  {name:20s}: {len(df):>9,} 条  ({size_mb:.1f} MB)"
            if "trade_date" in df.columns and len(df):
                line += f"  日期 {df['trade_date'].min()} ~ {df['trade_date'].max()}"
            if "ts_code" in df.columns:
                line += f"  | 标的 {df['ts_code'].nunique()}"
            print(line)
        else:
            print(f"  {name:20s}: NOT FOUND")


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="可转债数据仓库构建 (akshare)")
    parser.add_argument("--update", action="store_true", help="增量更新 (最近 7 天日线)")
    parser.add_argument("--summary", action="store_true", help="只看仓库状态")
    parser.add_argument(
        "--max-bonds",
        type=int,
        default=None,
        help="限制 CB 数量, 调试用",
    )
    parser.add_argument("--skip-price-chg", action="store_true", help="跳过 cb_price_chg (JSL 反爬)")
    parser.add_argument("--skip-stock", action="store_true", help="跳过正股日线")
    args = parser.parse_args()

    if args.summary:
        summary()
        return

    # 阶段 1: 全宇宙
    universe = collect_cb_universe()
    if universe.empty:
        print("FATAL: 全宇宙为空, 退出", flush=True)
        sys.exit(1)

    # 阶段 2: cb_basic + cb_call
    df_basic, df_call = build_cb_basic_and_call(universe, max_bonds=args.max_bonds)
    if not df_basic.empty:
        df_basic.to_parquet(WAREHOUSE_DIR / "cb_basic.parquet", index=False)
        print(f"  -> cb_basic.parquet ({len(df_basic)} 条)", flush=True)
    if not df_call.empty:
        df_call.to_parquet(WAREHOUSE_DIR / "cb_call.parquet", index=False)
        print(f"  -> cb_call.parquet ({len(df_call)} 条)", flush=True)

    # 阶段 3: cb_daily
    codes = df_basic["code"].tolist() if not df_basic.empty else universe["code"].tolist()
    df_daily = build_cb_daily(codes, max_bonds=args.max_bonds)
    if not df_daily.empty:
        df_daily.to_parquet(WAREHOUSE_DIR / "cb_daily.parquet", index=False)
        print(f"  -> cb_daily.parquet ({len(df_daily)} 条)", flush=True)

    # 阶段 4: cb_price_chg (JSL 反爬, 默认尝试)
    if not args.skip_price_chg:
        df_chg = build_price_chg(codes, max_bonds=args.max_bonds)
        if not df_chg.empty:
            df_chg.to_parquet(WAREHOUSE_DIR / "cb_price_chg.parquet", index=False)
            print(f"  -> cb_price_chg.parquet ({len(df_chg)} 条)", flush=True)

    # 阶段 5: 正股日线
    if not args.skip_stock and not df_basic.empty:
        stk_codes = (
            df_basic["stk_code"].dropna().astype(str).str.strip().unique().tolist()
        )
        stk_codes = [c for c in stk_codes if c.isdigit() and len(c) == 6]
        print(f"\n[stock] 唯一正股 {len(stk_codes)} 只, 双拉 (raw + qfq)", flush=True)

        df_raw = build_stock_daily(stk_codes, adjust="")
        if not df_raw.empty:
            df_raw.to_parquet(WAREHOUSE_DIR / "stk_daily.parquet", index=False)
            print(f"  -> stk_daily.parquet ({len(df_raw)} 条)", flush=True)

        df_qfq = build_stock_daily(stk_codes, adjust="qfq")
        if not df_qfq.empty:
            df_qfq.to_parquet(WAREHOUSE_DIR / "stk_daily_qfq.parquet", index=False)
            print(f"  -> stk_daily_qfq.parquet ({len(df_qfq)} 条)", flush=True)

    print("\n" + "=" * 70)
    print("数据仓库构建完成")
    print("=" * 70)
    summary()


if __name__ == "__main__":
    main()
