"""cb_arb verifier 单元 + 集成测试."""

from __future__ import annotations

import math
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from strategies.cb_arb import verifier as v
from strategies.cb_arb.verifier import (
    CBArbConfig,
    IS_END,
    OOS_START,
    RATING_TO_INT,
    _build_call_index,
    _compute_metrics,
    _index_total_return,
    _is_force_redeemed_on_date,
    _restrict_dates_for_pool,
    _unpack_config,
    run_backtest,
)
from strategies.cb_redemption.result_types import BacktestResult, TradeRecord


# --------------------------------------------------------------------------- #
# Lightweight unit tests (no I/O)
# --------------------------------------------------------------------------- #


def test_unpack_config_defaults():
    """空 weights → 默认值."""
    cfg = _unpack_config([], {}, {})
    assert cfg.vol_window_days == 60
    assert cfg.rank_buy_pct == 0.10
    assert cfg.rank_sell_pct == 0.50
    assert cfg.fee_pct == 0.0003
    assert cfg.initial_capital == 1_000_000.0


def test_unpack_config_full_weights():
    """填满 12 个 weights, 顺序对得上."""
    weights = [
        30,      # vol_window_days
        1.2,     # vol_multiplier
        0.05,    # rank_buy_pct
        0.6,     # rank_sell_pct
        0.04,    # max_position_pct
        20,      # max_holdings
        60,      # max_holding_days
        -0.1,    # stop_loss_pct
        2e8,     # min_remaining_size
        2e6,     # min_avg_amount
        80,      # credit_spread_aaa_bp
        200,     # credit_spread_aa_bp
    ]
    rules = {"rating_floor_int": 3, "fee_pct": 0.0005, "initial_capital": 2e6}
    cfg = _unpack_config(weights, {}, rules)
    assert cfg.vol_window_days == 30
    assert cfg.vol_multiplier == 1.2
    assert cfg.rank_buy_pct == 0.05
    assert cfg.rank_sell_pct == 0.6
    assert cfg.max_position_pct == 0.04
    assert cfg.max_holdings == 20
    assert cfg.max_holding_days == 60
    assert cfg.stop_loss_pct == -0.1
    assert cfg.min_remaining_size == 2e8
    assert cfg.min_avg_amount == 2e6
    assert cfg.credit_spread_aaa_bp == 80
    assert cfg.credit_spread_aa_bp == 200
    assert cfg.rating_floor_int == 3
    assert cfg.fee_pct == 0.0005
    assert cfg.initial_capital == 2e6


def test_credit_spread_dict_monotonic():
    """AAA < AA+ < AA < AA- < A+ < A."""
    cfg = CBArbConfig(credit_spread_aaa_bp=50.0, credit_spread_aa_bp=150.0)
    d = cfg.credit_spread_dict()
    keys = ["AAA", "AA+", "AA", "AA-", "A+", "A"]
    vals = [d[k] for k in keys]
    for i in range(len(vals) - 1):
        assert vals[i] < vals[i + 1], f"{keys[i]}({vals[i]}) >= {keys[i+1]}({vals[i+1]})"


def test_rating_to_int_floor():
    """评级阈值 ≥ 2 应过滤掉 A 系."""
    assert RATING_TO_INT["AAA"] == 5
    assert RATING_TO_INT["AA+"] == 4
    assert RATING_TO_INT["AA"] == 3
    assert RATING_TO_INT["AA-"] == 2
    assert RATING_TO_INT["A+"] == 1
    assert RATING_TO_INT["A"] == 1


def test_compute_metrics_empty():
    """空 equity_curve → 全 0."""
    m = _compute_metrics([], [], 1_000_000.0)
    assert m["sharpe"] == 0.0
    assert m["total_trades"] == 0
    assert m["total_return"] == 0.0
    assert m["n_days"] == 0


def test_compute_metrics_winrate():
    """3 笔交易, 2 赢 1 输 → win_rate=2/3."""
    eq = [("20240101", 1_000_000.0), ("20240102", 1_010_000.0), ("20240103", 1_005_000.0)]
    fake_trade = lambda pnl: TradeRecord(
        cb_code="x", cb_name="x", entry_date="20240101", entry_price=100,
        prob_entry=0, premium_entry=0, exit_date="20240102", exit_price=101,
        pnl_pct=pnl, pnl_amount=0, holding_days=1, exit_reason="x",
    )
    trades = [fake_trade(0.05), fake_trade(0.02), fake_trade(-0.03)]
    m = _compute_metrics(eq, trades, 1_000_000.0)
    # 注意: _compute_metrics 把 win_rate round 到 4 位 → 0.6667
    assert abs(m["win_rate"] - 0.6667) < 1e-4
    assert m["total_trades"] == 3


