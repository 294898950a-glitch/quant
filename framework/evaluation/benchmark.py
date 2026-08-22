"""Benchmark loading, alignment, and excess-return calculation.

The default benchmark is the project-wide CB equal-weight index maintained in
`strategies/cb_arb/verifier.py`. Other benchmarks can be supplied as date-indexed
pandas Series of simple daily returns.
"""

from __future__ import annotations

from typing import Callable, Protocol

import pandas as pd


class BenchmarkProvider(Protocol):
    def get_returns(self, benchmark_id: str, start: str | None, end: str | None) -> pd.Series:
        ...

    def get_total_return(self, benchmark_id: str, start: str, end: str) -> float:
        ...


def load_benchmark(
    start_date: str | None = None,
    end_date: str | None = None,
    benchmark_id: str = "cb_equal_weight",
    provider: BenchmarkProvider | None = None,
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
    if provider is None:
        raise ValueError("benchmark provider is required; inject a strategy-specific provider explicitly")
    returns = provider.get_returns(benchmark_id, start_date, end_date)
    if not isinstance(returns, pd.Series):
        raise TypeError("benchmark provider must return pandas Series")
    result = returns.copy()
    result.name = "benchmark_return"
    return result


def load_benchmark_total_return(
    start_date: str,
    end_date: str,
    benchmark_id: str = "cb_equal_weight",
    provider: BenchmarkProvider | None = None,
) -> float:
    """Total benchmark return over [start_date, end_date]."""
    if provider is None:
        raise ValueError("benchmark provider is required; inject a strategy-specific provider explicitly")
    return float(provider.get_total_return(benchmark_id, start_date, end_date))


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
