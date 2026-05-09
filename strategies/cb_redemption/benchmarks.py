"""Benchmark return loaders for the evaluation framework.

Loaders are offline-first: by default they read cached parquet files under
``data/benchmarks`` and never call network providers. Passing ``refresh=True``
allows provider fetches and rewrites the cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import json

import pandas as pd


_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "benchmarks"


class BenchmarkDataError(RuntimeError):
    """Raised when benchmark data cannot be loaded safely."""


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for benchmark data sources and cache locations."""

    cache_dir: Path = DEFAULT_CACHE_DIR
    cash_annual_return: float = 0.015
    trading_days_per_year: int = 250
    csi300_symbol: str = "000300"
    treasury_etf_symbol: str = "511010"
    dividend_symbols: tuple[str, ...] = ("601088", "600900", "601398", "601939")
    stock_adjust: str = "qfq"
    metadata: dict[str, str] = field(default_factory=dict)


def load_benchmark(
    name: str,
    start: str,
    end: str,
    *,
    config: BenchmarkConfig | None = None,
    refresh: bool = False,
) -> pd.Series:
    """Load one benchmark as daily decimal returns.

    ``refresh=False`` never calls akshare. Non-cash, non-local benchmarks must
    already have a cache file in that mode.
    """

    cfg = config or BenchmarkConfig()
    if name == "cash":
        return _cash_returns(start, end, cfg)

    if name == "cb_equal":
        prices = _load_or_build_local_cb_equal(start, end, cfg, refresh=refresh)
        return _prices_to_returns(prices, start, end)

    prices = _load_cached_prices(name, cfg)
    if prices is None:
        if not refresh:
            raise BenchmarkDataError(
                f"missing cached benchmark {name!r}; rerun with refresh=True"
            )
        prices = _fetch_remote_prices(name, start, end, cfg)
        write_benchmark_cache(name, prices, config=cfg, metadata={"source": "akshare"})
    elif refresh:
        prices = _fetch_remote_prices(name, start, end, cfg)
        write_benchmark_cache(name, prices, config=cfg, metadata={"source": "akshare"})

    return _prices_to_returns(prices, start, end)


def load_benchmarks(
    names: tuple[str, ...] | list[str],
    start: str,
    end: str,
    *,
    config: BenchmarkConfig | None = None,
    refresh: bool = False,
) -> dict[str, pd.Series]:
    """Load multiple benchmark return series."""

    return {
        name: load_benchmark(name, start, end, config=config, refresh=refresh)
        for name in names
    }


def write_benchmark_cache(
    name: str,
    prices: pd.Series,
    *,
    config: BenchmarkConfig | None = None,
    metadata: dict[str, str] | None = None,
) -> Path:
    """Write a price series cache and sidecar metadata JSON."""

    cfg = config or BenchmarkConfig()
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    clean = _clean_prices(prices)
    if clean.empty:
        raise BenchmarkDataError(f"cannot cache empty benchmark {name!r}")

    out = _cache_path(name, cfg)
    df = clean.rename("close").reset_index().rename(columns={"index": "trade_date"})
    df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
    df.to_parquet(out, index=False)

    meta = {
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start": str(df["trade_date"].iloc[0]),
        "end": str(df["trade_date"].iloc[-1]),
    }
    meta.update(cfg.metadata)
    if metadata:
        meta.update(metadata)
    _metadata_path(name, cfg).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out


def _cache_path(name: str, cfg: BenchmarkConfig) -> Path:
    return cfg.cache_dir / f"{name}.parquet"


def _metadata_path(name: str, cfg: BenchmarkConfig) -> Path:
    return cfg.cache_dir / f"{name}.json"


def _cash_returns(start: str, end: str, cfg: BenchmarkConfig) -> pd.Series:
    dates = pd.bdate_range(start=start, end=end)
    daily = (1.0 + cfg.cash_annual_return) ** (1.0 / cfg.trading_days_per_year) - 1.0
    return pd.Series(daily, index=dates, name="cash")


def _load_cached_prices(name: str, cfg: BenchmarkConfig) -> pd.Series | None:
    path = _cache_path(name, cfg)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if not {"trade_date", "close"}.issubset(df.columns):
        raise BenchmarkDataError(f"invalid benchmark cache schema: {path}")
    return _clean_prices(
        pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["trade_date"]))
    )


def _load_or_build_local_cb_equal(
    start: str, end: str, cfg: BenchmarkConfig, *, refresh: bool
) -> pd.Series:
    cached = None if refresh else _load_cached_prices("cb_equal", cfg)
    if cached is not None:
        return cached

    path = _REPO_ROOT / "data" / "cb_warehouse" / "cb_daily.parquet"
    if not path.exists():
        raise BenchmarkDataError(f"missing local CB daily parquet: {path}")
    daily = pd.read_parquet(path, columns=["trade_date", "close"])
    if daily.empty:
        raise BenchmarkDataError("cb_daily parquet is empty")
    close = daily.groupby("trade_date")["close"].mean()
    prices = _clean_prices(pd.Series(close.to_numpy(), index=pd.to_datetime(close.index)))
    write_benchmark_cache(
        "cb_equal",
        prices,
        config=cfg,
        metadata={"source": "data/cb_warehouse/cb_daily.parquet"},
    )
    return prices