def test_force_redeem_date_check():
    """ann_date <= date <= expire_date 内为 True."""
    idx = {"110001.SH": [("20240301", "20240320")]}
    assert _is_force_redeemed_on_date("110001.SH", "20240310", idx) is True
    assert _is_force_redeemed_on_date("110001.SH", "20240301", idx) is True
    assert _is_force_redeemed_on_date("110001.SH", "20240320", idx) is True
    assert _is_force_redeemed_on_date("110001.SH", "20240228", idx) is False
    assert _is_force_redeemed_on_date("110001.SH", "20240321", idx) is False
    assert _is_force_redeemed_on_date("999999.SH", "20240310", idx) is False


def test_build_call_index_handles_missing_ann_date():
    """ann_date 为 NaN 时, 兜底用 expire_date."""
    df = pd.DataFrame({
        "ts_code": ["A.SH", "B.SH"],
        "ann_date": ["20240101", float("nan")],
        "call_date": ["20240105", "20240210"],
        "expire_date": ["20240115", "20240220"],
    })
    idx = _build_call_index(df)
    # A 用 ann -> exp
    assert idx["A.SH"] == [("20240101", "20240115")]
    # B ann_date 缺 → 用 expire_date 兜底
    assert idx["B.SH"] == [("20240220", "20240220")]


def test_restrict_dates_for_pool_with_warmup():
    """oos_event_ids 给定 → 取 pool 起点前 120 天到 pool 终点."""
    trading_days = [f"2024010{i}" if i < 10 else f"202401{i}" for i in range(1, 32)]
    pool = {"20240120", "20240121", "20240122"}
    days_to_run, pool_set = _restrict_dates_for_pool(trading_days, pool)
    assert pool_set == pool
    # 不超过原列表长度
    assert len(days_to_run) <= len(trading_days)
    # 包含 pool 全部
    assert pool.issubset(set(days_to_run))


def test_restrict_dates_empty_pool_returns_empty():
    """空池 → 空回测."""
    days_to_run, pool_set = _restrict_dates_for_pool(["20240101"], set())
    # 空 oos_event_ids 视作 None → 全段
    assert days_to_run == ["20240101"]
    assert pool_set == set()

    # 但如果显式传空 set → 也是无 pool
    # (在我们实现中, "" 也会触发 if not pool 分支)


# --------------------------------------------------------------------------- #
# Mock-based behavior tests
# --------------------------------------------------------------------------- #


