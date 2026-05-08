"""共享回测产物的数据结构。

verifier 把它的回测结果包装成这套 dataclass,judge / orchestrator / memory
等所有下游消费者都按这个结构读取。

历史:这套类型最初定义在 ``backtest.py`` 里(强赎策略的回测引擎),后来
框架剥离出来后,sp500_grid / csi500_grid 等其它策略的 verifier 也得返回
同样的形状 → 类型从 backtest.py 提到这里,变成 strategy-agnostic 的
"框架级数据契约"。

用法
----

verifier (各策略自己的): 把回测结果转成 ``BacktestResult``,trades 用
``TradeRecord`` 列表填,is_metrics / oos_metrics / all_metrics 用 dict。

judge / orchestrator: import from this module,不依赖任何具体策略。

字段说明
--------

``TradeRecord`` 的字段沿用强赎策略时代的命名(cb_code / cb_name / 等),
但语义对所有策略通用:cb_code = 标的标识,cb_name = 友好名,其余是
入场/出场/盈亏的常规字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeRecord:
    """单笔交易的完整记录。"""
    cb_code: str
    cb_name: str
    entry_date: str
    entry_price: float
    prob_entry: float
    premium_entry: float
    exit_date: str
    exit_price: float
    pnl_pct: float
    pnl_amount: float
    holding_days: int
    exit_reason: str


@dataclass
class BacktestConfig:
    """纯函数 verifier 的参数容器。

    历史上是给 cb_redemption ``run_backtest_core`` 用的,sp500_grid /
    csi500_grid 的 verifier 适配器内部不直接消费这些字段(它们用自己的
    GridConfig),但保留这个类型让 orchestrator 调 verifier_fn 时签名稳定。
    """
    hold_max_days: int = 15
    target_exit_pct: float = 10.0
    stop_loss_pct: float = -8.0
    max_positions: int = 5
    position_size: float = 20_000
    top_k: int = 10
    alert_threshold: float = 0.6
    min_close: float = 90.0
    max_close: float = 300.0
    max_premium_ratio: float = 30.0


@dataclass
class BacktestResult:
    """回测全量输出。

    Attributes
    ----------
    trades : list[TradeRecord]
        全部交易记录。
    all_metrics : dict
        全样本绩效(包含 ``sharpe`` / ``win_rate`` / ``total_trades`` /
        ``avg_return`` / ``total_return`` 等关键字)。
    is_metrics : dict
        样本内绩效(默认 IS = 2023-01-01 ~ 2024-12-31)。
    oos_metrics : dict
        样本外绩效(默认 OOS = 2025-01-01 起;但具体策略的 verifier 可能
        按 holdout 池子进一步过滤)。
    date_range : tuple[str, str]
        实际回测覆盖的 (start, end) 日期。
    """
    trades: list[TradeRecord] = field(default_factory=list)
    all_metrics: dict = field(default_factory=dict)
    is_metrics: dict = field(default_factory=dict)
    oos_metrics: dict = field(default_factory=dict)
    date_range: tuple[str, str] = ("", "")
