"""单元测试: call_condition.

覆盖:
1. 权威参数常量(30/1.30/15)锁定, 改动需要显式改测试
2. trailing_trigger_count 基本手算
3. 历史不足 window 天返回 NaN(不是偏低的部分计数)
4. 边界: 恰好 14/15/16 天达标, eligible 应在 15 处翻转
5. NaN 视为当天不达标, 不传染进相邻窗口
6. TrailingTriggerCounter 逐步维护 与 向量化函数 结果一致(交叉校验)
7. TrailingTriggerCounter.clone 状态独立
"""

import math

import numpy as np

from strategies.cb_arb.call_condition import (
    DEFAULT_REQUIRED_DAYS,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
    TrailingTriggerCounter,
    is_call_eligible,
    trailing_trigger_count,
)


# ----------------------------------------------------------------------
# 1. 权威参数常量
# ----------------------------------------------------------------------

def test_default_constants_match_market_convention():
    """默认参数是唯一权威值: 30 个交易日 / 130% / 15 天。改这三个数要显式改这里。"""
    assert DEFAULT_WINDOW == 30
    assert DEFAULT_THRESHOLD == 1.30
    assert DEFAULT_REQUIRED_DAYS == 15


# ----------------------------------------------------------------------
# 2. 基本手算
# ----------------------------------------------------------------------

def test_trailing_trigger_count_basic_handcalc():
    """window=3, threshold=1.0: [0.9, 1.1, 1.2, 0.5, 1.3] -> 最后三个窗口手算."""
    ratio = np.array([0.9, 1.1, 1.2, 0.5, 1.3])
    counts = trailing_trigger_count(ratio, window=3, threshold=1.0)
    # index 0,1: 历史不足 3 天 -> NaN
    assert math.isnan(counts[0])
    assert math.isnan(counts[1])
    # index 2: 窗口 [0.9,1.1,1.2] -> 达标 2 天 (1.1, 1.2)
    assert counts[2] == 2
    # index 3: 窗口 [1.1,1.2,0.5] -> 达标 2 天
    assert counts[3] == 2
    # index 4: 窗口 [1.2,0.5,1.3] -> 达标 2 天
    assert counts[4] == 2


# ----------------------------------------------------------------------
# 3. 历史不足返回 NaN, 不是偏低计数
# ----------------------------------------------------------------------

def test_insufficient_history_returns_nan_not_partial_count():
    """全部达标但长度 < window: 每个位置都该是 NaN, 不能返回"目前看到几天算几天"."""
    ratio = np.full(10, 2.0)  # 全部远高于阈值, 但只有 10 天历史
    counts = trailing_trigger_count(ratio, window=30, threshold=1.30)
    assert np.all(np.isnan(counts))

    eligible = is_call_eligible(ratio, window=30, threshold=1.30, required=15)
    # 历史不足时判定为不满足, 不是"未知"
    assert not np.any(eligible)


# ----------------------------------------------------------------------
# 4. 边界: 14/15/16 天
# ----------------------------------------------------------------------

def _series_with_exact_hits(n_hit: int, window: int = 30) -> np.ndarray:
    """构造一段恰好 window 天、其中 n_hit 天达标的序列(只有最后一个位置可评估)."""
    arr = np.full(window, 1.0)  # 不达标 (< 1.30)
    arr[:n_hit] = 1.35  # 达标
    return arr


def test_call_condition_boundary_flips_exactly_at_required_days():
    """恰好 14/15/16 天达标: eligible 应在 required=15 处翻转, 不早不晚."""
    for n_hit, expect_eligible in [(14, False), (15, True), (16, True)]:
        ratio = _series_with_exact_hits(n_hit)
        counts = trailing_trigger_count(ratio)
        eligible = is_call_eligible(ratio)
        assert counts[-1] == n_hit, f"n_hit={n_hit}: count={counts[-1]}"
        assert eligible[-1] == expect_eligible, (
            f"n_hit={n_hit}: expect eligible={expect_eligible}, got {eligible[-1]}"
        )


# ----------------------------------------------------------------------
# 5. NaN 视为当天不达标, 不传染
# ----------------------------------------------------------------------

def test_nan_ratio_treated_as_not_hit_and_does_not_propagate():
    """序列中间插入 NaN(缺数据日): 该天记 0, 前后窗口正常计数, 不会整段变 NaN."""
    arr = np.full(35, 1.35)  # 全部达标
    arr[20] = np.nan  # 第 21 天缺数据
    counts = trailing_trigger_count(arr, window=30, threshold=1.30)
    # index 34 的窗口是 [5..34], 含那个 NaN, 应为 29 (30 天里 1 天不算)
    assert counts[34] == 29
    # 不应该整段变 NaN
    assert not math.isnan(counts[34])


# ----------------------------------------------------------------------
# 6. 逐步维护 vs 向量化: 交叉校验
# ----------------------------------------------------------------------

def test_incremental_counter_matches_vectorized_reference():
    """TrailingTriggerCounter 逐天 push, 结果应与 trailing_trigger_count 向量化版本逐点一致."""
    rng = np.random.default_rng(7)
    ratio = rng.uniform(0.7, 1.6, size=80)

    vectorized = trailing_trigger_count(ratio, window=DEFAULT_WINDOW, threshold=DEFAULT_THRESHOLD)

    counter = TrailingTriggerCounter(window=DEFAULT_WINDOW, threshold=DEFAULT_THRESHOLD)
    incremental = np.array([counter.push(r) for r in ratio], dtype=float)

    for i in range(len(ratio)):
        if i < DEFAULT_WINDOW - 1:
            # 向量化版本这里是 NaN(历史不足); 逐步版本仍然给出"目前累计的计数",
            # 两者不可比, 只比较 has_full_window 之后的区间。
            continue
        assert incremental[i] == vectorized[i], (
            f"index {i}: incremental={incremental[i]} vectorized={vectorized[i]}"
        )


def test_incremental_counter_is_eligible_matches_vectorized():
    rng = np.random.default_rng(11)
    ratio = rng.uniform(0.7, 1.6, size=50)

    eligible_vec = is_call_eligible(ratio)

    counter = TrailingTriggerCounter()
    eligible_inc = []
    for r in ratio:
        counter.push(r)
        eligible_inc.append(counter.is_eligible())

    assert list(eligible_inc) == list(eligible_vec)


# ----------------------------------------------------------------------
# 7. clone 状态独立
# ----------------------------------------------------------------------

def test_clone_is_independent_of_original():
    counter = TrailingTriggerCounter(window=5, threshold=1.0)
    for r in [1.2, 1.2, 0.5, 1.2, 1.2]:
        counter.push(r)
    assert counter.count == 4

    branch_a = counter.clone()
    branch_b = counter.clone()

    branch_a.push(1.5)  # 挤掉最早的 1.2(命中), 加入一个命中 -> 计数不变
    branch_b.push(0.1)  # 挤掉最早的 1.2(命中), 加入一个不命中 -> 计数减 1

    assert counter.count == 4, "原对象不应被 clone 出去的分支影响"
    assert branch_a.count == 4
    assert branch_b.count == 3