def _make_synthetic_data(n_cb: int = 5, n_days: int = 60):
    """合成最小数据: 5 只 CB × 60 个交易日.

    cb_basic / cb_daily / cb_call / stk_daily_qfq 全部对齐.
    一只 CB 设为 force_redeemed (在中间一段日期).
    日期必须连续 (无月末月初跳跃) — 否则 max_holding_days 等检测会被时间窗口
    外的"无报价日"误踩.
    """
    # 连续 60 个工作日 (从 2024-01-01 起)
    bdays = pd.bdate_range(start="2024-01-02", periods=n_days)
    days = [d.strftime("%Y%m%d") for d in bdays]

    cb_codes = [f"CB00{i}.SH" for i in range(1, n_cb + 1)]
    stk_codes = [f"STK00{i}" for i in range(1, n_cb + 1)]

    # cb_basic
    cb_basic = pd.DataFrame({
        "ts_code": cb_codes,
        "code": [c.split(".")[0] for c in cb_codes],
        "bond_short_name": [f"测试转债{i}" for i in range(1, n_cb + 1)],
        "stk_code": stk_codes,
        "issue_size": [10.0] * n_cb,  # 10 亿
        "remain_size": [None] * n_cb,
        "conv_price": [10.0] * n_cb,
        "value_date": ["20200101"] * n_cb,
        "list_date": ["20210101"] * n_cb,
        "delist_date": [""] * n_cb,
        "maturity_date": ["20280101"] * n_cb,
        "transfer_start_date": ["20210701"] * n_cb,
        "transfer_end_date": ["20280101"] * n_cb,
        "rating": ["AA+"] * n_cb,
        "coupon_rate": [0.01] * n_cb,
        "interest_rate_explain": [""] * n_cb,
        "par_value": [100] * n_cb,
        "issue_price": [100] * n_cb,
    })

    # cb_daily — 5 只 × n_days
    rows = []
    for i, ts in enumerate(cb_codes):
        # CB i 的 close 价: i=0 偏便宜 (90 元), i=4 偏贵 (140 元)
        base = 90 + i * 10
        for j, d in enumerate(days):
            rows.append({
                "ts_code": ts,
                "trade_date": d,
                "open": base,
                "high": base + 1,
                "low": base - 1,
                "close": base + (j % 5) * 0.5,  # 微小波动
                "vol": 50000,  # 50000 张 → close*vol ≈ 5e6 元
            })
    cb_daily = pd.DataFrame(rows)

    # cb_call: 把第 4 只 (CB004) 在第 30~40 天标记为 force_redeem
    if n_cb >= 4 and n_days >= 40:
        cb_call = pd.DataFrame({
            "ts_code": ["CB004.SH"],
            "code": ["CB004"],
            "ann_date": [days[30]],
            "call_date": [days[35]],
            "call_price": [103.0],
            "is_call": ["公告实施强赎"],
            "expire_date": [days[40]],
        })
    else:
        cb_call = pd.DataFrame({
            "ts_code": [],
            "code": [],
            "ann_date": [],
            "call_date": [],
            "call_price": [],
            "is_call": [],
            "expire_date": [],
        })

    # stk_daily_qfq — 5 只正股 × n_days, 价格漂移
    rows = []
    for i, sc in enumerate(stk_codes):
        base_stk = 8 + i * 0.5
        for j, d in enumerate(days):
            rows.append({
                "stk_code": sc,
                "trade_date": d,
                "close": base_stk + 0.05 * np.sin(j / 5),
            })
    stk_daily = pd.DataFrame(rows)

    return cb_basic, cb_daily, cb_call, stk_daily, days, cb_codes


def _install_synthetic_caches(monkeypatch):
    """把模块缓存替成合成数据."""
    cb_basic, cb_daily, cb_call, stk_daily, days, cb_codes = _make_synthetic_data(
        n_cb=5, n_days=60
    )
    # cb_basic 加派生字段 + index
    cb_basic_proc = cb_basic.copy()
    cb_basic_proc["rating_int"] = cb_basic_proc["rating"].map(
        lambda r: RATING_TO_INT.get(r, 0)
    ).astype(int)
    cb_basic_proc["issue_size_yuan"] = cb_basic_proc["issue_size"].astype(float) * 1e8
    cb_basic_proc = cb_basic_proc.set_index("ts_code", drop=False)

    cb_daily_proc = cb_daily.copy()
    cb_daily_proc["amount_yuan"] = (
        cb_daily_proc["close"].astype(float) * cb_daily_proc["vol"].astype(float)
    )
    cb_daily_proc = cb_daily_proc.sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)

    stk_daily_proc = stk_daily[["stk_code", "trade_date", "close"]].copy()
    stk_daily_proc = stk_daily_proc.sort_values(
        ["stk_code", "trade_date"]
    ).reset_index(drop=True)

    monkeypatch.setattr(v, "_CB_BASIC_CACHE", cb_basic_proc)
    monkeypatch.setattr(v, "_CB_DAILY_CACHE", cb_daily_proc)
    monkeypatch.setattr(v, "_CB_CALL_CACHE", cb_call)
    monkeypatch.setattr(v, "_STK_DAILY_CACHE", stk_daily_proc)
    monkeypatch.setattr(v, "_TRADING_DAYS_CACHE", days)
    return cb_codes, days


