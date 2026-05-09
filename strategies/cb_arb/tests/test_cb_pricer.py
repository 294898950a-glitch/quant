"""单元测试: cb_pricer.

至少 8 个测试覆盖:
1. BS 公式 vs 已知值
2. 债底手算
3. 距到期 < 30 天走 intrinsic 路径
4. 强赎触发锁顶
5. 回售期债底防跌穿
6. 转股价下修后理论价变化
7. 极端 vol (0% / 200%) 不崩
8. 缺数据 (stock_price=NaN) 返回 invalid
"""

import math

import numpy as np
import pytest

from strategies.cb_arb.cb_pricer import (
    CBSpec,
    CBValuation,
    DEFAULT_CREDIT_SPREAD_BP,
    NEAR_MATURITY_DAYS,
    REDEMPTION_LOCKED_VALUE,
    bond_floor_pv,
    bs_call,
    price_cb,
    realized_vol,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

def make_spec(
    ts_code="110001.SH",
    face_value=100.0,
    conv_price=10.0,
    list_date="20200101",
    maturity_date="20260101",
    coupon_rate=0.005,
    rating="AA",
):
    return CBSpec(
        ts_code=ts_code,
        face_value=face_value,
        conv_price=conv_price,
        list_date=list_date,
        maturity_date=maturity_date,
        coupon_rate=coupon_rate,
        rating=rating,
    )


# ----------------------------------------------------------------------
# 1. BS 公式 vs 已知值
# ----------------------------------------------------------------------

def test_bs_call_known_value():
    """BS call: S=100, K=100, T=1, sigma=0.20, r=0.05 -> ~10.45.

    可在线 BS 计算器对照 (e.g. https://www.optionsprofitcalculator.com/).
    """
    price = bs_call(S=100.0, K=100.0, T=1.0, sigma=0.20, r=0.05)
    assert 10.30 < price < 10.60, f"BS call mismatch: {price:.4f}"


def test_bs_call_zero_T():
    """T=0: 收敛到 max(S-K, 0)."""
    assert bs_call(S=110, K=100, T=0, sigma=0.30, r=0.05) == 10.0
    assert bs_call(S=90, K=100, T=0, sigma=0.30, r=0.05) == 0.0


def test_bs_call_zero_sigma():
    """sigma=0 时退化到 max(S - K*exp(-rT), 0)."""
    val = bs_call(S=100, K=100, T=1, sigma=0.0, r=0.05)
    expected = max(100 - 100 * math.exp(-0.05), 0)
    assert abs(val - expected) < 1e-6


# ----------------------------------------------------------------------
# 2. 债底手算
# ----------------------------------------------------------------------

def test_bond_floor_two_year_handcalc():
    """2 年到期, 票面 1%, 折现 2.5% (无利差).

    现金流: 第 1 年末 1 元利息, 第 2 年末 1 元利息 + 100 元面值.
    PV = 1/1.025 + 101/1.025^2.
    """
    pv = bond_floor_pv(
        face_value=100.0,
        coupon_rate=0.01,
        years_to_maturity=2.0,
        discount_rate=0.025,
    )
    expected = 1 / 1.025 + 101 / (1.025 ** 2)
    assert abs(pv - expected) < 0.01, f"bond_floor mismatch: {pv:.4f} vs {expected:.4f}"


def test_bond_floor_zero_time():
    """到期日: 拿面值 + 一次利息."""
    pv = bond_floor_pv(
        face_value=100.0,
        coupon_rate=0.02,
        years_to_maturity=0.0,
        discount_rate=0.025,
    )
    assert abs(pv - 102.0) < 1e-6


# ----------------------------------------------------------------------
# 3. 距到期 < 30 天走 intrinsic 路径
# ----------------------------------------------------------------------

def test_near_maturity_uses_intrinsic():
    """距到期 < 30 天: method='intrinsic', 用 max(intrinsic, bond_floor)."""
    spec = make_spec(
        list_date="20200101",
        maturity_date="20260601",  # 距估值日 25 天
        conv_price=10.0,
        rating="AA",
    )
    v = price_cb(
        spec=spec,
        valuation_date="20260507",  # 距 20260601 = 25 天
        stock_price=12.0,
        stock_vol=0.30,
    )
    assert v.method == "intrinsic"
    # intrinsic = (100/10) * 12 = 120
    assert abs(v.intrinsic - 120.0) < 1e-6
    # 最终 = max(intrinsic, bf), 因为 stock 显著高于转股价, intrinsic 应主导
    assert v.theoretical >= v.intrinsic - 1e-6
    assert "near_maturity_lt_30d" in v.notes


def test_near_maturity_under_par_uses_bond_floor():
    """距到期 < 30 天且正股低: theoretical = bond_floor (>= face)."""
    spec = make_spec(
        list_date="20200101",
        maturity_date="20260601",
        conv_price=10.0,
        rating="AA",
        coupon_rate=0.02,
    )
    v = price_cb(
        spec=spec,
        valuation_date="20260507",
        stock_price=5.0,  # intrinsic = 50
        stock_vol=0.30,
    )
    assert v.method == "intrinsic"
    # intrinsic = 50, bond_floor 应该接近 100+ (回售期内防跌穿到面值)
    assert v.theoretical >= 100.0
    assert v.theoretical >= v.intrinsic


# ----------------------------------------------------------------------
# 4. 强赎触发锁顶
# ----------------------------------------------------------------------

def test_force_redemption_locks_theo():
    """is_force_redeemed=True 时, theoretical=REDEMPTION_LOCKED_VALUE."""
    spec = make_spec()
    v = price_cb(
        spec=spec,
        valuation_date="20240601",
        stock_price=20.0,  # 即使正股很高
        stock_vol=0.40,
        is_force_redeemed=True,
    )
    assert v.method == "redemption_locked"
    assert abs(v.theoretical - REDEMPTION_LOCKED_VALUE) < 1e-6
    assert v.option_value == 0.0


# ----------------------------------------------------------------------
# 5. 回售期债底防跌穿
# ----------------------------------------------------------------------

def test_putable_period_bond_floor_at_least_face():
    """最后 2 年内, 债底应 >= 面值."""
    # 票面利率极低 + 信用利差大 -> 自然债底会 < 面值
    spec = make_spec(
        list_date="20200101",
        maturity_date="20260601",  # 估值日距到期 1 年, 在回售期内
        conv_price=20.0,           # 行权价高 (期权 itm 浅) 让 BF 主导
        rating="A",
        coupon_rate=0.001,
    )
    v = price_cb(
        spec=spec,
        valuation_date="20250601",
        stock_price=10.0,
        stock_vol=0.30,
        risk_free_rate=0.025,
    )
    # 在回售期内, 债底应不小于面值 (即使裸算 < 100)
    assert v.bond_floor >= spec.face_value - 1e-6
    assert "putable_period_floor" in v.notes


# ----------------------------------------------------------------------
# 6. 转股价下修后理论价变化
# ----------------------------------------------------------------------

def test_conv_price_downward_revision_changes_theo():
    """下修转股价后, 期权价值更高, 总理论价应上升."""
    base_kwargs = dict(
        valuation_date="20240601",
        stock_price=10.0,
        stock_vol=0.30,
        risk_free_rate=0.025,
    )
    spec_v0 = make_spec(conv_price=15.0)  # 原转股价 15 (otm)
    spec_v1 = make_spec(conv_price=10.0)  # 下修到 10 (atm)

    v0 = price_cb(spec_v0, **base_kwargs)
    v1 = price_cb(spec_v1, **base_kwargs)

    assert v1.option_value > v0.option_value, (
        f"期权值: K=10 ({v1.option_value:.4f}) "
        f"应 > K=15 ({v0.option_value:.4f})"
    )
    assert v1.theoretical > v0.theoretical


# ----------------------------------------------------------------------
# 7. 极端 vol (0% / 200%) 不崩
# ----------------------------------------------------------------------

def test_extreme_vol_does_not_crash():
    """vol=0 和 vol=2.0 (200%) 都不应崩, 返回有限实数."""
    spec = make_spec()
    base_kwargs = dict(
        spec=spec,
        valuation_date="20240601",
        stock_price=10.0,
        risk_free_rate=0.025,
    )
    v_zero = price_cb(stock_vol=0.0, **base_kwargs)
    v_high = price_cb(stock_vol=2.0, **base_kwargs)

    assert math.isfinite(v_zero.theoretical), "vol=0 崩"
    assert math.isfinite(v_high.theoretical), "vol=2.0 崩"
    # vol 越高, 期权值越高
    assert v_high.option_value > v_zero.option_value


# ----------------------------------------------------------------------
# 8. 缺数据返回 invalid
# ----------------------------------------------------------------------

def test_missing_stock_price_returns_invalid():
    """stock_price=NaN 时返回 method='invalid'."""
    spec = make_spec()
    v = price_cb(
        spec=spec,
        valuation_date="20240601",
        stock_price=float("nan"),
        stock_vol=0.30,
    )
    assert v.method == "invalid"
    assert math.isnan(v.theoretical)


def test_negative_stock_price_returns_invalid():
    """stock_price<=0 也是 invalid."""
    spec = make_spec()
    v = price_cb(
        spec=spec,
        valuation_date="20240601",
        stock_price=-5.0,
        stock_vol=0.30,
    )
    assert v.method == "invalid"


# ----------------------------------------------------------------------
# 9. 综合: 典型 BS 路径
# ----------------------------------------------------------------------

def test_typical_bs_path():
    """中段健康 CB: 走 BS, 理论价 = 债底 + 期权."""
    spec = make_spec(
        list_date="20220101",
        maturity_date="20280101",
        conv_price=10.0,
        rating="AA+",
        coupon_rate=0.01,
    )
    v = price_cb(
        spec=spec,
        valuation_date="20240601",
        stock_price=10.0,    # atm
        stock_vol=0.30,
        risk_free_rate=0.025,
    )
    assert v.method == "BS"
    assert v.theoretical == pytest.approx(v.bond_floor + v.option_value, abs=1e-6)
    assert v.option_value > 0
    assert 80 < v.bond_floor < 100  # AA+ 折现, 6 年后回到面值, PV < 100


# ----------------------------------------------------------------------
# 10. 实现波动率小工具
# ----------------------------------------------------------------------

def test_realized_vol_basic():
    """1% 日波动 (恒定) -> 年化 ~16%."""
    np.random.seed(42)
    n = 252
    log_returns = np.random.normal(0, 0.01, n)
    prices = 100 * np.exp(np.cumsum(log_returns))
    vol = realized_vol(prices)
    # 0.01 * sqrt(252) ~= 0.1587
    assert 0.12 < vol < 0.20, f"realized_vol={vol:.4f}"


def test_realized_vol_handles_short_series():
    """长度 < 2 返回 NaN."""
    assert math.isnan(realized_vol(np.array([100.0])))
    assert math.isnan(realized_vol(np.array([])))
