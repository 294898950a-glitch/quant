"""Integration tests for evaluator + benchmark cache plumbing."""

from __future__ import annotations

import pandas as pd

from strategies.cb_redemption.benchmarks import (
    BenchmarkConfig,
    load_benchmarks,
    write_benchmark_cache,
)
from strategies.cb_redemption.evaluator import (
    EvaluationConfig,
    evaluate,
    format_evaluation_report,
)


def _prices(base: float, daily_step: float, n: int = 40) -> pd.Series:
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series([base + daily_step * i for i in range(n)], index=idx)


def test_cached_benchmarks_feed_evaluator_and_report(tmp_path) -> None:
    bench_cfg = BenchmarkConfig(cache_dir=tmp_path)
    write_benchmark_cache("csi300", _prices(100.0, 0.03), config=bench_cfg)
    write_benchmark_cache("dividend", _prices(100.0, 0.04), config=bench_cfg)
    write_benchmark_cache("sixty_forty", _prices(100.0, 0.02), config=bench_cfg)
    write_benchmark_cache("cb_equal", _prices(100.0, 0.01), config=bench_cfg)

    benchmarks = load_benchmarks(
        ["cash", "cb_equal", "csi300", "dividend", "sixty_forty"],
        "2024-01-01",
        "2024-02-23",
        config=bench_cfg,
    )
    strategy = pd.Series(0.001, index=benchmarks["cb_equal"].index)

    result = evaluate(strategy, benchmarks, EvaluationConfig())
    report = format_evaluation_report(result, title="Cached Benchmark Smoke")

    assert result.cumulative_curves.index.equals(result.metrics_table.index) is False
    assert result.cumulative_curves.iloc[0]["strategy"] == 1.0
    assert "Cached Benchmark Smoke" in report
    assert "cb_equal" in report
    assert "dividend" in report