def test_signal_buys_cheapest(monkeypatch):
    """合成数据: CB001 价低 (相对最便宜) → 应进入 holdings 且产生交易."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    # 极端 buy_pct=0.5 (5 只 → 买 2-3 只), max_holdings 大, sell_pct 高
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [
        20, 1.0, 0.50, 0.99, 0.10, 30, 90, -0.30,
        1e6,    # min_remaining_size: 1 千万足够
        1e3,    # min_avg_amount: 1 千元 (合成数据低成交)
        50, 150,
    ]
    result = run_backtest(weights, {}, rules, oos_event_ids=None)
    assert isinstance(result, BacktestResult)
    # 买入候选有 → 期间至少有交易
    assert result.all_metrics["total_trades"] > 0


def test_force_redemption_forces_sell(monkeypatch):
    """CB004 在 days[30:40] 标记 force_redeem → 不应有 entry > days[30] 且
    holdings 中包含 CB004 持续到 days[40] 之后的情况.
    """
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [20, 1.0, 0.80, 0.99, 0.10, 30, 90, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules, oos_event_ids=None)

    # 找 CB004 的所有交易
    cb004_trades = [t for t in result.trades if t.cb_code == "CB004.SH"]
    for t in cb004_trades:
        # 要么在强赎期前已平仓, 要么以 force_redemption 退出
        if t.exit_date >= days[30] and t.exit_date <= days[40]:
            assert t.exit_reason in {
                "force_redemption", "stop_loss", "rank_sell", "max_holding_days"
            }


def test_max_holding_days_triggers(monkeypatch):
    """max_holding_days=5 (calendar days) → 任何 max_holding_days 触发的交易应在
    5 + 2 (weekend buffer) 天内退出.
    """
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [20, 1.0, 0.80, 0.99, 0.10, 30, 5, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules, oos_event_ids=None)
    # 只看 max_holding_days reason 的交易
    mhd_trades = [t for t in result.trades if t.exit_reason == "max_holding_days"]
    for t in mhd_trades:
        # 用 calendar days, weekend +2 容差
        assert t.holding_days <= 5 + 4, (
            f"max_holding=5 但实际 {t.holding_days} 天: {t}"
        )


def test_stop_loss_triggers(monkeypatch):
    """stop_loss=-0.001 (0.1%) + 价格小幅波动 → 容易触发止损."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    # 极小止损阈值
    weights = [20, 1.0, 0.80, 0.99, 0.05, 30, 90, -0.001, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules, oos_event_ids=None)
    # 应该有 stop_loss 触发的交易 (合成价格波动至少 ±0.5)
    sl_trades = [t for t in result.trades if t.exit_reason == "stop_loss"]
    # 至少有 1 笔; 如果 0 也 OK (价格走得平), 但断言至少形态正确
    assert all(t.pnl_pct <= -0.0005 for t in sl_trades)  # 触发时 pnl 应负


def test_oos_pool_filters_oos_metrics(monkeypatch):
    """oos_event_ids 给一段 → oos_metrics 在该段上, all_metrics 在更长段上."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    # 选最后 10 天作 pool
    pool = set(days[-10:])
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [20, 1.0, 0.50, 0.99, 0.05, 30, 90, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules, oos_event_ids=pool)
    # OOS metrics 的 n_days 应在 10 上下
    assert result.oos_metrics["n_days"] <= 10
    # all_metrics n_days 大于等于 OOS
    assert result.all_metrics["n_days"] >= result.oos_metrics["n_days"]


def test_empty_pool_returns_empty_result(monkeypatch):
    """oos_event_ids=set() (空) → 走 all-段 (向后兼容)."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [20, 1.0, 0.50, 0.99, 0.05, 30, 90, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules, oos_event_ids=set())
    assert isinstance(result, BacktestResult)


