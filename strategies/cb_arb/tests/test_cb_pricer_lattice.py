"""单元测试: cb_pricer_lattice(合成数据, 不碰真实仓库, 本机可跑).

覆盖:
1. corner case 与 price_cb 完全一致(缺数据/已强赎/距到期<30天/回售期防跌穿)
2. 远离触发条件时, 应大致收敛到 price_cb 的旧算法(已知有小的、可解释的偏差)
3. 已经很接近满足强赎条件时, 理论价应明显低于 price_cb, 且触发概率应接近 1
4. 蒙特卡洛的随机性: 固定 seed 结果可复现; 路径数变化不应改变数量级
"""

import math

import numpy as np
import pytest

from strategies.cb_arb.cb_pricer import price_cb
from strategies.cb_arb.cb_pricer_lattice import price_cb_lattice
from strategies.cb_arb.tests.test_cb_pricer import make_spec


# ----------------------------------------------------------------------
# 1. corner case 一致性
# ----------------------------------------------------------------------

def test_missing_stock_price_returns_invalid():
    spec = make_spec()
    v = price_cb_lattice(
        spec=spec,
        valuation_date="20240601",
        stock_price=float("nan"),
        stock_vol=0.30,
        moneyness_history=np.array([]),
    )
    assert v.method == "invalid"
    assert math.isnan(v.theoretical)


def test_force_redemption_locks_theo():
    spec = make_spec()
    v = price_cb_lattice(
        spec=spec,
        valuation_date="20240601",
        stock_price=20.0,
        stock_vol=0.40,
        moneyness_history=np.array([]),
        is_force_redeemed=True,
    )
    assert v.method == "redemption_locked"
    assert v.theoretical == 103.0


def test_near_maturity_matches_price_cb_exactly():
    """距到期 < 30 天: 两边都走 intrinsic 路径, 应完全一致(不涉及 MC)."""
    spec = make_spec(maturity_date="20260601", conv_price=10.0)
    kwargs = dict(
        valuation_date="20260507",
        stock_price=12.0,
        stock_vol=0.30,
    )
    v_old = price_cb(spec=spec, **kwargs)
    v_new = price_cb_lattice(spec=spec, moneyness_history=np.array([1.0] * 40), **kwargs)
    assert v_new.method == "intrinsic"
    assert v_new.theoretical == pytest.approx(v_old.theoretical, abs=1e-6)


def test_putable_period_bond_floor_at_least_face():
    spec = make_spec(
        maturity_date="20260601",
        conv_price=20.0,
        rating="A",
        coupon_rate=0.001,
    )
    v = price_cb_lattice(
        spec=spec,
        valuation_date="20250601",
        stock_price=10.0,
        stock_vol=0.30,
        moneyness_history=np.full(40, 0.5),
        risk_free_rate=0.025,
        seed=1,
    )
    assert v.bond_floor >= spec.face_value - 1e-6
    assert "putable_period_floor" in v.notes


# ----------------------------------------------------------------------
# 2. 远离触发条件: 大致收敛到旧算法
# ----------------------------------------------------------------------

def test_far_from_trigger_converges_to_price_cb():
    """长期 ATM, moneyness 历史平稳在 1.0 附近(远低于 1.3): 两边应接近.

    已知会有小偏差(见模块 docstring "已知局限"): repayment_terminal 比
    face_value 高一点点, 蒙特卡洛本身有统计误差。这里给一个宽松容差,
    报告实际差值而不是只判断通过/不通过。
    """
    spec = make_spec(
        list_date="20220101",
        maturity_date="20280101",
        conv_price=10.0,
        rating="AA+",
        coupon_rate=0.01,
    )
    kwargs = dict(
        valuation_date="20240601",
        stock_price=10.0,
        stock_vol=0.20,
        risk_free_rate=0.025,
    )
    v_old = price_cb(spec=spec, **kwargs)
    v_new = price_cb_lattice(
        spec=spec,
        moneyness_history=np.full(40, 1.0),
        n_paths=8000,
        seed=42,
        **kwargs,
    )

    diff_pct = abs(v_new.theoretical - v_old.theoretical) / v_old.theoretical
    print(
        f"\n[far_from_trigger] old={v_old.theoretical:.4f} new={v_new.theoretical:.4f} "
        f"diff_pct={diff_pct:.4%} notes={v_new.notes}"
    )
    assert diff_pct < 0.05, f"远离触发时偏差过大: {diff_pct:.4%}"


