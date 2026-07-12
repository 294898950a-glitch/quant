"""Aggregate backtest results into a standardized metrics dict.

The output shape is designed to match the fields consumed by
`data/*/report.yaml` and the review pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from framework.evaluation import benchmark, costs, metrics


@dataclass
class BacktestResult:
    """Container for backtest outputs."""

    total_return: float = 0.0
    excess_return: float = 0.0
    cumulative_excess_return: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    volatility_annualized: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_hold_days: float = 0.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    benchmark_total_return: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a flat dict matching report.yaml expectations."""
        return {
            "total_return": self.total_return,
            "excess_return": self.excess_return,
            "cumulative_excess_return": self.cumulative_excess_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown": self.max_drawdown,
            "calmar": self.calmar,
            "volatility_annualized": self.volatility_annualized,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "avg_hold_days": self.avg_hold_days,
            "cost_breakdown": self.cost_breakdown,
            "benchmark_total_return": self.benchmark_total_return,
        }


def _daily_returns_from_pnl(
    daily_pnl: pd.Series,
    initial_capital: float = 1.0,
) -> pd.Series:
    """Convert a daily PnL series into simple daily returns."""
    if daily_pnl.empty:
        return pd.Series(dtype=float)
    capital = float(initial_capital)
    returns = daily_pnl / capital
    returns.name = "strategy_return"
    return returns


def compute_backtest_metrics(
    trades: list[dict[str, Any]] | pd.DataFrame | None = None,
    daily_pnl: pd.Series | None = None,
    daily_returns: pd.Series | None = None,
    benchmark_returns: pd.Series | None = None,
    cost_config: costs.CostConfig | None = None,
    initial_capital: float = 1.0,
    periods_per_year: float = 252.0,
) -> BacktestResult:
    """Compute standardized backtest metrics from trades and/or return series.

    Args:
        trades: List of trade dicts or DataFrame with columns
            ``realized_pnl`` and optionally ``hold_days``.
        daily_pnl: Date-indexed daily PnL series. Either this or
            ``daily_returns`` must be provided.
        daily_returns: Date-indexed simple daily return series.
        benchmark_returns: Optional date-indexed benchmark return series.
        cost_config: Optional cost configuration for cost breakdown.
        initial_capital: Capital used to convert PnL to returns.
        periods_per_year: Trading periods per year (252 for daily).

    Returns:
        BacktestResult dataclass.
    """
    if daily_returns is None and daily_pnl is not None:
        daily_returns = _daily_returns_from_pnl(daily_pnl, initial_capital)

    if daily_returns is None or daily_returns.empty:
        return BacktestResult()

    result = BacktestResult()

    # Time-series metrics
    result.total_return = metrics.total_return(daily_returns)
    result.volatility_annualized = metrics.volatility_annualized(
        daily_returns, periods_per_year
    )
    result.sharpe = metrics.sharpe_ratio(daily_returns, periods_per_year=periods_per_year)
    result.sortino = metrics.sortino_ratio(daily_returns, periods_per_year=periods_per_year)
    result.max_drawdown = metrics.max_drawdown(daily_returns)
    annualized_ret = metrics.annualized_return(daily_returns, periods_per_year)
    result.calmar = metrics.calmar_ratio(annualized_ret, result.max_drawdown)

    # Benchmark / excess
    if benchmark_returns is not None:
        result.excess_return = float(
            benchmark.compute_excess_returns(daily_returns, benchmark_returns).sum()
        )
        result.cumulative_excess_return = float(
            benchmark.compute_cumulative_excess(daily_returns, benchmark_returns).iloc[-1]
        )
        result.benchmark_total_return = metrics.total_return(benchmark_returns)

    # Trade-level metrics
    if trades is not None and len(trades) > 0:
        if isinstance(trades, list):
            trades_df = pd.DataFrame(trades)
        else:
            trades_df = trades
        result.total_trades = int(len(trades_df))
        result.win_rate = metrics.win_rate(trades_df)
        result.profit_factor = metrics.profit_factor(trades_df)
        wl = metrics.avg_win_loss(trades_df)
        result.avg_win = wl["avg_win"]
        result.avg_loss = wl["avg_loss"]
        result.avg_hold_days = metrics.avg_hold_days(trades_df)

    # Cost breakdown (simplified estimate if trades not provided)
    if cost_config is not None:
        result.cost_breakdown = {
            "fee_pct": cost_config.fee_pct,
            "slippage_pct": cost_config.slippage_pct,
            "market_impact_coeff": cost_config.market_impact_coeff,
            "holding_cost_pct": cost_config.holding_cost_pct,
            "cost_model_enabled": cost_config.cost_model_enabled,
        }

    return result