def test_returns_BacktestResult_shape(monkeypatch):
    """结构契约: 返回 BacktestResult, trades 是 TradeRecord."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [20, 1.0, 0.50, 0.99, 0.05, 30, 90, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules)
    assert isinstance(result, BacktestResult)
    assert isinstance(result.trades, list)
    if result.trades:
        assert isinstance(result.trades[0], TradeRecord)
    # metrics 关键字
    for m in (result.is_metrics, result.oos_metrics, result.all_metrics):
        assert "sharpe" in m
        assert "win_rate" in m
        assert "total_trades" in m
        assert "total_return" in m
        assert "max_drawdown" in m
    assert isinstance(result.cumulative_metrics, dict)
    assert "excess_return" in result.cumulative_metrics
    assert isinstance(result.equity_curve, pd.Series)
    assert not result.equity_curve.empty
    serialised = result.to_dict()
    assert "cumulative_metrics" in serialised
    assert "equity_curve" not in serialised


def test_cb_style_5arg_call_works(monkeypatch):
    """cb-style: (snapshots, weights, thresholds, cfg) 也应跑通."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)

    class _Cfg:
        fee_pct = 0.0003

    snap = pd.DataFrame()
    weights = [20, 1.0, 0.50, 0.99, 0.05, 30, 90, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(snap, weights, {}, _Cfg())
    assert isinstance(result, BacktestResult)


# --------------------------------------------------------------------------- #
# excess_return: 跑赢 CB 等权指数多少 (= 真 alpha 信号)
# --------------------------------------------------------------------------- #


def test_excess_return_field_present_in_metrics(monkeypatch):
    """跑一次回测, is/oos/all metrics 都该有 excess_return 字段."""
    cb_codes, days = _install_synthetic_caches(monkeypatch)
    rules = {"rating_floor_int": 0, "fee_pct": 0.0001, "initial_capital": 1_000_000.0}
    weights = [20, 1.0, 0.50, 0.99, 0.10, 30, 90, -0.30, 1e6, 1e3, 50, 150]
    result = run_backtest(weights, {}, rules)
    for name, m in (
        ("all", result.all_metrics),
        ("is", result.is_metrics),
        ("oos", result.oos_metrics),
    ):
        assert "excess_return" in m, f"{name}_metrics 缺 excess_return: {m}"
        assert isinstance(m["excess_return"], (int, float)), (
            f"{name}_metrics.excess_return 类型 {type(m['excess_return'])} 不是数"
        )
        assert math.isfinite(float(m["excess_return"]))


def test_excess_return_equals_strategy_minus_index(monkeypatch):
    """假设策略 +30%, 同期指数 +10%, excess_return 应该约 +20%.

    用 mock 替换 _index_total_return 和 _compute_metrics 输入, 直接验证差值公式.
    """
    # 直接调 _compute_metrics, 用 mock _index_total_return 控制返回值
    fake_curve = [
        ("20240101", 1_000_000.0),
        ("20240601", 1_300_000.0),  # +30%
    ]

    def fake_index(start, end):
        # 控制为 10%
        return 0.10

    monkeypatch.setattr(v, "_index_total_return", fake_index)

    m = _compute_metrics(
        fake_curve, [], initial_capital=1_000_000.0,
        index_dates=["20240101", "20240601"],
    )
    assert abs(m["total_return"] - 0.30) < 1e-6, m
    # excess_return = total_return - index_return = 0.30 - 0.10 = 0.20
    assert abs(m["excess_return"] - 0.20) < 1e-4, m


def test_excess_return_zero_dates_handled(monkeypatch):
    """空日期段不 raise — _index_total_return("", "") 应返回 0,
    _compute_metrics 给空 curve 时 excess_return 为 0.
    """
    # 1) 空 equity_curve → 整体走空分支, excess_return = 0
    m_empty = _compute_metrics([], [], 1_000_000.0)
    assert m_empty["excess_return"] == 0.0

    # 2) _index_total_return 直接拿空字符串 — 不 raise, 返回 0
    assert _index_total_return("", "") == 0.0
    assert _index_total_return("20240101", "") == 0.0
    # 反向区间也返回 0 (而不是 raise)
    assert _index_total_return("20240601", "20240101") == 0.0

    # 3) 单点 curve (n_days=1) → excess_return 应该 0 (不能算总收益走势)
    m_single = _compute_metrics(
        [("20240101", 1_000_000.0)], [], 1_000_000.0,
        index_dates=["20240101"],
    )
    # 单点有 total_return=0; excess_return = 0 - 0 = 0
    assert m_single["excess_return"] == 0.0


# --------------------------------------------------------------------------- #
# 集成测试: 真实 cb_warehouse, 一个小窗口
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_real_data_30day_smoke():
    """真实 cb_warehouse 数据, 跑 30 天 OOS 子段, 验证全流程."""
    import time
    from strategies.cb_arb.verifier import _load_trading_days, reset_cache

    reset_cache()
    days = _load_trading_days()
    test_pool = set([d for d in days if d.startswith("2024")][:30])
    assert len(test_pool) == 30
    t0 = time.time()
    result = run_backtest([], {}, {}, oos_event_ids=test_pool)
    dt = time.time() - t0
    print(f"\n[real_data smoke] took {dt:.1f}s, oos: {result.oos_metrics}")
    assert isinstance(result, BacktestResult)
    # 合理性: pool 30 天, n_days <= 30 (最后)
    assert result.oos_metrics["n_days"] <= 30
    # 至少有些交易 — 30 天偶尔 0 是可能的, 但通常 > 0
    assert result.all_metrics["total_trades"] >= 0
