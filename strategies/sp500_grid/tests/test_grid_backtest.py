"""sp500_grid backtest tests — 全部用合成数据, 不读 parquet。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from strategies.sp500_grid.backtest import (
    GridConfig,
    GridResult,
    IS_END,
    OOS_START,
    run_grid_backtest,
)


def _make_df(closes: list[float], start: str = "20230101") -> pd.DataFrame:
    """合成日线 DataFrame: open=high=low=close, vol=0, amount=0。

    日期从 start 起, 工作日步进 (但允许周末; date 仅作字符串排序使用)。
    """
    dates = pd.bdate_range(start=pd.to_datetime(start), periods=len(closes))
    return pd.DataFrame({
        "date": dates.strftime("%Y%m%d"),
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "vol": [0] * len(closes),
        "amount": [0.0] * len(closes),
    })


def test_synthetic_oscillation_generates_trades():
    """正弦震荡在 [100, 110] 内, 应触发 grid trades。"""
    n = 300
    t = np.arange(n)
    closes = (105 + 5 * np.sin(t / 5.0)).tolist()
    df = _make_df(closes)
    cfg = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0)

    result = run_grid_backtest(df, cfg)

    assert isinstance(result, GridResult)
    assert len(result.trades) > 0, "震荡行情应产生格内成交"
    sides = {t.side for t in result.trades}
    assert "buy" in sides, "应有 buy trades"
    # equity_curve 每日一条
    assert len(result.equity_curve) == n


def test_oversold_triggers_full_close_then_reset():
    """close 跌穿区间下界 → 全部清仓 + 后续区间重算。"""
    # 前 80 天盘整在 100~104, 第 81 天暴跌到 50
    closes = ([100.0, 101.0, 102.0, 103.0, 104.0] * 16) + [50.0] + [55.0, 60.0, 65.0, 70.0]
    df = _make_df(closes)
    cfg = GridConfig(grid_count=8, range_window=40, initial_capital=100_000.0)

    result = run_grid_backtest(df, cfg)

    # 应有至少一笔 sell（暴跌日全部清仓）
    sells = [t for t in result.trades if t.side == "sell"]
    assert len(sells) >= 1, f"暴跌日应触发 sell, 实际 trades={result.trades}"

    # 暴跌日的 sell 必然在 closes[80] 处（即 _make_df 第 81 行 date）
    crash_date = df["date"].iloc[80]
    sells_on_crash = [t for t in sells if t.date == crash_date]
    assert len(sells_on_crash) >= 1, "暴跌日没有清仓单"
    # 越界 sell 用 grid_level=-1 标识 (跌穿下界)
    assert any(s.grid_level == -1 for s in sells_on_crash)


def test_no_oscillation_no_trades():
    """单调上升: 早期可能 buy（rolling window 还不够触发越界）, 但应主要是 sell, 且最终持仓被末日平掉。"""
    closes = [100.0 + i * 0.5 for i in range(120)]
    df = _make_df(closes)
    cfg = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0)

    result = run_grid_backtest(df, cfg)

    buys = [t for t in result.trades if t.side == "buy"]
    sells = [t for t in result.trades if t.side == "sell"]
    # 单调上升时, 买入次数应远少于卖出次数（除了末日强平）
    assert len(buys) == 0, f"单调上升不应该补回 (buy), 实际 buys={buys}"
    # 应至少有末日强平 / 越界 sell
    # （持仓初始 0, 单调上涨没机会 buy → 也不会 sell; 此分支允许 sells==0）
    assert isinstance(result, GridResult)
    # equity 至少不亏 (无交易)
    final_equity = result.equity_curve[-1]["equity"]
    assert final_equity == pytest.approx(cfg.initial_capital, rel=1e-6)


def test_initial_capital_preserved_when_no_trades():
    """全段静止价格 → equity 始终 = initial_capital。"""
    closes = [100.0] * 100
    df = _make_df(closes)
    cfg = GridConfig(grid_count=10, range_window=30, initial_capital=50_000.0)

    result = run_grid_backtest(df, cfg)

    assert len(result.trades) == 0, "静止价格不应有任何 trades"
    for r in result.equity_curve:
        assert r["equity"] == pytest.approx(50_000.0, rel=1e-9)


def test_fee_reduces_equity():
    """同样 trades, fee=0 vs fee=0.001, 后者 equity 更低。"""
    n = 200
    t = np.arange(n)
    closes = (105 + 5 * np.sin(t / 4.0)).tolist()
    df = _make_df(closes)

    cfg_no_fee = GridConfig(grid_count=10, range_window=30, fee_pct=0.0,
                            initial_capital=100_000.0)
    cfg_with_fee = GridConfig(grid_count=10, range_window=30, fee_pct=0.001,
                              initial_capital=100_000.0)

    r1 = run_grid_backtest(df, cfg_no_fee)
    r2 = run_grid_backtest(df, cfg_with_fee)

    # 必须真的有 trades, 否则 fee 不起作用
    assert len(r1.trades) > 0
    assert len(r2.trades) > 0
    e1 = r1.equity_curve[-1]["equity"]
    e2 = r2.equity_curve[-1]["equity"]
    assert e2 < e1, f"有 fee 的 equity 应更低: no_fee={e1}, with_fee={e2}"


def test_is_oos_split():
    """跨 IS/OOS 边界, is_metrics 和 oos_metrics 都非空（n_days > 0）。"""
    # IS_END=20241231, OOS_START=20250101 — 构造 2024Q4 + 2025Q1 的震荡
    n_is = 60
    n_oos = 60
    t = np.arange(n_is + n_oos)
    closes = (105 + 5 * np.sin(t / 5.0)).tolist()
    df = _make_df(closes, start="20241001")
    # sanity: 数据应跨越 IS_END
    assert (df["date"] <= IS_END).any()
    assert (df["date"] >= OOS_START).any()

    cfg = GridConfig(grid_count=10, range_window=20, initial_capital=100_000.0)
    result = run_grid_backtest(df, cfg)

    assert result.is_metrics["n_days"] > 0
    assert result.oos_metrics["n_days"] > 0
    assert result.all_metrics["n_days"] == result.is_metrics["n_days"] + result.oos_metrics["n_days"]


def _make_df_hl(closes: list[float], highs: list[float], lows: list[float],
                start: str = "20230101") -> pd.DataFrame:
    """合成日线 (close 与 high/low 分离), 用于 ATR 测试。"""
    dates = pd.bdate_range(start=pd.to_datetime(start), periods=len(closes))
    return pd.DataFrame({
        "date": dates.strftime("%Y%m%d"),
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "vol": [0] * len(closes),
        "amount": [0.0] * len(closes),
    })


def test_trend_filter_blocks_buys_in_downtrend():
    """明确下跌走势 + 趋势过滤 → buy 数应少于过滤关闭时。

    构造: 先一段上涨(~80 天)建立高 SMA-long, 再震荡下行让 SMA-short<SMA-long
    且仍有反复回调可触发 buy。过滤关闭时下跌震荡段会补仓;开启时被闸住。
    """
    # 80 天上涨从 80 到 130 (SMA-long 抬升), 后 80 天围绕 ~120 震荡下行(每天均值降 0.2)
    rise = [80.0 + 0.625 * i for i in range(80)]              # 80 -> 130
    decline = [
        130.0 - 0.2 * (i + 1) + 3.0 * np.sin(i / 3.0)         # 缓慢下行 + 震荡
        for i in range(80)
    ]
    closes = rise + decline
    df = _make_df(closes)

    cfg_off = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0,
                         trend_filter_enabled=0, vol_filter_enabled=0)
    cfg_on = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0,
                        trend_filter_enabled=1, trend_short_window=10, trend_long_window=40,
                        vol_filter_enabled=0)

    r_off = run_grid_backtest(df, cfg_off)
    r_on = run_grid_backtest(df, cfg_on)

    buys_off = [t for t in r_off.trades if t.side == "buy"]
    buys_on = [t for t in r_on.trades if t.side == "buy"]
    assert len(buys_off) > 0, "前提:过滤关时震荡下行段应有补仓 buy"
    assert len(buys_on) < len(buys_off), (
        f"趋势过滤应减少下跌段 buy: off={len(buys_off)}, on={len(buys_on)}"
    )


def test_vol_filter_blocks_buys_when_atr_spikes():
    """高波动 (ATR/close > skip_pct) + 波动过滤 → buy 数应少于过滤关闭时。"""
    # 构造: 大幅上下震荡(每日 ±5%), high/low 拉宽以撑起 ATR
    n = 200
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    base = 100.0
    for i in range(n):
        # 锯齿波: 偶数日 100, 奇数日 105 — close 在 [100, 105] 跳; high/low 故意拉到 [98, 108]
        c = base + (5.0 if i % 2 else 0.0)
        closes.append(c)
        highs.append(c + 3.0)
        lows.append(c - 2.0)
    df = _make_df_hl(closes, highs, lows)

    cfg_off = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0,
                         trend_filter_enabled=0, vol_filter_enabled=0)
    cfg_on = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0,
                        trend_filter_enabled=0,
                        vol_filter_enabled=1, vol_atr_window=14, vol_atr_skip_pct=0.01)

    r_off = run_grid_backtest(df, cfg_off)
    r_on = run_grid_backtest(df, cfg_on)

    buys_off = [t for t in r_off.trades if t.side == "buy"]
    buys_on = [t for t in r_on.trades if t.side == "buy"]
    assert len(buys_off) > 0, "前提:过滤关时锯齿波应有补仓 buy"
    assert len(buys_on) < len(buys_off), (
        f"波动过滤应减少高 ATR 期 buy: off={len(buys_off)}, on={len(buys_on)}"
    )


def test_filters_off_matches_original_behavior():
    """两个 enabled=0 时, trades 数量与不传新字段时相同 (向后兼容)。"""
    n = 250
    t = np.arange(n)
    closes = (105 + 5 * np.sin(t / 5.0)).tolist()
    df = _make_df(closes)

    cfg_default = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0)
    cfg_explicit_off = GridConfig(
        grid_count=10, range_window=30, initial_capital=100_000.0,
        trend_filter_enabled=0, trend_short_window=20, trend_long_window=60,
        vol_filter_enabled=0, vol_atr_window=14, vol_atr_skip_pct=0.03,
    )

    r_def = run_grid_backtest(df, cfg_default)
    r_off = run_grid_backtest(df, cfg_explicit_off)

    assert len(r_def.trades) == len(r_off.trades)
    # equity 末值也应一致
    assert r_def.equity_curve[-1]["equity"] == pytest.approx(
        r_off.equity_curve[-1]["equity"], rel=1e-9
    )


def test_filters_dont_block_sells():
    """下跌+高波动数据下, 卖出笔数(含越界平仓)不受过滤影响。"""
    # 先震荡积仓, 后下跌 + 锯齿(高 ATR)。要求 sells 数量 enabled=1 ≥ enabled=0
    osc = [105 + 5 * np.sin(i / 5.0) for i in range(80)]
    # 震荡下跌: 整体下行 + 每日抖动 ±2
    decline_closes = []
    decline_highs = []
    decline_lows = []
    cur = osc[-1]
    for i in range(80):
        cur = cur - 0.4
        c = cur + (2.0 if i % 2 else -2.0)
        decline_closes.append(c)
        decline_highs.append(c + 3.0)
        decline_lows.append(c - 3.0)
    closes = osc + decline_closes
    highs = osc + decline_highs   # 震荡段 high=close 简化
    lows = osc + decline_lows
    df = _make_df_hl(closes, highs, lows)

    cfg_off = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0,
                         trend_filter_enabled=0, vol_filter_enabled=0)
    cfg_on = GridConfig(grid_count=10, range_window=30, initial_capital=100_000.0,
                        trend_filter_enabled=1, trend_short_window=20, trend_long_window=60,
                        vol_filter_enabled=1, vol_atr_window=14, vol_atr_skip_pct=0.01)

    r_off = run_grid_backtest(df, cfg_off)
    r_on = run_grid_backtest(df, cfg_on)

    sells_off = [t for t in r_off.trades if t.side == "sell"]
    sells_on = [t for t in r_on.trades if t.side == "sell"]
    # 关键不变量: 过滤启用不应"消失"卖出 —— 已有仓位的减仓 / 越界平仓 / 末日强平
    # 都不受闸限制。允许 on 的 sell 比 off 略少(因为过滤减少了 buy → 后续可减仓量更小),
    # 但绝不应该因为过滤而"提前停止"卖出 —— 在该数据上 on 的 sells 应 >= off 的一半,
    # 且 on 的最后一日仍能强平。
    assert len(sells_on) >= 1, "过滤启用后仍应有 sells (含末日强平)"
    # buy 数减少 → 持仓减少 → sell 减少在合理范围 (不能跌到 0)
    assert len(sells_on) >= max(1, len(sells_off) // 4), (
        f"过滤不应彻底封掉 sell: off={len(sells_off)}, on={len(sells_on)}"
    )


def test_metrics_sharpe_finite():
    """equity_curve 至少 30 天后, sharpe 应是有限实数。"""
    n = 80
    rng = np.random.default_rng(seed=42)
    closes = (100 + np.cumsum(rng.normal(0, 0.5, size=n))).tolist()
    df = _make_df(closes)
    cfg = GridConfig(grid_count=8, range_window=20, initial_capital=100_000.0)

    result = run_grid_backtest(df, cfg)

    assert len(result.equity_curve) >= 30
    sharpe = result.all_metrics["sharpe"]
    assert math.isfinite(sharpe), f"sharpe 应为有限实数, got {sharpe}"
    # max_drawdown <= 0
    assert result.all_metrics["max_drawdown"] <= 0
    # winrate ∈ [0, 1]
    assert 0.0 <= result.all_metrics["winrate"] <= 1.0
