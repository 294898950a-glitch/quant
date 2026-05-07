"""sp500_grid verifier 适配器 —— 把 GridResult 转成 BacktestResult。

设计意图
--------
orchestrator (cb_redemption.orchestrator.Orchestrator) 通过 ``verifier_fn``
hook 调用 verifier。其调用约定（来自 orchestrator.py:_run_iteration）：

    result = verifier_fn(weights, thresholds, rules, oos_event_ids=oos_ids)

为了让 sp500_grid 能跨进框架，本模块的 :func:`run_backtest` 既兼容此约定，
也保留对 cb 风格 ``run_backtest_core(snapshots, weights, thresholds, cfg, ...)``
的调用风格 —— 通过 ``snapshots`` first-arg overload 实现，详见 docstring。

约定
----
- ``weights`` 解包顺序对应 ``tunable_space.yaml`` 的 ``parameters`` 段顺序：

    weights[0] -> grid_count           (int, round)         # 旧
    weights[1] -> range_window         (int, round)         # 旧
    weights[2] -> position_per_grid    (float)              # 旧
    weights[3] -> trend_short_window   (int, round)         # 新, 趋势短窗
    weights[4] -> trend_long_window    (int, round)         # 新, 趋势长窗
    weights[5] -> vol_atr_skip_pct     (float)              # 新, ATR/close 阈值

- ``rules`` 字典支持: ``fee_pct``、``trend_filter_enabled``、
  ``vol_filter_enabled``、``vol_atr_window``;缺省回退到 yaml current 或硬编码默认。
- ``thresholds`` 是空字典（grid 不用 threshold），但允许传入。
- ``oos_event_ids`` 当传入 set 时，被解释成 OOS 日期字符串集合
  （如 ``{"20250115", "20250116", ...}``），用于过滤 oos_metrics 的 trades。
- 返回 :class:`cb_redemption.backtest.BacktestResult` 兼容结构：
  ``trades`` 列表里的元素是 :class:`cb_redemption.backtest.TradeRecord`，
  ``is_metrics``/``oos_metrics``/``all_metrics`` 是 dict，键名对齐 cb：
  ``sharpe``、``win_rate``、``total_trades``、``total_return``、``max_drawdown``。
- ``snapshots`` 参数被故意忽略（grid 不需要，但 cb-style 签名留着以便
  人工调试）；本函数自己读 ``data/sp500_grid/raw/513500_daily.parquet``。

关于 initial_capital
-------------------
yaml 里没有 initial_capital（见 yaml 顶部注释）。本模块写死 100_000.0
作为 GridConfig.initial_capital。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from strategies.cb_redemption.backtest import BacktestResult, TradeRecord
from strategies.sp500_grid.backtest import (
    GridConfig,
    GridResult,
    GridTrade,
    IS_END,
    OOS_START,
    run_grid_backtest,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_PRICES_PARQUET = _REPO_ROOT / "data" / "sp500_grid" / "raw" / "513500_daily.parquet"
DEFAULT_YAML_PATH = _HERE / "tunable_space.yaml"

#: 写死的初始资金（仅 normalization 用，搜索它没意义）。
INITIAL_CAPITAL = 100_000.0

#: weights 解包默认（与 yaml.parameters 同序）。
_DEFAULT_GRID_COUNT = 10
_DEFAULT_RANGE_WINDOW = 60
_DEFAULT_POSITION_PER_GRID = 0.10
_DEFAULT_FEE_PCT = 0.0003
_DEFAULT_TREND_SHORT_WINDOW = 20
_DEFAULT_TREND_LONG_WINDOW = 60
_DEFAULT_VOL_ATR_SKIP_PCT = 0.03
#: rules 解包默认。
_DEFAULT_TREND_FILTER_ENABLED = 0
_DEFAULT_VOL_FILTER_ENABLED = 0
_DEFAULT_VOL_ATR_WINDOW = 14


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _load_prices(prices_parquet: Path = DEFAULT_PRICES_PARQUET) -> pd.DataFrame:
    """读取 sp500 grid 价格 parquet。"""
    if not prices_parquet.exists():
        raise FileNotFoundError(
            f"sp500_grid prices parquet not found at {prices_parquet}"
        )
    df = pd.read_parquet(prices_parquet)
    return df.sort_values("date").reset_index(drop=True)


def _read_fee_from_yaml(yaml_path: Path = DEFAULT_YAML_PATH) -> float:
    """yaml 缺/解析失败 → fallback 默认 fee_pct。"""
    if not yaml_path.exists():
        return _DEFAULT_FEE_PCT
    try:
        from ruamel.yaml import YAML
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = YAML(typ="rt").load(f)
        for r in (data.get("rules", []) or []):
            if r.get("name") == "fee_pct":
                return float(r.get("current", _DEFAULT_FEE_PCT))
    except Exception:
        pass
    return _DEFAULT_FEE_PCT


def _unpack_weights(weights: list[float]) -> tuple[int, int, float]:
    """从 weights list 顺序解包前 3 个核心参数。

    保持向后兼容（旧调用方只用前 3 项）：返回签名固定为 3-tuple。
    新增的 trend_short / trend_long / vol_atr_skip_pct 通过
    :func:`_unpack_filter_params` 单独解包。

    - weights 长度不足时用默认值填充
    - grid_count / range_window 被强制 round → int 并夹紧到合理下限 (>=1)
    - position_per_grid 限制 (0, 1)
    """
    w = list(weights or [])

    def _pick(i: int, default: float) -> float:
        if i < len(w) and isinstance(w[i], (int, float)) and math.isfinite(float(w[i])):
            return float(w[i])
        return default

    grid_count = max(1, int(round(_pick(0, _DEFAULT_GRID_COUNT))))
    range_window = max(2, int(round(_pick(1, _DEFAULT_RANGE_WINDOW))))
    pos = _pick(2, _DEFAULT_POSITION_PER_GRID)
    if not (0.0 < pos <= 1.0):
        pos = _DEFAULT_POSITION_PER_GRID
    return grid_count, range_window, pos


def _unpack_filter_params(weights: list[float]) -> tuple[int, int, float]:
    """解包 weights[3..5]: trend_short_window / trend_long_window / vol_atr_skip_pct。

    缺位用 yaml/硬编码默认。窗口 round → int 并夹紧 >=1;比例限制在 (0, 1)。
    """
    w = list(weights or [])

    def _pick(i: int, default: float) -> float:
        if i < len(w) and isinstance(w[i], (int, float)) and math.isfinite(float(w[i])):
            return float(w[i])
        return default

    trend_short = max(1, int(round(_pick(3, _DEFAULT_TREND_SHORT_WINDOW))))
    trend_long = max(1, int(round(_pick(4, _DEFAULT_TREND_LONG_WINDOW))))
    skip_pct = _pick(5, _DEFAULT_VOL_ATR_SKIP_PCT)
    if not (0.0 < skip_pct < 1.0):
        skip_pct = _DEFAULT_VOL_ATR_SKIP_PCT
    return trend_short, trend_long, skip_pct


def _resolve_fee(thresholds: dict | None, rules: dict | None) -> float:
    """fee_pct 优先级：rules > thresholds > yaml > default。"""
    if rules:
        v = rules.get("fee_pct")
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return float(v)
    if thresholds:
        v = thresholds.get("fee_pct")
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return float(v)
    return _read_fee_from_yaml()


def _read_rule_from_yaml(name: str, default: float, yaml_path: Path = DEFAULT_YAML_PATH) -> float:
    """从 yaml.rules 中读 ``name`` 的 current,失败回退 default。"""
    if not yaml_path.exists():
        return default
    try:
        from ruamel.yaml import YAML
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = YAML(typ="rt").load(f)
        for r in (data.get("rules", []) or []):
            if r.get("name") == name:
                v = r.get("current", default)
                if isinstance(v, (int, float)) and math.isfinite(float(v)):
                    return float(v)
    except Exception:
        pass
    return default


def _resolve_rule(
    name: str,
    default: float,
    thresholds: dict | None,
    rules: dict | None,
) -> float:
    """规则字段查找: rules > thresholds > yaml.rules > default。"""
    if rules:
        v = rules.get(name)
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return float(v)
    if thresholds:
        v = thresholds.get(name)
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return float(v)
    return _read_rule_from_yaml(name, default)


# --------------------------------------------------------------------------- #
# GridTrade -> TradeRecord 转换
# --------------------------------------------------------------------------- #


def _grid_trades_to_trade_records(trades: list[GridTrade]) -> list[TradeRecord]:
    """每个 sell 对应一笔 TradeRecord。

    grid 的 buy/sell 不是严格配对，简化处理：每个 sell 当作一笔交易，
    使用最近一次 buy 作为入场。缺失字段填 0/空字符串。
    """
    records: list[TradeRecord] = []
    last_buy: GridTrade | None = None
    for t in trades:
        if t.side == "buy":
            last_buy = t
            continue
        if t.side == "sell":
            entry_date = last_buy.date if last_buy is not None else ""
            entry_price = float(last_buy.price) if last_buy is not None else float(t.price)
            holding_days = 0
            if last_buy is not None and last_buy.date and t.date:
                try:
                    holding_days = max(
                        0,
                        (
                            pd.to_datetime(t.date, format="%Y%m%d", errors="coerce")
                            - pd.to_datetime(last_buy.date, format="%Y%m%d", errors="coerce")
                        ).days,
                    )
                except Exception:
                    holding_days = 0
            pnl_pct = 0.0
            if entry_price > 0:
                pnl_pct = (float(t.price) - entry_price) / entry_price * 100.0
            pnl_amount = (float(t.price) - entry_price) * float(t.qty)
            records.append(TradeRecord(
                cb_code="513500",
                cb_name="sp500_etf",
                entry_date=entry_date,
                entry_price=entry_price,
                prob_entry=0.0,
                premium_entry=0.0,
                exit_date=t.date,
                exit_price=float(t.price),
                pnl_pct=round(pnl_pct, 4),
                pnl_amount=round(pnl_amount, 2),
                holding_days=int(holding_days),
                exit_reason="grid_sell",
            ))
            # 不重置 last_buy：后续 sell 仍可参照同一 buy（grid 简化）
    return records


def _grid_metrics_to_cb_metrics(m: dict) -> dict:
    """对齐 cb 关键字 ``win_rate`` / ``total_trades`` / ``sharpe`` 等。"""
    if not m:
        return {"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0,
                "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0}
    return {
        "sharpe": float(m.get("sharpe", 0.0) or 0.0),
        "win_rate": float(m.get("winrate", 0.0) or 0.0),
        "total_trades": int(m.get("trades", 0) or 0),
        "total_return": float(m.get("total_return", 0.0) or 0.0),
        "max_drawdown": float(m.get("max_drawdown", 0.0) or 0.0),
        "n_days": int(m.get("n_days", 0) or 0),
    }


def _filter_oos_by_event_ids(
    grid_result: GridResult,
    oos_event_ids: set[str],
    initial_capital: float,
) -> dict:
    """oos_event_ids 当作日期字符串集合 → 在该子集上重算 oos metrics。

    用 grid_result.equity_curve 切日期子集；trades 切日期子集。
    """
    eq = [r for r in grid_result.equity_curve if str(r["date"]) in oos_event_ids]
    tr = [t for t in grid_result.trades if str(t.date) in oos_event_ids]
    # 起始资金近似为切片首日 equity；空切片回退 initial_capital
    base = eq[0]["equity"] if eq else initial_capital
    return _grid_metrics_to_cb_metrics(_compute_metrics_from_curve(eq, tr, base))


def _compute_metrics_from_curve(
    equity_curve: list[dict],
    trades: list[GridTrade],
    initial_capital: float,
) -> dict:
    """复刻 cb-grid backtest._calc_metrics 的精简版。"""
    n_days = len(equity_curve)
    if n_days == 0:
        return {"trades": 0, "winrate": 0.0, "sharpe": 0.0,
                "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0}

    import numpy as np
    equities = np.array([r["equity"] for r in equity_curve], dtype=float)
    base = float(initial_capital) if initial_capital > 0 else 1.0
    total_return = float(equities[-1] / base - 1.0)
    peak = np.maximum.accumulate(equities)
    dd = (equities - peak) / np.where(peak > 0, peak, 1.0)
    max_drawdown = float(dd.min()) if dd.size else 0.0

    if n_days >= 2:
        rets = np.diff(equities) / np.where(equities[:-1] != 0, equities[:-1], 1.0)
        if rets.size and np.std(rets) > 1e-12:
            sharpe = float(np.mean(rets) / np.std(rets) * math.sqrt(252))
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0
    if not math.isfinite(sharpe):
        sharpe = 0.0

    sells = [t for t in trades if t.side == "sell"]
    winrate = 0.0
    if sells:
        buy_qty_total = 0
        buy_cost_total = 0.0
        win_pairs = 0
        for tr in trades:
            if tr.side == "buy":
                buy_qty_total += tr.qty
                buy_cost_total += tr.qty * tr.price
            elif tr.side == "sell":
                avg_cost = buy_cost_total / buy_qty_total if buy_qty_total > 0 else tr.price
                if tr.price > avg_cost:
                    win_pairs += 1
        winrate = win_pairs / len(sells)
    return {
        "trades": len(trades),
        "winrate": round(winrate, 4),
        "sharpe": round(sharpe, 4),
        "total_return": round(total_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "n_days": n_days,
    }


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #


def run_backtest(
    *args: Any,
    **kwargs: Any,
) -> BacktestResult:
    """sp500_grid verifier 入口。

    支持两种调用风格（运行时根据位置参数自动判别）:

    1. **orchestrator 注入风格**（v1, 默认）::

           run_backtest(weights, thresholds, rules, oos_event_ids=None)

       这是 ``Orchestrator._run_iteration`` 实际使用的协议，是主路径。

    2. **cb run_backtest_core 兼容风格**::

           run_backtest(snapshots, weights, thresholds, cfg, oos_event_ids=None)

       ``snapshots`` 被忽略；``cfg`` 不强求是 BacktestConfig，本模块自取
       ``cfg.fee_pct`` 等若有；价格仍然内部读 parquet。

    输出
    ----
    :class:`cb_redemption.backtest.BacktestResult`，``trades`` 是
    :class:`cb_redemption.backtest.TradeRecord` list；metrics 用 cb 命名键。
    """
    # ---- 解析调用风格 ----
    snapshots: Any = None
    weights: list[float]
    thresholds: dict[str, float]
    rules: dict[str, Any]
    cfg: Any = None
    oos_event_ids = kwargs.get("oos_event_ids", None)

    if len(args) >= 4 and not isinstance(args[0], list):
        # cb-style: snapshots, weights, thresholds, cfg
        snapshots = args[0]
        weights = list(args[1] or [])
        thresholds = dict(args[2] or {})
        cfg = args[3]
        # cfg 中如果带 fee_pct 等，作为 rules 替身
        rules = {}
        if cfg is not None and hasattr(cfg, "fee_pct"):
            rules["fee_pct"] = float(getattr(cfg, "fee_pct"))
    else:
        # orchestrator-style: weights, thresholds, rules
        weights = list(args[0] if len(args) >= 1 else kwargs.get("weights", []))
        thresholds = dict(args[1] if len(args) >= 2 else kwargs.get("thresholds", {}) or {})
        rules = dict(args[2] if len(args) >= 3 else kwargs.get("rules", {}) or {})

    # snapshots 参数被故意忽略
    _ = snapshots

    # ---- 构造 GridConfig ----
    grid_count, range_window, pos = _unpack_weights(weights)
    trend_short, trend_long, vol_atr_skip = _unpack_filter_params(weights)
    fee_pct = _resolve_fee(thresholds, rules)
    trend_enabled = int(round(_resolve_rule(
        "trend_filter_enabled", _DEFAULT_TREND_FILTER_ENABLED, thresholds, rules,
    )))
    vol_enabled = int(round(_resolve_rule(
        "vol_filter_enabled", _DEFAULT_VOL_FILTER_ENABLED, thresholds, rules,
    )))
    vol_atr_window = max(1, int(round(_resolve_rule(
        "vol_atr_window", _DEFAULT_VOL_ATR_WINDOW, thresholds, rules,
    ))))
    # 0/1 闸夹紧
    trend_enabled = 1 if trend_enabled >= 1 else 0
    vol_enabled = 1 if vol_enabled >= 1 else 0
    grid_cfg = GridConfig(
        grid_count=grid_count,
        range_window=range_window,
        range_method="rolling_minmax",
        initial_capital=INITIAL_CAPITAL,
        position_per_grid=pos,
        fee_pct=fee_pct,
        trend_filter_enabled=trend_enabled,
        trend_short_window=trend_short,
        trend_long_window=trend_long,
        vol_filter_enabled=vol_enabled,
        vol_atr_window=vol_atr_window,
        vol_atr_skip_pct=vol_atr_skip,
    )

    # ---- 加载价格 + 跑核心 ----
    prices = _load_prices()
    grid_result: GridResult = run_grid_backtest(prices, grid_cfg)

    # ---- 转 TradeRecord ----
    trade_records = _grid_trades_to_trade_records(grid_result.trades)

    # ---- 转 metrics ----
    is_m = _grid_metrics_to_cb_metrics(grid_result.is_metrics)
    all_m = _grid_metrics_to_cb_metrics(grid_result.all_metrics)
    if oos_event_ids is not None:
        # 用日期 set 重新过滤 oos
        oos_m = _filter_oos_by_event_ids(grid_result, set(oos_event_ids), INITIAL_CAPITAL)
    else:
        oos_m = _grid_metrics_to_cb_metrics(grid_result.oos_metrics)

    return BacktestResult(
        trades=trade_records,
        all_metrics=all_m,
        is_metrics=is_m,
        oos_metrics=oos_m,
        date_range=tuple(grid_result.date_range),
    )


__all__ = [
    "run_backtest",
    "INITIAL_CAPITAL",
    "DEFAULT_PRICES_PARQUET",
    "DEFAULT_YAML_PATH",
]
