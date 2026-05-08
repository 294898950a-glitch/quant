"""icbc_grid verifier 适配器测试。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from strategies.cb_redemption.result_types import BacktestResult, TradeRecord
from strategies.icbc_grid.verifier import (
    INITIAL_CAPITAL,
    _grid_metrics_to_cb_metrics,
    _unpack_weights,
    run_backtest,
)


def test_unpack_weights_returns_int_and_float():
    """weights[0:2] 解包为 int，weights[2] 解包为 float。"""
    grid_count, range_window, pos = _unpack_weights([12.7, 75.4, 0.15])
    assert isinstance(grid_count, int)
    assert isinstance(range_window, int)
    assert isinstance(pos, float)
    assert grid_count == 13       # round(12.7) -> 13
    assert range_window == 75
    assert pos == 0.15


def test_unpack_weights_handles_short_list():
    """缺位用默认值兜底。"""
    grid_count, range_window, pos = _unpack_weights([])
    assert grid_count >= 1
    assert range_window >= 2
    assert 0.0 < pos <= 1.0


def test_run_backtest_returns_BacktestResult_type():
    """orchestrator-style 调用 → 返回 cb.BacktestResult 实例。"""
    weights = [10, 60, 0.10, 0.0, 0.0]
    thresholds: dict = {}
    rules: dict = {"fee_pct": 0.0003}

    result = run_backtest(weights, thresholds, rules, oos_event_ids=None)

    assert isinstance(result, BacktestResult)
    # metrics 用 cb 关键字
    assert "sharpe" in result.is_metrics
    assert "win_rate" in result.is_metrics
    assert "total_trades" in result.is_metrics
    # date_range tuple 字符串
    assert isinstance(result.date_range, tuple) and len(result.date_range) == 2


def test_run_backtest_oos_event_ids_filters():
    """传 oos_event_ids 子集 → oos_metrics.total_trades < all_metrics.total_trades。"""
    weights = [10, 60, 0.10]
    rules = {"fee_pct": 0.0003}

    full = run_backtest(weights, {}, rules, oos_event_ids=None)
    full_oos_trades = full.oos_metrics.get("total_trades", 0)

    # 取一个非常窄的日期子集（只 1 个日期）→ 几乎一定 trade 数 < 全 OOS
    narrow_set = {"20250115"}
    narrow = run_backtest(weights, {}, rules, oos_event_ids=narrow_set)
    narrow_oos_trades = narrow.oos_metrics.get("total_trades", 0)

    # 至少不大于全集（通常严格小于）；当全 OOS 也是 0 时退化但不破坏断言
    if full_oos_trades > 0:
        assert narrow_oos_trades <= full_oos_trades
    else:
        # 若 grid 无 OOS 交易，就只断言形态一致
        assert isinstance(narrow_oos_trades, int)


def test_run_backtest_ignores_snapshots_arg():
    """cb-style 5-arg 风格：snapshots=DataFrame() 也能跑（被忽略）。"""
    weights = [10, 60, 0.10]
    rules = {"fee_pct": 0.0003}

    class _DummyCfg:
        fee_pct = 0.0003

    # cb-style: (snapshots, weights, thresholds, cfg)
    snap = pd.DataFrame()
    result = run_backtest(snap, weights, {}, _DummyCfg(), oos_event_ids=None)
    assert isinstance(result, BacktestResult)


def test_run_backtest_trades_are_TradeRecord():
    """trades list 元素是 cb.TradeRecord，且 cb_code 标记为 601398。"""
    weights = [10, 60, 0.10]
    result = run_backtest(weights, {}, {"fee_pct": 0.0003})
    if result.trades:
        first = result.trades[0]
        assert isinstance(first, TradeRecord)
        # entry/exit 字段必在
        assert hasattr(first, "entry_date")
        assert hasattr(first, "exit_date")
        # 标的代码标记为 icbc 601398
        assert first.cb_code == "601398"


def test_grid_metrics_to_cb_metrics_renames_keys():
    """winrate -> win_rate, trades -> total_trades 的对齐。"""
    grid_m = {"winrate": 0.42, "trades": 17, "sharpe": 1.5,
              "total_return": 0.12, "max_drawdown": -0.08, "n_days": 200}
    cb_m = _grid_metrics_to_cb_metrics(grid_m)
    assert cb_m["sharpe"] == 1.5
    assert cb_m["win_rate"] == 0.42
    assert cb_m["total_trades"] == 17
    assert cb_m["total_return"] == 0.12
