"""标普500ETF (513500.SH) 朴素网格策略 — 回测引擎。

设计沿用 cb_redemption/backtest.py 风格:
- 纯函数核心: ``run_grid_backtest(prices, cfg) -> GridResult`` 无 IO 无 print
- IS / OOS 切分: IS = date <= 20241231, OOS = date >= 20250101
- dataclass 输出便于后续接 orchestrator/judge

模拟逻辑（每日）:
  1. 用过去 cfg.range_window 日 close 计算区间 [low, high]（rolling_minmax）
  2. 在 [low, high] 间均分 cfg.grid_count+1 个格线
  3. 当日 close 与昨日 close 之间穿越的所有格线触发成交:
     - 向下穿越: 每穿一格 buy（用 position_per_grid 比例的当前现金）
     - 向上穿越: 每穿一格 sell（卖出 1 格手数, 没持仓不卖）
  4. 越界处理:
     - close < low: 全部清仓（"跌穿下界止损"）, 次日重新撒网格
     - close > high: 全部清仓（"涨穿上界止盈"）, 次日重新撒网格
  5. 手续费按 fee_pct 单边扣
  6. 末日强制以收盘价平仓
  7. 1 股 = 1 手（朴素简化, 真实 ETF 是 100 股 1 手）
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# IS / OOS 切分
IS_END = "20241231"
OOS_START = "20250101"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class GridConfig:
    grid_count: int = 10                       # 网格数
    range_window: int = 60                     # 滚动区间窗口（交易日）
    range_method: str = "rolling_minmax"       # 仅实现 rolling_minmax
    initial_capital: float = 100_000.0
    position_per_grid: float = 0.10            # 每格使用 10% 当前现金
    fee_pct: float = 0.0003                    # 单边手续费 3 bp
    # ------ 过滤器开关（int 而非 bool, 兼容 yaml editor / CMA-ES round） ------
    trend_filter_enabled: int = 0              # 0=关 1=开;sma_short<sma_long 暂停建仓
    trend_short_window: int = 20               # 短均线窗口
    trend_long_window: int = 60                # 长均线窗口
    vol_filter_enabled: int = 0                # 0=关 1=开;ATR/close>skip 暂停建仓
    vol_atr_window: int = 14                   # ATR 窗口（交易日）
    vol_atr_skip_pct: float = 0.03             # ATR/close 阈值,>该值禁开新仓


@dataclass
class GridTrade:
    date: str
    side: str            # "buy" | "sell"
    price: float
    qty: int
    grid_level: int      # 第几格（从下往上 0-indexed; 越界用 -1 / grid_count+1 标识）
    cash_after: float


@dataclass
class GridResult:
    trades: list[GridTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    all_metrics: dict = field(default_factory=dict)
    is_metrics: dict = field(default_factory=dict)
    oos_metrics: dict = field(default_factory=dict)
    date_range: tuple[str, str] = ("", "")


# ---------------------------------------------------------------------------
# 区间计算
# ---------------------------------------------------------------------------


def _compute_range(closes: np.ndarray, i: int, window: int, method: str) -> tuple[float, float]:
    """t=i 时, 用 closes[max(0,i-window):i] (不含当日) 计算 [low, high]。

    第一根 K 之前数据不足 window, 用前面有的全部。i==0 时无前置数据 → (close, close)。
    """
    if i <= 0:
        c = float(closes[0])
        return c, c
    start = max(0, i - window)
    win = closes[start:i]
    if win.size == 0:
        c = float(closes[i])
        return c, c
    if method == "rolling_minmax":
        return float(win.min()), float(win.max())
    # 留口子: bollinger_2std 等后续扩展, 当前版本仅 rolling_minmax
    raise ValueError(f"unsupported range_method: {method}")


def _grid_levels(low: float, high: float, n: int) -> np.ndarray:
    """在 [low, high] 间均分 n+1 条线。low==high 时退化为单线。"""
    if high <= low or n <= 0:
        return np.array([low])
    return np.linspace(low, high, n + 1)


def _compute_can_open(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    i: int,
    cfg: "GridConfig",
) -> bool:
    """计算第 i 日是否允许开新仓（向下穿格 buy）。

    规则:
    - trend_filter_enabled: SMA(short) < SMA(long) → False
    - vol_filter_enabled:   ATR(window) / close > vol_atr_skip_pct → False
    - 历史窗口不足以算 SMA/ATR 时 → True (降级开放, 与未启用过滤的旧行为兼容)
    - 两个开关均 0 → 直接 True
    """
    can_open = True

    close = float(closes[i]) if i < len(closes) else 0.0
    if close <= 0:
        return True  # 异常价格不阻断

    if int(cfg.trend_filter_enabled or 0) == 1:
        short_w = max(1, int(cfg.trend_short_window))
        long_w = max(1, int(cfg.trend_long_window))
        # 需要 long_w 根（含当日, 含=简化, 与"当日已知收盘"一致）
        if i + 1 >= long_w and i + 1 >= short_w:
            sma_short = float(np.mean(closes[i + 1 - short_w: i + 1]))
            sma_long = float(np.mean(closes[i + 1 - long_w: i + 1]))
            if sma_short < sma_long:
                can_open = False
        # 数据不足 → 不阻断（降级开放）

    if int(cfg.vol_filter_enabled or 0) == 1:
        atr_w = max(1, int(cfg.vol_atr_window))
        # ATR 用 true range, 需要前一日收盘. 至少 atr_w+1 根历史含当日
        if i + 1 >= atr_w + 1:
            tr_list: list[float] = []
            for j in range(i + 1 - atr_w, i + 1):
                if j <= 0:
                    tr_list.append(float(highs[j] - lows[j]))
                else:
                    h = float(highs[j])
                    lo = float(lows[j])
                    pc = float(closes[j - 1])
                    tr_list.append(max(h - lo, abs(h - pc), abs(lo - pc)))
            atr = float(np.mean(tr_list)) if tr_list else 0.0
            atr_pct = atr / close if close > 0 else 0.0
            if atr_pct > float(cfg.vol_atr_skip_pct):
                can_open = False
        # 数据不足 → 不阻断

    return can_open


def _level_index(price: float, levels: np.ndarray) -> int:
    """price 落在网格中的位置: <levels[0] -> -1; >levels[-1] -> len(levels); 否则 0..len-1。

    用于判断穿越方向与格数。
    """
    if price < levels[0]:
        return -1
    if price > levels[-1]:
        return len(levels)
    # 找最大的 i 使 levels[i] <= price
    idx = int(np.searchsorted(levels, price, side="right") - 1)
    return max(0, min(idx, len(levels) - 1))


# ---------------------------------------------------------------------------
# 纯函数核心
# ---------------------------------------------------------------------------


def run_grid_backtest(prices: pd.DataFrame, cfg: GridConfig) -> GridResult:
    """纯函数。

    输入 prices: 含 date / open / high / low / close 的日线 DataFrame, 按 date 升序。
    输出 GridResult。
    """
    if prices is None or prices.empty:
        return GridResult()

    df = prices.sort_values("date").reset_index(drop=True)
    dates = df["date"].astype(str).tolist()
    closes = df["close"].to_numpy(dtype=float)
    # ATR 需要 high/low; 缺失时用 close 填补（朴素降级, 等价于 TR=0 不阻断）
    if "high" in df.columns:
        highs = df["high"].to_numpy(dtype=float)
    else:
        highs = closes.copy()
    if "low" in df.columns:
        lows = df["low"].to_numpy(dtype=float)
    else:
        lows = closes.copy()
    n = len(df)

    cash = float(cfg.initial_capital)
    holding_qty = 0          # 持仓股数 (1 股 = 1 手, 朴素简化)
    trades: list[GridTrade] = []
    equity_curve: list[dict] = []

    prev_close: float | None = None
    prev_levels: np.ndarray | None = None
    prev_idx: int | None = None  # 昨日 close 在网格中的层级
    just_reset = False           # 上一根越界清仓后, 当日重新撒网格、不交易

    for i in range(n):
        date = dates[i]
        close = float(closes[i])

        # 1) 撒网格 (基于不含当日的滚动窗口)
        low, high = _compute_range(closes, i, cfg.range_window, cfg.range_method)
        levels = _grid_levels(low, high, cfg.grid_count)
        cur_idx = _level_index(close, levels)

        # 1.5) 过滤闸: 仅决定本日是否允许"开新仓"(向下穿格 buy)
        # 卖出 / 越界平仓不受影响 —— 已有仓位继续按格线减仓 / 越界止损止盈始终生效
        can_open = _compute_can_open(closes, highs, lows, i, cfg)

        # 2) 触发成交 (跨越格线)
        if (
            prev_close is not None
            and prev_levels is not None
            and prev_idx is not None
            and not just_reset
        ):
            # 越界: 全部清仓（用 close 平仓）
            if cur_idx < 0 or cur_idx >= len(levels):
                if holding_qty > 0:
                    proceeds = close * holding_qty * (1 - cfg.fee_pct)
                    cash += proceeds
                    trades.append(GridTrade(
                        date=date, side="sell", price=close,
                        qty=holding_qty,
                        grid_level=(-1 if cur_idx < 0 else len(levels)),
                        cash_after=cash,
                    ))
                    holding_qty = 0
                # 越界后, 标记下一日用新网格但当日不再做格线交易
                just_reset = True
            else:
                # 穿越方向 (基于 prev close 与今日 close 的相对位置, 用 levels)
                # 用 prev_close 重投到当前 levels 上, 算跨越数
                prev_idx_on_cur = _level_index(prev_close, levels)
                if cur_idx < prev_idx_on_cur and can_open:
                    # 向下: 每穿一格 buy 一次（仅在 can_open=True 时执行;闸关则跳过, 不计 trade）
                    crossings = prev_idx_on_cur - cur_idx
                    for k in range(crossings):
                        # 第 k 次穿过的格线 (从上往下) = levels[prev_idx_on_cur - k]
                        gl = prev_idx_on_cur - k - 1  # 落入的格区底端 index
                        spend = cash * cfg.position_per_grid
                        if spend <= 0 or close <= 0:
                            break
                        qty = int(spend / (close * (1 + cfg.fee_pct)))
                        if qty <= 0:
                            break
                        cost = qty * close * (1 + cfg.fee_pct)
                        if cost > cash:
                            break
                        cash -= cost
                        holding_qty += qty
                        trades.append(GridTrade(
                            date=date, side="buy", price=close, qty=qty,
                            grid_level=max(0, gl), cash_after=cash,
                        ))
                elif cur_idx > prev_idx_on_cur:
                    # 向上: 每穿一格 sell 1 格 (按 grid_count 等分持仓)
                    crossings = cur_idx - prev_idx_on_cur
                    for k in range(crossings):
                        if holding_qty <= 0:
                            break
                        # 卖出 1 格的份额 = 总持仓 / grid_count, 至少 1 股
                        qty = max(1, holding_qty // max(1, cfg.grid_count))
                        qty = min(qty, holding_qty)
                        proceeds = qty * close * (1 - cfg.fee_pct)
                        cash += proceeds
                        holding_qty -= qty
                        trades.append(GridTrade(
                            date=date, side="sell", price=close, qty=qty,
                            grid_level=prev_idx_on_cur + k + 1, cash_after=cash,
                        ))
        else:
            # 第一日 / 刚重置 → 仅记录, 不交易
            just_reset = False

        # 3) equity
        mark_value = holding_qty * close
        equity = cash + mark_value
        equity_curve.append({
            "date": date,
            "cash": cash,
            "holding_qty": holding_qty,
            "mark_value": mark_value,
            "equity": equity,
        })

        # 4) 翻页
        prev_close = close
        prev_levels = levels
        prev_idx = cur_idx

    # 5) 末日强制平仓
    if holding_qty > 0 and n > 0:
        last_date = dates[-1]
        last_close = float(closes[-1])
        proceeds = last_close * holding_qty * (1 - cfg.fee_pct)
        cash += proceeds
        trades.append(GridTrade(
            date=last_date, side="sell", price=last_close,
            qty=holding_qty, grid_level=-99, cash_after=cash,
        ))
        holding_qty = 0
        # 修正最后一条 equity_curve
        if equity_curve:
            equity_curve[-1] = {
                "date": last_date,
                "cash": cash,
                "holding_qty": 0,
                "mark_value": 0.0,
                "equity": cash,
            }

    # 6) IS / OOS metrics
    is_curve = [r for r in equity_curve if r["date"] <= IS_END]
    oos_curve = [r for r in equity_curve if r["date"] >= OOS_START]
    is_trades = [t for t in trades if t.date <= IS_END]
    oos_trades = [t for t in trades if t.date >= OOS_START]

    return GridResult(
        trades=trades,
        equity_curve=equity_curve,
        all_metrics=_calc_metrics(equity_curve, trades, cfg.initial_capital),
        is_metrics=_calc_metrics(is_curve, is_trades, cfg.initial_capital),
        oos_metrics=_calc_metrics(
            oos_curve, oos_trades,
            # OOS 起点资金按区间首日 equity 计算 (近似), 若空则回退 initial
            oos_curve[0]["equity"] if oos_curve else cfg.initial_capital,
        ),
        date_range=(dates[0], dates[-1]) if dates else ("", ""),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _calc_metrics(
    equity_curve: list[dict],
    trades: list[GridTrade],
    initial_capital: float,
) -> dict:
    """计算 trades / winrate / sharpe / total_return / max_drawdown / n_days。"""
    n_days = len(equity_curve)
    if n_days == 0:
        return {
            "trades": 0, "winrate": 0.0, "sharpe": 0.0,
            "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0,
        }

    equities = np.array([r["equity"] for r in equity_curve], dtype=float)
    base = float(initial_capital) if initial_capital > 0 else 1.0
    total_return = float(equities[-1] / base - 1.0)

    # Max drawdown
    peak = np.maximum.accumulate(equities)
    dd = (equities - peak) / np.where(peak > 0, peak, 1.0)
    max_drawdown = float(dd.min()) if dd.size else 0.0

    # Sharpe (按日收益, 年化 sqrt(252))
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

    # Winrate: 简化 — 每个 sell 视为一对的平仓
    sells = [t for t in trades if t.side == "sell"]
    n_sells = len(sells)
    # 用 sell 价 vs 之前 buy 平均成本对比, 过粗则降级用 sell 价 vs 区间均价
    # 这里采用最简化: 净盈利成对 = sell 笔数中其价格高于该 sell 之前所有 buy 平均价
    win_pairs = 0
    if n_sells > 0:
        buy_qty_total = 0
        buy_cost_total = 0.0
        for tr in trades:
            if tr.side == "buy":
                buy_qty_total += tr.qty
                buy_cost_total += tr.qty * tr.price
            elif tr.side == "sell":
                avg_cost = (
                    buy_cost_total / buy_qty_total
                    if buy_qty_total > 0
                    else tr.price
                )
                if tr.price > avg_cost:
                    win_pairs += 1
        winrate = win_pairs / n_sells
    else:
        winrate = 0.0

    return {
        "trades": len(trades),
        "winrate": round(winrate, 4),
        "sharpe": round(sharpe, 4),
        "total_return": round(total_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "n_days": n_days,
    }