def _prices_to_returns(prices: pd.Series, start: str, end: str) -> pd.Series:
    clean = _clean_prices(prices)
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    window = clean[(clean.index >= start_ts) & (clean.index <= end_ts)]
    if len(window) < 2:
        raise BenchmarkDataError(
            f"not enough benchmark prices in requested range {start}~{end}"
        )
    returns = window.pct_change().dropna()
    returns.name = prices.name
    if returns.isna().any():
        raise BenchmarkDataError("benchmark returns contain NaN after pct_change")
    return returns


def _clean_prices(prices: pd.Series) -> pd.Series:
    if not isinstance(prices, pd.Series):
        raise TypeError("prices must be pandas Series")
    clean = prices.copy()
    clean.index = pd.to_datetime(clean.index)
    clean = pd.to_numeric(clean, errors="coerce")
    clean = clean.dropna()
    clean = clean[clean > 0]
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    return clean.astype(float)


def _fetch_remote_prices(name: str, start: str, end: str, cfg: BenchmarkConfig) -> pd.Series:
    if name == "csi300":
        return _fetch_csi300(start, end, cfg)
    if name == "dividend":
        return _fetch_equal_weight_stocks(cfg.dividend_symbols, start, end, cfg)
    if name == "sixty_forty":
        csi300 = _fetch_csi300(start, end, cfg)
        bond = _fetch_etf(cfg.treasury_etf_symbol, start, end, cfg)
        return _weighted_price_index({"csi300": csi300, "bond": bond}, {"csi300": 0.6, "bond": 0.4})
    raise BenchmarkDataError(f"unknown benchmark: {name}")


def _akshare():
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise BenchmarkDataError("akshare is required for refresh=True") from exc
    return ak


def _fetch_csi300(start: str, end: str, cfg: BenchmarkConfig) -> pd.Series:
    ak = _akshare()
    df = ak.index_zh_a_hist(
        symbol=cfg.csi300_symbol,
        period="daily",
        start_date=_compact_date(start),
        end_date=_compact_date(end),
    )
    return _series_from_ak_df(df, close_col_candidates=("收盘", "close"))


def _fetch_etf(symbol: str, start: str, end: str, cfg: BenchmarkConfig) -> pd.Series:
    ak = _akshare()
    df = ak.fund_etf_hist_em(
        symbol=symbol,
        period="daily",
        start_date=_compact_date(start),
        end_date=_compact_date(end),
        adjust=cfg.stock_adjust,
    )
    return _series_from_ak_df(df, close_col_candidates=("收盘", "close"))


def _fetch_stock(symbol: str, start: str, end: str, cfg: BenchmarkConfig) -> pd.Series:
    ak = _akshare()
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=_compact_date(start),
        end_date=_compact_date(end),
        adjust=cfg.stock_adjust,
    )
    return _series_from_ak_df(df, close_col_candidates=("收盘", "close"))


def _fetch_equal_weight_stocks(
    symbols: tuple[str, ...], start: str, end: str, cfg: BenchmarkConfig
) -> pd.Series:
    prices = {symbol: _fetch_stock(symbol, start, end, cfg) for symbol in symbols}
    weights = {symbol: 1.0 / len(symbols) for symbol in symbols}
    return _weighted_price_index(prices, weights)


def _weighted_price_index(
    prices: dict[str, pd.Series], weights: dict[str, float]
) -> pd.Series:
    returns = pd.DataFrame(
        {name: _clean_prices(series).pct_change() for name, series in prices.items()}
    ).dropna(how="any")
    if returns.empty:
        raise BenchmarkDataError("weighted benchmark has no overlapping dates")
    combined = sum(returns[name] * weights[name] for name in weights)
    index = (1.0 + combined).cumprod()
    index.iloc[0] = 1.0
    return index.rename("close")


def _series_from_ak_df(
    df: pd.DataFrame, *, close_col_candidates: tuple[str, ...]
) -> pd.Series:
    date_col = "日期" if "日期" in df.columns else "date"
    close_col = next((c for c in close_col_candidates if c in df.columns), None)
    if date_col not in df.columns or close_col is None:
        raise BenchmarkDataError(f"unexpected akshare schema: {list(df.columns)}")
    return _clean_prices(pd.Series(df[close_col].to_numpy(), index=pd.to_datetime(df[date_col])))


def _compact_date(date_s: str) -> str:
    return pd.to_datetime(date_s).strftime("%Y%m%d")


__all__ = [
    "BenchmarkConfig",
    "BenchmarkDataError",
    "load_benchmark",
    "load_benchmarks",
    "write_benchmark_cache",
]
