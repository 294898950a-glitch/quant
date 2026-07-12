"""Tests for framework.evaluation.backtest_metrics using unittest."""

from __future__ import annotations

import unittest

import pandas as pd

from framework.evaluation import backtest_metrics, costs


def _series(values: list[float], start: str = "20240101") -> pd.Series:
    dates = pd.date_range(start=start, periods=len(values), freq="D").strftime("%Y%m%d")
    return pd.Series(values, index=dates)


class TestComputeBacktestMetrics(unittest.TestCase):
    def test_empty_returns(self) -> None:
        result = backtest_metrics.compute_backtest_metrics(daily_returns=pd.Series(dtype=float))
        self.assertEqual(result.total_trades, 0)
        self.assertEqual(result.total_return, 0.0)

    def test_basic_metrics(self) -> None:
        daily_returns = _series([0.01, 0.02, -0.01, 0.005, 0.0])
        result = backtest_metrics.compute_backtest_metrics(daily_returns=daily_returns)
        expected_total = (1.01 * 1.02 * 0.99 * 1.005 * 1.0) - 1
        self.assertAlmostEqual(result.total_return, expected_total, places=6)
        self.assertEqual(result.total_trades, 0)
        self.assertGreaterEqual(result.sharpe, 0.0)

    def test_with_trades(self) -> None:
        daily_returns = _series([0.01, 0.02, -0.01, 0.005, 0.0])
        trades = [
            {"realized_pnl": 100.0, "hold_days": 5},
            {"realized_pnl": -50.0, "hold_days": 3},
            {"realized_pnl": 200.0, "hold_days": 7},
        ]
        result = backtest_metrics.compute_backtest_metrics(
            daily_returns=daily_returns, trades=trades
        )
        self.assertEqual(result.total_trades, 3)
        self.assertAlmostEqual(result.win_rate, 2 / 3, places=6)
        self.assertAlmostEqual(result.avg_hold_days, 5.0, places=6)

    def test_with_pnl(self) -> None:
        daily_pnl = _series([100.0, -50.0, 200.0, 0.0, -30.0])
        result = backtest_metrics.compute_backtest_metrics(
            daily_pnl=daily_pnl, initial_capital=10000.0
        )
        daily_returns = [v / 10000.0 for v in [100.0, -50.0, 200.0, 0.0, -30.0]]
        expected_total = float((1.0 + pd.Series(daily_returns)).prod() - 1.0)
        self.assertAlmostEqual(result.total_return, expected_total, places=6)

    def test_with_benchmark(self) -> None:
        daily_returns = _series([0.01, 0.02, -0.01, 0.005, 0.0])
        benchmark_returns = _series([0.005, 0.005, 0.005, 0.005, 0.005])
        result = backtest_metrics.compute_backtest_metrics(
            daily_returns=daily_returns, benchmark_returns=benchmark_returns
        )
        self.assertAlmostEqual(result.benchmark_total_return, (1.005**5) - 1, places=6)
        self.assertNotEqual(result.excess_return, 0.0)

    def test_with_cost_config(self) -> None:
        daily_returns = _series([0.01, 0.02, -0.01, 0.005, 0.0])
        cfg = costs.CostConfig(fee_pct=0.001)
        result = backtest_metrics.compute_backtest_metrics(
            daily_returns=daily_returns, cost_config=cfg
        )
        self.assertEqual(result.cost_breakdown["fee_pct"], 0.001)

    def test_to_dict(self) -> None:
        daily_returns = _series([0.01, 0.02, -0.01])
        result = backtest_metrics.compute_backtest_metrics(daily_returns=daily_returns)
        d = result.to_dict()
        self.assertIn("total_return", d)
        self.assertIn("sharpe", d)
        self.assertIn("max_drawdown", d)


if __name__ == "__main__":
    unittest.main()
