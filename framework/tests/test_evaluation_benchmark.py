"""Tests for framework.evaluation.benchmark using unittest."""

from __future__ import annotations

import unittest

import pandas as pd

from framework.evaluation import benchmark


def _series(values: list[float], start: str = "20240101") -> pd.Series:
    dates = pd.date_range(start=start, periods=len(values), freq="D").strftime("%Y%m%d")
    return pd.Series(values, index=dates)


class TestAlignDates(unittest.TestCase):
    def test_basic_alignment(self) -> None:
        s = _series([0.01, 0.02, -0.01])
        b = _series([0.005, 0.005, 0.005])
        aligned = benchmark.align_dates(s, b)
        self.assertEqual(list(aligned.columns), ["strategy", "benchmark"])
        self.assertEqual(len(aligned), 3)

    def test_missing_values_forward_fill(self) -> None:
        s = _series([0.01, 0.02, -0.01])
        b = _series([0.005, 0.005])
        aligned = benchmark.align_dates(s, b)
        self.assertAlmostEqual(aligned["benchmark"].iloc[-1], 0.005, places=6)

    def test_zero_fill(self) -> None:
        s = _series([0.01, 0.02, -0.01])
        b = _series([0.005, 0.005])
        aligned = benchmark.align_dates(s, b, fill_method="zero")
        self.assertEqual(aligned["benchmark"].iloc[-1], 0.0)


class TestComputeExcessReturns(unittest.TestCase):
    def test_simple(self) -> None:
        s = _series([0.01, 0.02, -0.01])
        b = _series([0.005, 0.005, 0.005])
        excess = benchmark.compute_excess_returns(s, b)
        self.assertAlmostEqual(excess.iloc[0], 0.005, places=6)
        self.assertAlmostEqual(excess.iloc[1], 0.015, places=6)
        self.assertAlmostEqual(excess.iloc[2], -0.015, places=6)


class TestComputeCumulativeExcess(unittest.TestCase):
    def test_simple(self) -> None:
        s = _series([0.10, 0.10, 0.10])
        b = _series([0.05, 0.05, 0.05])
        cum = benchmark.compute_cumulative_excess(s, b)
        # excess per day = 0.05; cumulative = 1.05^3 - 1
        self.assertAlmostEqual(cum.iloc[-1], 1.05**3 - 1, places=6)


class TestLoadBenchmarkTotalReturn(unittest.TestCase):
    def test_invalid_benchmark(self) -> None:
        with self.assertRaises(ValueError):
            benchmark.load_benchmark_total_return(
                "20240101", "20240105", benchmark_id="unknown"
            )


if __name__ == "__main__":
    unittest.main()