# ----------------------------------------------------------------------
# 3. 已接近触发: 理论价应明显更低
# ----------------------------------------------------------------------

def test_near_trigger_prices_lower_than_price_cb():
    """历史已经连续 29 天满足 moneyness>=1.3(只差 1 天不到 30 天窗口, 几乎
    肯定立刻触发): 新模型应给出明显更低的理论价, 且触发概率应接近 1。

    判据不是"低多少个百分点"这种拍脑袋的数(第一版测试就是这么写的, 结果
    掩盖了一个真 bug, 见 cb_pricer_lattice.py 模块 docstring "实现过程中的
    一次修正")。这里改成算"理论价比 intrinsic(转股价值) 多出来的部分",
    即模型还剩多少"时间价值"——旧模型按名义到期日(还有好几年)算, 这部分
    时间价值理应很大; 新模型知道明天大概率就被叫停, 这部分时间价值理应
    几乎归零。判据就是这个"剩余时间价值"新模型应远小于旧模型。
    """
    spec = make_spec(
        list_date="20220101",
        maturity_date="20280101",  # 名义还有好几年
        conv_price=10.0,
        rating="AA+",
        coupon_rate=0.01,
    )
    kwargs = dict(
        valuation_date="20240601",
        stock_price=14.0,  # moneyness = 1.4, 已经在触发区间
        stock_vol=0.20,
        risk_free_rate=0.025,
    )
    # 过去 29 天全部满足 >=1.3: 只差 1 天不到 30 天窗口, 模拟出的下一天几乎
    # 必然满足"过去 30 天里 >=15 天达标", 几乎肯定立刻触发。
    history = np.full(29, 1.35)

    v_old = price_cb(spec=spec, **kwargs)
    v_new = price_cb_lattice(
        spec=spec,
        moneyness_history=history,
        n_paths=8000,
        seed=42,
        **kwargs,
    )

    excess_old = v_old.theoretical - v_old.intrinsic
    excess_new = v_new.theoretical - v_new.intrinsic
    print(
        f"\n[near_trigger] old={v_old.theoretical:.4f}(excess={excess_old:.4f}) "
        f"new={v_new.theoretical:.4f}(excess={excess_new:.4f}) notes={v_new.notes}"
    )
    assert v_new.theoretical < v_old.theoretical
    assert excess_new < excess_old * 0.5, (
        f"几乎立刻被叫停时, 剩余时间价值应大幅收窄: "
        f"old_excess={excess_old:.4f} new_excess={excess_new:.4f}"
    )
    # 触发概率应接近 1(历史已经 29/30 天满足, 只要第一天不跌破就立刻触发)
    frac = float(v_new.notes.split("mc_frac_triggered=")[1])
    assert frac > 0.9, f"应该几乎立刻触发, 实际触发概率={frac:.4f}"


# ----------------------------------------------------------------------
# 4. 蒙特卡洛可复现性
# ----------------------------------------------------------------------

def test_seed_reproducibility():
    spec = make_spec(maturity_date="20280101", conv_price=10.0, rating="AA")
    kwargs = dict(
        spec=spec,
        valuation_date="20240601",
        stock_price=10.0,
        stock_vol=0.25,
        moneyness_history=np.full(40, 1.0),
        n_paths=2000,
        seed=123,
    )
    v1 = price_cb_lattice(**kwargs)
    v2 = price_cb_lattice(**kwargs)
    assert v1.theoretical == v2.theoretical


def test_more_paths_does_not_change_order_of_magnitude():
    spec = make_spec(maturity_date="20280101", conv_price=10.0, rating="AA")
    kwargs = dict(
        spec=spec,
        valuation_date="20240601",
        stock_price=10.0,
        stock_vol=0.25,
        moneyness_history=np.full(40, 1.0),
        seed=7,
    )
    v_small = price_cb_lattice(n_paths=500, **kwargs)
    v_big = price_cb_lattice(n_paths=8000, **kwargs)
    assert abs(v_small.theoretical - v_big.theoretical) / v_big.theoretical < 0.1
