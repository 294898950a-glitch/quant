"""强赎条件天数统计 —— 唯一权威实现.

判断"一只转债现在是否满足强赎条件"这件事, 之前在三个地方各自算一遍
(`verifier.py` 读不可靠的 `cb_call.parquet`; `study/diagnose_implied_vs_realized_vol.py`
临时写了一份内联逻辑; 格子定价器差点又简化出第三份)。本模块是唯一权威实现,
其余地方改成调用这里, 不再各自维护一份。

规则(A 股转债市场通行条款, 非逐券核实): 过去 ``window`` 个交易日里,
有 ``required`` 天以上 正股价/转股价 >= ``threshold``, 视为满足强赎条件。
默认 30/15/1.30, 与 `study/diagnose_implied_vs_realized_vol.py` 里
`call_condition_met` 的口径一致(该脚本后续应改为调用本模块, 见
`docs/2026-08-28-cb-embedded-call-lattice-pricer-spec.txt` 第 6 节)。

本模块不认识"转债"是什么 —— 只吃一段 S/K 比值(moneyness), 不依赖
`cb_pricer.py` / `verifier.py` 里任何 CB 专属的类型或数据加载逻辑,
两边都可以单向调用它, 它不反向依赖任何一边。

两种用法:
1. 历史/截至今天(向量化) —— 喂真实历史 moneyness 序列, 见 ``trailing_trigger_count``
   / ``is_call_eligible``。
2. 格子前向模拟(逐步维护) —— 见 ``TrailingTriggerCounter``, 每条模拟路径各自
   维护一份, 每步 O(1) 更新, 不用每步重算整段历史。

已知边界: ``TrailingTriggerCounter`` 对单条路径是精确的(它记得完整的过去
``window`` 天), 但如果格子定价器选择"状态增强树"(把计数当成节点上的离散状态
共享给不同路径), 单纯一个 0-``window`` 的计数不足以精确知道"即将滑出窗口的
那一天是不是命中", 状态转移会退化成近似。这是 `cb_pricer_lattice.py` 实现时
要做的工程判断(树 vs 蒙特卡洛, 见 spec 第 3 节), 不是本模块要解决的问题 ——
本模块对它列出的两种用法都是精确的。
"""

from __future__ import annotations

from collections import deque

import numpy as np

DEFAULT_WINDOW = 30
DEFAULT_THRESHOLD = 1.30
DEFAULT_REQUIRED_DAYS = 15


# ----------------------------------------------------------------------
# 历史/截至今天: 向量化
# ----------------------------------------------------------------------

def trailing_trigger_count(
    ratio: np.ndarray,
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
) -> np.ndarray:
    """给一段 S/K 序列, 逐日算"过去 window 个交易日里有几天 >= threshold".

    跟 pandas ``rolling(window, min_periods=window).sum()`` 语义一致: 历史不足
    window 天的位置返回 NaN, 不返回"不完整窗口"的偏低计数(那会系统性低估早期
    观测的达标天数)。

    Args:
        ratio: 一维数组, S/K(moneyness), 允许含 NaN(视为当天不达标, 见下)。
        window: 回看窗口(交易日), 默认 30。
        threshold: 达标阈值, 默认 1.30(转股价 130%)。

    Returns:
        与 ratio 等长的 float 数组。索引 < window-1 处为 NaN;
        索引 >= window-1 处为过去 window 天里达标的天数(0..window)。
    """
    arr = np.asarray(ratio, dtype=float)
    n = arr.shape[0]

    # NaN 视为当天不达标(缺数据不该算作"满足强赎条件"), 不让 NaN 传染进窗口和.
    with np.errstate(invalid="ignore"):
        hit = (arr >= threshold).astype(float)
    hit = np.nan_to_num(hit, nan=0.0)

    out = np.full(n, np.nan)
    if n < window:
        return out

    cumsum = np.concatenate(([0.0], np.cumsum(hit)))
    counts = cumsum[window:] - cumsum[:-window]
    out[window - 1 :] = counts
    return out


def is_call_eligible(
    ratio: np.ndarray,
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
    required: int = DEFAULT_REQUIRED_DAYS,
) -> np.ndarray:
    """`trailing_trigger_count(...) >= required` 的布尔版本.

    历史不足 window 天的位置(NaN 计数)判定为 False, 不是"未知"——
    数据不够时不能认定满足强赎条件。
    """
    counts = trailing_trigger_count(ratio, window=window, threshold=threshold)
    with np.errstate(invalid="ignore"):
        eligible = counts >= required
    return np.where(np.isnan(counts), False, eligible)


# ----------------------------------------------------------------------
# 格子前向模拟: 逐步维护(单条路径, 精确)
# ----------------------------------------------------------------------

class TrailingTriggerCounter:
    """单条模拟路径上"过去 window 天里有几天达标"的增量计数器.

    跟 `trailing_trigger_count` 算的是同一件事, 只是这个不重算整段历史 ——
    每步 O(1), 给格子/蒙特卡洛前向模拟时逐路径维护用。对单条路径精确
    (参见模块docstring"已知边界"一节)。
    """

    def __init__(self, window: int = DEFAULT_WINDOW, threshold: float = DEFAULT_THRESHOLD):
        self.window = window
        self.threshold = threshold
        self._history: deque[int] = deque()
        self._count = 0

    def push(self, ratio: float) -> int:
        """喂入新一天的 S/K, 返回更新后"过去 window 天达标天数"."""
        hit = 1 if (ratio is not None and not np.isnan(ratio) and ratio >= self.threshold) else 0
        self._history.append(hit)
        self._count += hit
        if len(self._history) > self.window:
            self._count -= self._history.popleft()
        return self._count

    @property
    def count(self) -> int:
        return self._count

    @property
    def has_full_window(self) -> bool:
        return len(self._history) >= self.window

    def is_eligible(self, required: int = DEFAULT_REQUIRED_DAYS) -> bool:
        """历史不足 window 天时返回 False, 语义同 `is_call_eligible`."""
        return self.has_full_window and self._count >= required

    def clone(self) -> "TrailingTriggerCounter":
        """深拷贝一份独立状态, 给树/模拟分叉出新路径时用."""
        new = TrailingTriggerCounter(window=self.window, threshold=self.threshold)
        new._history = deque(self._history)
        new._count = self._count
        return new
