"""Tests for framework.evaluation.metrics using unittest."""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from framework.evaluation import metrics


class TestCumulativeReturn(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(metrics.cumulative_return([]), 0.0)

    def test_constant_positive(self) -> None:
        # 1% per day for 10 days
        self.assertAlmostEqual(metrics.cumulative_return([0.01] * 10), 0.104622, places=4)

    def test_list_and_series_equal(self) -> None:
        data = [0.01, -0.005, 0.02, 0.0]
        self.assertEqual(
            metrics.cumulative_return(data),
            metrics.cumulative_return(pd.Series(data)),
        )


class TestAnnualizedReturn(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(metrics.annualized_return([0.0] * 252), 0.0)

    def test_daily_to_annual(self) -> None:
        # geometric annualization from daily returns
        daily = 0.1 / 252
        self.assertAlmostEqual(
            metrics.annualized_return([daily] * 252), (1.0 + daily) ** 252 - 1.0, places=3
        )


class TestVolatility(unittest.TestCase):
    def test_constant(self) -> None:
        self.assertEqual(metrics.volatility_annualized([0.01] * 100), 0.0)

    def test_random_series(self) -> None:
        rng = np.random.default_rng(42)
        returns = rng.normal(0.0, 0.02, 252)
        vol = metrics.volatility_annualized(returns)
        self.assertGreater(vol, 0.0)
        self.assertTrue(math.isfinite(vol))


class TestSharpeRatio(unittest.TestCase):
    def test_no_variance(self) -> None:
        self.assertEqual(metrics.sharpe_ratio([0.01] * 10), 0.0)

    def test_positive_sharpe(self) -> None:
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.01, 252)
        sharpe = metrics.sharpe_ratio(returns)
        self.assertGreater(sharpe, 0.0)
        self.assertTrue(math.isfinite(sharpe))


class TestSortinoRatio(unittest.TestCase):
    def test_no_downside(self) -> None:
        self.assertEqual(metrics.sortino_ratio([0.01] * 10), float("inf"))

    def test_with_downside(self) -> None:
        returns = [0.02, -0.01, 0.015, -0.02, 0.01]
        sortino = metrics.sortino_ratio(returns, periods_per_year=252)
        self.assertTrue(math.isfinite(sortino))


class TestMaxDrawdown(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(metrics.max_drawdown([]), 0.0)

    def test_from_returns(self) -> None:
        returns = [0.1, -0.2, 0.05, 0.05]
        # equity: 1.1 -> 0.88 after the -20% day -> max drawdown = -0.2
        self.assertAlmostEqual(metrics.max_drawdown(returns), -0.2, places=6)

    def test_from_equity(self) -> None:
        equity = [1.0, 1.1, 0.88, 0.9, 0.95]
        self.assertAlmostEqual(metrics.max_drawdown(equity), -0.2, places=6)


class TestCalmarRatio(unittest.TestCase):
    def test_zero_drawdown(self) -> None:
        self.assertEqual(metrics.calmar_ratio(0.1, 0.0), 0.0)

    def test_positive(self) -> None:
        self.assertAlmostEqual(metrics.calmar_ratio(0.1, -0.2), 0.5, places=6)


class TestTradeMetrics(unittest.TestCase):
    def test_win_rate(self) -> None:
        trades = [
            {"realized_pnl": 100.0},
            {"realized_pnl": -50.0},
            {"realized_pnl": 200.0},
        ]
        self.assertAlmostEqual(metrics.win_rate(trades), 2 / 3, places=6)

    def test_profit_factor(self) -> None:
        trades = [
            {"realized_pnl": 100.0},
            {"realized_pnl": -50.0},
            {"realized_pnl": 200.0},
        ]
        self.assertAlmostEqual(metrics.profit_factor(trades), 300 / 50, places=6)

    def test_avg_win_loss(self) -> None:
        trades = [
            {"realized_pnl": 100.0},
            {"realized_pnl": -50.0},
            {"realized_pnl": 200.0},
        ]
        result = metrics.avg_win_loss(trades)
        self.assertAlmostEqual(result["avg_win"], 150.0, places=6)
        self.assertAlmostEqual(result["avg_loss"], -50.0, places=6)

    def test_avg_hold_days(self) -> None:
        trades = [
            {"hold_days": 5},
            {"hold_days": 10},
            {"hold_days": 15},
        ]
        self.assertAlmostEqual(metrics.avg_hold_days(trades), 10.0, places=6)


if __name__ == "__main__":
    unittest.main()
