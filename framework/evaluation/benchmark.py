"""Benchmark loading, alignment, and excess-return calculation.

The default benchmark is the project-wide CB equal-weight index maintained in
`strategies/cb_arb/verifier.py`. Other benchmarks can be supplied as date-indexed
pandas Series of simple daily returns.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd


def load_benchmark(
    start_date: str | None = None,
    end_date: str | None = None,
    benchmark_id: str = "cb_equal_weight",
) -> pd.Series:
    """Load a daily benchmark return series.

    Args:
        start_date: Optional inclusive start date (YYYYmmdd).
        end_date: Optional inclusive end date (YYYYmmdd).
        benchmark_id: Currently only "cb_equal_weight" is supported, which
            wraps the project's existing CB equal-weight index.

    Returns:
        Date-indexed pandas Series of simple daily returns.
    """
    if benchmark_id != "cb_equal_weight":
        raise ValueError(f"Unsupported benchmark_id: {benchmark_id}")

    # Import here to avoid heavy module loading at package import time.
    from strategies.cb_arb.verifier import _get_cb_index

    idx = _get_cb_index()
    if start_date is not None:
        idx = idx[idx.index >= start_date]
    if end_date is not None:
        idx = idx[idx.index <= end_date]

    returns = idx.pct_change().fillna(0.0)
    returns.name = "benchmark_return"
    return returns


def load_benchmark_total_return(
    start_date: str,
    end_date: str,
    benchmark_id: str = "cb_equal_weight",
) -> float:
    """Total benchmark return over [start_date, end_date]."""
    if benchmark_id != "cb_equal_weight":
        raise ValueError(f"Unsupported benchmark_id: {benchmark_id}")

    from strategies.cb_arb.verifier import _index_total_return

    return float(_index_total_return(start_date, end_date))


def align_dates(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    fill_method: str = "forward",
) -> pd.DataFrame:
    """Align two date-indexed return series to a common date index.

    Args:
        strategy_returns: Date-indexed series of simple daily returns.
        benchmark_returns: Date-indexed series of simple daily returns.
        fill_method: How to fill missing benchmark values.
            "forward" uses ffill then bfill; "zero" fills with 0.0.

    Returns:
        DataFrame with columns ``strategy`` and ``benchmark``.
    """
    df = pd.DataFrame({"strategy": strategy_returns, "benchmark": benchmark_returns})
    df = df.sort_index()

    if fill_method == "forward":
        df["benchmark"] = df["benchmark"].ffill().bfill()
        df["strategy"] = df["strategy"].ffill().bfill()
    elif fill_method == "zero":
        df = df.fillna(0.0)
    else:
        raise ValueError(f"Unknown fill_method: {fill_method}")

    return df


def compute_excess_returns(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    fill_method: str = "forward",
) -> pd.Series:
    """Return strategy - benchmark on aligned dates."""
    aligned = align_dates(strategy_returns, benchmark_returns, fill_method=fill_method)
    return aligned["strategy"] - aligned["benchmark"]


def compute_cumulative_excess(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    fill_method: str = "forward",
) -> pd.Series:
    """Cumulative excess return series: prod(1 + strategy - benchmark) - 1."""
    excess = compute_excess_returns(strategy_returns, benchmark_returns, fill_method=fill_method)
    return (1.0 + excess).cumprod() - 1.0
