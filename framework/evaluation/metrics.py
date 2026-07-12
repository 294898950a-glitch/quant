"""Pure risk/return metrics for strategy evaluation.

Inputs accept either a list of floats or a pandas Series. All returns are
expected as simple (not log) returns unless otherwise noted.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd


# Tolerance for treating floating-point noise as zero in variance-based metrics.
_EPS = 1e-12


def _to_array(values: Sequence[float] | pd.Series) -> np.ndarray:
    """Convert input to a 1-D float numpy array, dropping NaNs."""
    if isinstance(values, pd.Series):
        arr = values.dropna().to_numpy(dtype=float)
    else:
        arr = np.asarray(values, dtype=float)
        arr = arr[~np.isnan(arr)]
    return arr.reshape(-1)


def cumulative_return(returns: Sequence[float] | pd.Series) -> float:
    """Geometric cumulative return from a simple return series."""
    arr = _to_array(returns)
    if len(arr) == 0:
        return 0.0
    return float(np.prod(1.0 + arr) - 1.0)


def total_return(returns: Sequence[float] | pd.Series) -> float:
    """Alias for cumulative_return."""
    return cumulative_return(returns)


def annualized_return(returns: Sequence[float] | pd.Series, periods_per_year: float = 252.0) -> float:
    """Annualized geometric return assuming `periods_per_year` periods."""
    arr = _to_array(returns)
    n = len(arr)
    if n == 0:
        return 0.0
    total = float(np.prod(1.0 + arr))
    if total <= 0.0:
        return -1.0
    return float(total ** (periods_per_year / n) - 1.0)


def volatility_annualized(returns: Sequence[float] | pd.Series, periods_per_year: float = 252.0) -> float:
    """Annualized standard deviation of returns."""
    arr = _to_array(returns)
    if len(arr) < 2:
        return 0.0
    std = float(np.std(arr, ddof=1))
    if abs(std) < _EPS or not math.isfinite(std):
        return 0.0
    return float(std * math.sqrt(periods_per_year))


def sharpe_ratio(
    returns: Sequence[float] | pd.Series,
    risk_free: float = 0.0,
    periods_per_year: float = 252.0,
) -> float:
    """Annualized Sharpe ratio: (mean return - risk_free) / std(returns)."""
    arr = _to_array(returns)
    if len(arr) < 2:
        return 0.0
    mean = float(np.mean(arr) - risk_free)
    std = float(np.std(arr, ddof=1))
    if abs(std) < _EPS or not math.isfinite(std):
        return 0.0
    return float(mean / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: Sequence[float] | pd.Series,
    risk_free: float = 0.0,
    periods_per_year: float = 252.0,
) -> float:
    """Annualized Sortino ratio using downside deviation."""
    arr = _to_array(returns)
    if len(arr) < 2:
        return 0.0
    mean = float(np.mean(arr) - risk_free)
    downside = arr[arr < 0.0]
    downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    if abs(downside_std) < _EPS or not math.isfinite(downside_std):
        if mean > 0.0:
            return float("inf")
        if mean < 0.0:
            return -float("inf")
        return 0.0
    return float(mean / downside_std * math.sqrt(periods_per_year))


def max_drawdown(returns_or_equity: Sequence[float] | pd.Series) -> float:
    """Maximum peak-to-trough drawdown from a return or equity series.

    If input looks like returns (values near zero, some negative), it is
    converted to an equity curve. If input is already an equity curve
    (all positive or starting near 1), it is used directly.
    """
    arr = _to_array(returns_or_equity)
    if len(arr) == 0:
        return 0.0

    # Heuristic: if all values are positive and the first is >= 0.5,
    # treat as equity curve; otherwise convert returns to equity.
    if np.all(arr > 0.0) and arr[0] >= 0.5:
        equity = arr
    else:
        equity = np.cumprod(1.0 + arr)

    if equity[0] == 0.0:
        return 0.0

    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    return float(np.min(drawdowns))


def calmar_ratio(annualized_return_value: float, max_drawdown_value: float) -> float:
    """Calmar ratio = annualized return / |max drawdown|."""
    dd = abs(max_drawdown_value)
    if dd == 0.0 or not math.isfinite(dd):
        return 0.0
    return float(annualized_return_value / dd)


def win_rate(trades: Sequence[dict[str, Any]] | pd.DataFrame) -> float:
    """Fraction of trades with positive realized PnL."""
    if isinstance(trades, pd.DataFrame):
        if trades.empty:
            return 0.0
        pnls = trades["realized_pnl"].dropna().to_numpy(dtype=float)
    else:
        pnls = np.array([float(t.get("realized_pnl", 0.0)) for t in trades], dtype=float)
    n = len(pnls)
    if n == 0:
        return 0.0
    return float(np.sum(pnls > 0.0) / n)


def profit_factor(trades: Sequence[dict[str, Any]] | pd.DataFrame) -> float:
    """Gross profits / gross losses."""
    if isinstance(trades, pd.DataFrame):
        if trades.empty:
            return 0.0
        pnls = trades["realized_pnl"].dropna().to_numpy(dtype=float)
    else:
        pnls = np.array([float(t.get("realized_pnl", 0.0)) for t in trades], dtype=float)
    gross_profit = float(np.sum(pnls[pnls > 0.0]))
    gross_loss = float(np.sum(np.abs(pnls[pnls < 0.0])))
    if gross_loss == 0.0:
        return float("inf") if gross_profit > 0.0 else 0.0
    return float(gross_profit / gross_loss)


def avg_win_loss(trades: Sequence[dict[str, Any]] | pd.DataFrame) -> dict[str, float]:
    """Average winning and losing trade PnL."""
    if isinstance(trades, pd.DataFrame):
        if trades.empty:
            return {"avg_win": 0.0, "avg_loss": 0.0}
        pnls = trades["realized_pnl"].dropna().to_numpy(dtype=float)
    else:
        pnls = np.array([float(t.get("realized_pnl", 0.0)) for t in trades], dtype=float)
    wins = pnls[pnls > 0.0]
    losses = pnls[pnls < 0.0]
    return {
        "avg_win": float(np.mean(wins)) if len(wins) > 0 else 0.0,
        "avg_loss": float(np.mean(losses)) if len(losses) > 0 else 0.0,
    }


def avg_hold_days(trades: Sequence[dict[str, Any]] | pd.DataFrame) -> float:
    """Average holding period in days."""
    if isinstance(trades, pd.DataFrame):
        if trades.empty or "hold_days" not in trades.columns:
            return 0.0
        values = trades["hold_days"].dropna().to_numpy(dtype=float)
    else:
        values = np.array([float(t.get("hold_days", 0.0)) for t in trades], dtype=float)
    if len(values) == 0:
        return 0.0
    return float(np.mean(values))
