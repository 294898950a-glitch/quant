"""Tests for :mod:`strategies.cb_redemption.pool_stats`.

Pool stats is the ninth role: a thin layer that distils an OHLC slice
into five raw numbers (``trend_pct``, ``slope_per_day``, ``vol_daily``,
``range_pct``, ``sample_n``). The role is **deliberately label-free** —
it is a contract violation for this module to ever emit
``bull / bear / ranging / volatile / dead`` (or their Chinese
equivalents) in any prompt output. These tests pin both the maths and
the no-label invariant.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.cb_redemption.pool_stats import PoolStats, compute_pool_stats


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _df(close_values: list[float]) -> pd.DataFrame:
    """Wrap a list of closes into the minimal DataFrame the function needs."""
    return pd.DataFrame({"close": close_values})


# --------------------------------------------------------------------------- #
# 1. Linear up trend → trend_pct ≈ +30%, slope > 0
# --------------------------------------------------------------------------- #


def test_linear_uptrend_yields_positive_trend_and_slope() -> None:
    closes = list(np.linspace(100.0, 130.0, 31))  # 31 bars, +30% total
    stats = compute_pool_stats(_df(closes))

    assert isinstance(stats, PoolStats)
    assert stats.sample_n == 31
    assert stats.trend_pct == pytest.approx(0.30, abs=1e-9)
    # Slope is positive on a strictly rising line; normalised by median
    # close (~115) → roughly 1/115 = ~0.0087 per day.
    assert stats.slope_per_day > 0.0
    # Range is monotonic from 100 to 130, mean ≈ 115 → (30/115) ≈ 0.26.
    assert stats.range_pct == pytest.approx(30.0 / 115.0, abs=1e-3)


# --------------------------------------------------------------------------- #
# 2. Linear down trend → trend_pct ≈ -25%, slope < 0
# --------------------------------------------------------------------------- #


def test_linear_downtrend_yields_negative_trend_and_slope() -> None:
    closes = list(np.linspace(100.0, 75.0, 26))  # 26 bars, -25% total
    stats = compute_pool_stats(_df(closes))

    assert stats.sample_n == 26
    assert stats.trend_pct == pytest.approx(-0.25, abs=1e-9)
    assert stats.slope_per_day < 0.0


# --------------------------------------------------------------------------- #
# 3. Flat / range-bound → narrow range_pct, near-zero trend_pct
# --------------------------------------------------------------------------- #


def test_flat_data_has_small_range_and_trend() -> None:
    rng = np.random.default_rng(seed=7)
    closes = list(100.0 + rng.uniform(-1.0, 1.0, size=120))  # bounded [99, 101]
    stats = compute_pool_stats(_df(closes))

    assert stats.sample_n == 120
    assert abs(stats.trend_pct) < 0.03
    assert stats.range_pct < 0.03


# --------------------------------------------------------------------------- #
# 4. High-volatility data → vol_daily > 2%
# --------------------------------------------------------------------------- #


def test_high_volatility_data_has_large_vol_daily() -> None:
    # Alternating ±3% daily moves around 100 — keeps level roughly stable
    # but generates daily returns with stdev well above 2%.
    closes = [100.0]
    sign = 1
    for _ in range(99):
        closes.append(closes[-1] * (1.0 + 0.03 * sign))
        sign *= -1
    stats = compute_pool_stats(_df(closes))

    assert stats.sample_n == 100
    assert stats.vol_daily > 0.02


# --------------------------------------------------------------------------- #
# 5. Empty DataFrame → sample_n = 0, no exception, all stats 0.0
# --------------------------------------------------------------------------- #


def test_empty_dataframe_returns_zero_stats() -> None:
    empty = pd.DataFrame({"close": []})
    stats = compute_pool_stats(empty)

    assert stats.sample_n == 0
    assert stats.trend_pct == 0.0
    assert stats.slope_per_day == 0.0
    assert stats.vol_daily == 0.0
    assert stats.range_pct == 0.0


# --------------------------------------------------------------------------- #
# 6. Single-row DataFrame → sample_n = 1, no exception, derived stats 0.0
# --------------------------------------------------------------------------- #


def test_single_row_returns_zero_stats() -> None:
    one = pd.DataFrame({"close": [123.4]})
    stats = compute_pool_stats(one)

    assert stats.sample_n == 1
    assert stats.trend_pct == 0.0
    assert stats.slope_per_day == 0.0
    assert stats.vol_daily == 0.0
    assert stats.range_pct == 0.0


# --------------------------------------------------------------------------- #
# 7. to_prompt_lines returns 5 strings and contains NO market-state labels
# --------------------------------------------------------------------------- #


def test_prompt_lines_have_no_label_strings() -> None:
    closes = list(np.linspace(100.0, 88.0, 130))  # downtrend
    stats = compute_pool_stats(_df(closes))
    lines = stats.to_prompt_lines()

    assert isinstance(lines, list)
    assert len(lines) == 5
    for line in lines:
        assert isinstance(line, str)

    joined = "\n".join(lines).lower()
    forbidden = (
        "bull",
        "bear",
        "ranging",
        "volatile",
        "dead",
        "牛",
        "熊",
        "震荡",
        "高波动",
        "低波动",
    )
    for token in forbidden:
        # case-insensitive English / exact Chinese
        assert token.lower() not in joined, (
            f"to_prompt_lines must remain label-free; saw {token!r} in {lines!r}"
        )


# --------------------------------------------------------------------------- #
# Bonus: to_dict round-trips all five fields
# --------------------------------------------------------------------------- #


def test_to_dict_contains_all_fields() -> None:
    stats = compute_pool_stats(_df([100.0, 101.0, 102.0, 103.0]))
    d = stats.to_dict()
    assert set(d) == {"sample_n", "trend_pct", "slope_per_day", "vol_daily", "range_pct"}
    assert d["sample_n"] == 4


# --------------------------------------------------------------------------- #
# Bonus: missing close column degrades gracefully
# --------------------------------------------------------------------------- #


def test_no_close_column_returns_zero_stats() -> None:
    df = pd.DataFrame({"open": [1, 2, 3]})
    stats = compute_pool_stats(df)
    # Length-3 DataFrame, no close column — fall back to zeros, but
    # carry the row count so the LLM can see "no usable closes".
    assert stats.sample_n == 3
    assert stats.trend_pct == 0.0
