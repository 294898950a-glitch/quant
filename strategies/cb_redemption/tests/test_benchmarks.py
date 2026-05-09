"""Tests for offline-first benchmark loaders."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from strategies.cb_redemption.benchmarks import (
    BenchmarkConfig,
    BenchmarkDataError,
    BenchmarkNotAvailableError,
    load_benchmark,
    load_benchmarks,
    write_benchmark_cache,
)


def _prices(start: str = "2024-01-01", n: int = 8) -> pd.Series:
    idx = pd.bdate_range(start, periods=n)
    return pd.Series([100.0 + i for i in range(n)], index=idx)


def test_cash_benchmark_loads_without_cache(tmp_path) -> None:
    cfg = BenchmarkConfig(cache_dir=tmp_path)
    returns = load_benchmark("cash", "2024-01-01", "2024-01-10", config=cfg)

    assert len(returns) > 0
    assert returns.notna().all()
    assert returns.iloc[0] == pytest.approx((1.015 ** (1 / 250)) - 1)


def test_cached_benchmark_round_trips_with_metadata(tmp_path) -> None:
    cfg = BenchmarkConfig(cache_dir=tmp_path)
    write_benchmark_cache(
        "csi300", _prices(), config=cfg, metadata={"source": "unit-test"}
    )

    returns = load_benchmark("csi300", "2024-01-01", "2024-01-10", config=cfg)

    assert len(returns) == 7
    assert returns.notna().all()
    meta = json.loads((tmp_path / "csi300.json").read_text(encoding="utf-8"))
    assert meta["name"] == "csi300"
    assert meta["source"] == "unit-test"


def test_missing_cache_does_not_refresh_implicitly(tmp_path) -> None:
    cfg = BenchmarkConfig(cache_dir=tmp_path)

    with pytest.raises(BenchmarkDataError, match="missing cached benchmark"):
        load_benchmark("csi300", "2024-01-01", "2024-01-10", config=cfg)


def test_load_benchmarks_uses_same_date_window(tmp_path) -> None:
    cfg = BenchmarkConfig(cache_dir=tmp_path)
    for name in ("csi300", "dividend", "sixty_forty"):
        write_benchmark_cache(name, _prices(n=10), config=cfg)

    loaded = load_benchmarks(
        ["cash", "csi300", "dividend", "sixty_forty"],
        "2024-01-01",
        "2024-01-12",
        config=cfg,
    )

    assert set(loaded) == {"cash", "csi300", "dividend", "sixty_forty"}
    assert all(s.notna().all() for s in loaded.values())
    # Cached price series lose the first row to pct_change; cash starts directly
    # from the requested business-date range.
    assert len(loaded["csi300"]) == 9
    assert len(loaded["cash"]) == 10


def test_short_cached_price_window_raises(tmp_path) -> None:
    cfg = BenchmarkConfig(cache_dir=tmp_path)
    write_benchmark_cache("dividend", _prices(n=1), config=cfg)

    with pytest.raises(BenchmarkDataError, match="not enough benchmark prices"):
        load_benchmark("dividend", "2024-01-01", "2024-01-10", config=cfg)


def test_sixty_forty_rejects_dates_before_bond_etf_listing(tmp_path) -> None:
    cfg = BenchmarkConfig(cache_dir=tmp_path)

    with pytest.raises(BenchmarkNotAvailableError, match="2013-08-15"):
        load_benchmark("sixty_forty", "2012-01-01", "2012-12-31", config=cfg)
