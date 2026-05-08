"""Pool statistics (Layer 9.5) — raw numerical description of the
currently-attached holdout pool.

Role in the framework
---------------------
The ninth role added to the self-loop: a thin layer that, given the OHLC
DataFrame for the events in the currently-attached holdout pool, computes
five **raw, label-free** statistics describing the data slice. The
output is consumed downstream by:

- :mod:`hypothesizer` — packs the numbers into the LLM prompt so the
  caller can decide what kind of market shape is in front of it
  *without* the framework having pre-baked any priors.
- :mod:`memory` — the dict is attached to each :class:`RunRecord` so
  the auditor / future analyses can look back at what the data actually
  looked like for any iteration.

Design principle
----------------
**Numbers, not labels.** This module deliberately does NOT classify the
data into bull / bear / ranging / volatile / dead or any other named
state. Such tags would re-introduce hand-tuned thresholds (e.g. ">+10%
is bull") that vary across asset classes (10% drift is huge for SPX,
trivial for crypto). Every threshold in this codebase is a prior we
do not have evidence for; the framework's stated goal is to let data
speak for itself, so we pass raw numbers to the LLM and let *it*
synthesise a market read.

If you find yourself reaching for an ``if x > THRESHOLD: tag = "..."``
pattern in this file, stop — that belongs in the LLM prompt, not the
statistic computation.

Public API
----------
- :class:`PoolStats` — immutable dataclass of five float fields plus
  ``sample_n``, with ``to_dict()`` and ``to_prompt_lines()`` for
  serialisation / display.
- :func:`compute_pool_stats(prices)` — non-raising entry; degrades to
  an all-zero PoolStats with the appropriate ``sample_n`` for empty /
  single-row inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Public dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PoolStats:
    """Raw statistical description of one OHLC slice.

    All five numerical fields are scale-aware enough that they compare
    cleanly across instruments at very different absolute price levels:
    the slope is normalised by the median close, the trend is a
    fraction, the volatility is a daily-return standard deviation and
    the range is normalised by the mean close.

    Attributes
    ----------
    sample_n : int
        Number of bars (rows) in the source DataFrame. Zero or one
        bar means the other fields cannot be computed and stay at 0.0.
    trend_pct : float
        ``close[-1] / close[0] - 1`` — fractional cumulative move
        across the whole window. Sign-significant; magnitude only
        meaningful relative to the asset's typical volatility.
    slope_per_day : float
        Linear-regression slope of close vs day-index, divided by
        ``median(close)`` to make it comparable across price levels.
        Reported as a daily fraction.
    vol_daily : float
        Standard deviation of the daily simple returns (close pct
        change), as a fraction. Population sigma (``ddof=0``) so the
        single-row degenerate case stays at 0.0 cleanly.
    range_pct : float
        ``(max(close) - min(close)) / mean(close)`` — a width measure
        that is independent of where the move started. Always non-
        negative.
    """

    sample_n: int
    trend_pct: float
    slope_per_day: float
    vol_daily: float
    range_pct: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON / outbox / RunRecord use."""
        return asdict(self)

    def to_prompt_lines(self) -> list[str]:
        """Format as five Chinese lines for LLM prompts.

        The wording is intentionally **descriptive, not interpretive**:
        we report numbers and let the consumer judge. Do not add
        adjectives like "high"/"low" here — that's a label in disguise.
        """
        return [
            f"累计涨跌: {self.trend_pct:+.1%}",
            f"日斜率: {self.slope_per_day:+.4f}%",
            f"日波动率: {self.vol_daily:.2%}",
            f"区间宽度: {self.range_pct:.1%}",
            f"样本数: {self.sample_n}",
        ]


# --------------------------------------------------------------------------- #
# Compute helper
# --------------------------------------------------------------------------- #


def _zero_stats(sample_n: int) -> PoolStats:
    """Build the all-zero degenerate PoolStats with ``sample_n`` carried."""
    return PoolStats(
        sample_n=int(sample_n),
        trend_pct=0.0,
        slope_per_day=0.0,
        vol_daily=0.0,
        range_pct=0.0,
    )


def compute_pool_stats(prices: pd.DataFrame) -> PoolStats:
    """Compute :class:`PoolStats` from an OHLC DataFrame.

    Parameters
    ----------
    prices : pandas.DataFrame
        Must contain a ``close`` column. Rows are assumed to be in
        ascending date order. Any other columns are ignored.

    Returns
    -------
    PoolStats
        The five raw numbers. **Never raises** — empty / single-row /
        malformed inputs degrade to a zero-filled record with the
        observed ``sample_n``.

    Notes
    -----
    The slope is computed as
    ``polyfit(day_index, close, 1)[0] / median(close)``. Normalising
    by the median (rather than the mean) is more robust to the
    occasional spike that an ETF or futures contract may exhibit and
    keeps slopes from different-priced instruments comparable.
    """
    if prices is None:
        return _zero_stats(0)

    try:
        close = prices["close"]
    except (KeyError, TypeError):
        # No usable column — fall back to zeros at whatever length we
        # can introspect.
        try:
            n = len(prices)  # type: ignore[arg-type]
        except TypeError:
            n = 0
        return _zero_stats(n)

    # Drop NaNs so a partially-empty window does not poison the math;
    # report sample_n as the length AFTER dropping. Matches what the LLM
    # actually has to work with.
    try:
        close = pd.Series(close).dropna().astype(float)
    except (TypeError, ValueError):
        return _zero_stats(0)

    n = int(len(close))
    if n <= 1:
        return _zero_stats(n)

    first = float(close.iloc[0])
    last = float(close.iloc[-1])
    trend_pct = (last / first) - 1.0 if first != 0.0 else 0.0

    median_close = float(np.median(close.to_numpy()))
    if median_close == 0.0:
        slope_per_day = 0.0
    else:
        x = np.arange(n, dtype=float)
        # polyfit returns highest-order coefficient first; degree 1 ->
        # [slope, intercept].
        slope = float(np.polyfit(x, close.to_numpy(dtype=float), 1)[0])
        slope_per_day = slope / median_close

    daily_returns = close.pct_change().dropna().to_numpy()
    if daily_returns.size == 0:
        vol_daily = 0.0
    else:
        vol_daily = float(np.std(daily_returns, ddof=0))

    arr = close.to_numpy(dtype=float)
    mean_close = float(arr.mean()) if arr.size else 0.0
    if mean_close == 0.0:
        range_pct = 0.0
    else:
        range_pct = float((arr.max() - arr.min()) / mean_close)

    return PoolStats(
        sample_n=n,
        trend_pct=trend_pct,
        slope_per_day=slope_per_day,
        vol_daily=vol_daily,
        range_pct=range_pct,
    )


__all__ = [
    "PoolStats",
    "compute_pool_stats",
]
