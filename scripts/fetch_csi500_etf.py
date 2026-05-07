"""一次性拉取南方中证500ETF (510500.SH) 日线，落盘到 parquet。

输出: data/csi500_grid/raw/510500_daily.parquet
列: date (str YYYYMMDD), open, high, low, close, vol, amount
    open/high/low/close/amount = float64; vol = int64

akshare 接口尝试顺序:
    1. ak.fund_etf_hist_em(symbol="510500", ...)
    2. ak.fund_etf_hist_sina(symbol="sh510500")  (fallback)
失败重试 3 次，每次间隔 0.5s。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "csi500_grid" / "raw"
OUT_PATH = OUT_DIR / "510500_daily.parquet"

START_DATE = "20180101"


def _retry(fn, attempts: int = 3, sleep_s: float = 0.5):
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("attempt %d/%d failed: %s", i + 1, attempts, e)
            time.sleep(sleep_s)
    raise RuntimeError(f"all {attempts} attempts failed: {last_err}")


def _try_em(start: str, end: str) -> pd.DataFrame:
    import akshare as ak

    df = ak.fund_etf_hist_em(
        symbol="510500",
        period="daily",
        start_date=start,
        end_date=end,
        adjust="qfq",
    )
    return df


def _try_sina() -> pd.DataFrame:
    import akshare as ak

    df = ak.fund_etf_hist_sina(symbol="sh510500")
    return df


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名 + dtype。"""
    rename_map: dict[str, str] = {}
    for c in df.columns:
        cs = str(c).strip()
        if cs in ("日期", "date"):
            rename_map[c] = "date"
        elif cs in ("开盘", "open"):
            rename_map[c] = "open"
        elif cs in ("最高", "high"):
            rename_map[c] = "high"
        elif cs in ("最低", "low"):
            rename_map[c] = "low"
        elif cs in ("收盘", "close"):
            rename_map[c] = "close"
        elif cs in ("成交量", "volume", "vol"):
            rename_map[c] = "vol"
        elif cs in ("成交额", "amount"):
            rename_map[c] = "amount"
    df = df.rename(columns=rename_map)

    keep = ["date", "open", "high", "low", "close", "vol", "amount"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        # amount 缺失 (新浪接口) 用 0 填
        for c in missing:
            if c == "amount":
                df["amount"] = 0.0
            else:
                raise ValueError(f"missing required col {c}; have={list(df.columns)}")
    df = df[keep].copy()

    # date -> str YYYYMMDD
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0).astype("int64")

    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return df


def fetch(start: str = START_DATE, end: str | None = None) -> pd.DataFrame:
    end = end or datetime.now().strftime("%Y%m%d")

    # 1. 试 em
    try:
        df = _retry(lambda: _try_em(start, end))
        logger.info("fund_etf_hist_em ok, raw rows=%d", len(df))
        df = _normalize(df)
        # 区间过滤 (em 一般已经按 start/end 切，但保险)
        df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
        return df
    except Exception as e:  # noqa: BLE001
        logger.warning("fund_etf_hist_em failed entirely: %s; falling back to sina", e)

    # 2. fallback sina
    df = _retry(_try_sina)
    logger.info("fund_etf_hist_sina ok, raw rows=%d", len(df))
    df = _normalize(df)
    df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
    return df


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = fetch()
    if df.empty:
        logger.error("fetched empty DataFrame")
        return 1

    df.to_parquet(OUT_PATH, index=False)
    logger.info(
        "wrote %s shape=%s date=%s..%s close[min=%.3f max=%.3f]",
        OUT_PATH, df.shape, df["date"].iloc[0], df["date"].iloc[-1],
        df["close"].min(), df["close"].max(),
    )

    # sanity
    assert df["date"].is_monotonic_increasing, "date not sorted"
    assert (df["close"] > 0).all(), "non-positive close"
    assert (df["high"] >= df["low"]).all(), "high < low"
    print(f"OK rows={len(df)} {df['date'].iloc[0]}..{df['date'].iloc[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
