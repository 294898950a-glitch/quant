"""
可转债强赎博弈策略 — 严格时序回测引擎

核心设计：
- 使用 build_historical_snapshots() 生成的每日快照，绝对避免前视偏差
- 每个交易日收盘后：根据当前可用信号重新计算评分，更新持仓
- 纯本地 Parquet 数据，无 API 调用
- 支持指定回测区间，默认最近 2 年

Usage:
    python -m strategies.cb_redemption.backtest
    python -m strategies.cb_redemption.backtest --start 20250101 --end 20260424 --weights 3.5 -2.0 -1.2 1.2 0.9
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from strategies.cb_redemption import config
from strategies.cb_redemption.data import build_historical_snapshots

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = config.LOGIT_WEIGHTS
DEFAULT_THRESHOLDS = config.DEFAULT_THRESHOLDS_CONFIG

# 默认回测区间
DEFAULT_BACKTEST_START = "20240101"
DEFAULT_BACKTEST_END = "20260424"


# ---------------------------------------------------------------------------
# Trade Record
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
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


# ---------------------------------------------------------------------------
# Logit Scoring
# ---------------------------------------------------------------------------


def logit_prob(
    redeem_progress: float,
    premium_ratio: float,
    remaining_size: float,
    stock_momentum: float,
    market_sentiment: float,
    ai_signal_score: float,
    ai_reduction_score: float,
    ai_is_original: float,
    weights: list[float],
) -> float:
    """8 因子 Logit 评分（5核心 + 3 AI持有人信号）。"""
    factors = [redeem_progress, premium_ratio, remaining_size, stock_momentum, market_sentiment,
               ai_signal_score, ai_reduction_score, ai_is_original]
    logit = sum(wi * fi for wi, fi in zip(weights, factors))
    if logit > 20:
        return 1.0
    if logit < -20:
        return 0.0
    return 1.0 / (1.0 + math.exp(-logit))


def _score_row(row: pd.Series, weights: list[float]) -> float:
    return logit_prob(
        row["redeem_progress"],
        row["premium_ratio"],
        row["remaining_size"],
        row["stock_momentum"],
        row["market_sentiment"],
        row.get("ai_signal_score", 0),
        row.get("ai_reduction_score", 0),
        row.get("ai_is_original", 0),
        weights,
    )


# ---------------------------------------------------------------------------
# Backtest Engine (Strict Temporal)
# ---------------------------------------------------------------------------


class BacktestEngine:
    """
    严格时序回测引擎。

    模拟流程：
    1. 加载 N 个交易日的历史快照（build_historical_snapshots）
    2. 对每个交易日 t，使用 t 时刻可用信号评分全部在持且满足条件的转债
    3. 每日检查止盈/止损/到期退出
    4. 评分最高的转债进入持仓队列（通过阈值 + 排名）
    5. 计算整体绩效

    关键约束：
    - signal_date = t 交易日的快照数据（t 收盘后可知的信息）
    - entry 价格 = t 交易日收盘价
    - hold 使用 t+1, t+2, ... 日的真实收盘价
    """

    def __init__(
        self,
        start_date: str = DEFAULT_BACKTEST_START,
        end_date: str = DEFAULT_BACKTEST_END,
        hold_max_days: int = 15,
        target_exit_pct: float = 10.0,
        stop_loss_pct: float = -8.0,
        max_positions: int = 5,
        position_size: float = 20_000,
        top_k: int = 10,               # 候选池池大小
        weights: list[float] | None = None,
        thresholds: dict[str, float] | None = None,
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.hold_max_days = hold_max_days
        self.target_exit_pct = target_exit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_positions = max_positions
        self.position_size = position_size
        self.top_k = top_k
        self.weights = weights or DEFAULT_WEIGHTS
        self.alert_threshold = (thresholds or DEFAULT_THRESHOLDS).get("alert", 0.6)

        self.trades: list[TradeRecord] = []

    # ── entry point ──────────────────────────────────────────────────

    def run(self, snapshots: pd.DataFrame | None = None) -> list[TradeRecord]:
        """执行回测。

        Args:
            snapshots: 可选，预构建的历史快照。如果为 None 则自动构建。
        """
        self.trades = []

        logger.info(f"加载历史快照: {self.start_date} ~ {self.end_date}")
        t0 = time.time()
        if snapshots is not None:
            _snapshots = snapshots
        else:
            _snapshots = build_historical_snapshots(self.start_date, self.end_date)
        if _snapshots.empty:
            logger.warning("无快照数据")
            return []
        elapsed = time.time() - t0
        logger.info(f"快照加载: {len(_snapshots)} 行, {_snapshots['date'].nunique()} 交易日, {elapsed:.1f}s")

        # 按日期分组，严格按时间顺序处理
        dates = sorted(_snapshots["date"].unique())
        logger.info(f"回测期间: {dates[0]} ~ {dates[-1]}, 共 {len(dates)} 交易日")

        # 构建 {date: DataFrame} 索引
        daily_groups = {d: _snapshots[_snapshots["date"] == d].set_index("ts_code") for d in dates}

        # 当前持仓: {ts_code: {entry_date, entry_price, holding_days, ...}}
        holdings: dict[str, dict] = {}

        for i, current_date in enumerate(dates):
            # 第1步：检查持仓退出条件（使用当前日期收盘价）
            if holdings and i + 1 < len(dates):
                next_date = dates[i + 1]
                # 实际上退出的价格应该用当前收盘价，即 current_date 的 close
                today_snapshot = daily_groups.get(current_date)
                if today_snapshot is not None:
                    self._check_exits(holdings, current_date, today_snapshot)

            # 第2步：如果当前日期有快照，生成新信号
            snapshot = daily_groups.get(current_date)
            if snapshot is not None and snapshot.empty:
                continue
            if snapshot is not None:
                self._enter_new_positions(holdings, current_date, snapshot)

            # 第3步：更新持仓天数
            for code in list(holdings.keys()):
                holdings[code]["holding_days"] += 1

        # 回测结束：强制平仓所有持仓
        last_date = dates[-1]
        last_snapshot = daily_groups.get(last_date)
        for code in list(holdings.keys()):
            holding = holdings[code]
            exit_price = holding["entry_price"]  # 保守：用成本价结算
            if last_snapshot is not None and code in last_snapshot.index:
                exit_price = float(last_snapshot.loc[code, "close"])
            self._close_trade(code, holding, last_date, exit_price, "end_of_backtest", holdings)

        return self.trades

    # ── exit check ───────────────────────────────────────────────────

    def _check_exits(
        self,
        holdings: dict[str, dict],
        current_date: str,
        snapshot: pd.DataFrame,
    ) -> None:
        """检查所有持仓的止盈/止损/超时退出。"""
        for code in list(holdings.keys()):
            if code not in snapshot.index:
                continue

            close_price = float(snapshot.loc[code, "close"])
            entry_price = holdings[code]["entry_price"]
            pnl = (close_price - entry_price) / entry_price * 100.0

            if pnl >= self.target_exit_pct:
                self._close_trade(code, holdings[code], current_date, close_price, "take_profit", holdings)
            elif pnl <= self.stop_loss_pct:
                self._close_trade(code, holdings[code], current_date, close_price, "stop_loss", holdings)
            elif holdings[code]["holding_days"] >= self.hold_max_days:
                self._close_trade(code, holdings[code], current_date, close_price, "time_exit", holdings)

    def _close_trade(self, code: str, holding: dict, exit_date: str,
                     exit_price: float, reason: str, holdings: dict | None = None) -> None:
        """平仓并记录交易。"""
        pnl_pct = round((exit_price - holding["entry_price"]) / holding["entry_price"] * 100, 2)
        trade = TradeRecord(
            cb_code=code,
            cb_name=holding["cb_name"],
            entry_date=holding["entry_date"],
            entry_price=round(holding["entry_price"], 2),
            prob_entry=round(holding["prob"], 4),
            premium_entry=round(holding["premium"], 2),
            exit_date=exit_date,
            exit_price=round(exit_price, 2),
            pnl_pct=pnl_pct,
            pnl_amount=round(self.position_size * pnl_pct / 100, 2),
            holding_days=holding["holding_days"],
            exit_reason=reason,
        )
        self.trades.append(trade)
        if holdings is not None:
            del holdings[code]

    # ── entry logic ──────────────────────────────────────────────────

    def _enter_new_positions(
        self,
        holdings: dict[str, dict],
        current_date: str,
        snapshot: pd.DataFrame,
    ) -> None:
        """根据当前快照信号评分，选出新标的进入持仓。"""
        if len(holdings) >= self.max_positions:
            return

        open_slots = self.max_positions - len(holdings)

        # 评分
        scored = snapshot.copy()
        scored["score"] = scored.apply(lambda r: _score_row(r, self.weights), axis=1)

        # 过滤已在持仓的
        already_held = set(holdings.keys())
        candidates = scored[~scored.index.isin(already_held)].copy()

        if candidates.empty:
            return

        # 过滤条件
        mask = (
            (candidates["score"] >= self.alert_threshold)
            & (candidates["premium_ratio"] <= 30.0)
            & (candidates["close"] >= 90)
            & (candidates["close"] <= 300)
        )
        filtered = candidates[mask].sort_values("score", ascending=False)

        if filtered.empty:
            return

        # 选 top K
        selected = filtered.head(min(open_slots, self.top_k))

        for code, row in selected.iterrows():
            if code in holdings:
                continue
            holdings[code] = {
                "entry_date": current_date,
                "entry_price": float(row["close"]),
                "holding_days": 0,
                "prob": float(row["score"]),
                "premium": float(row["premium_ratio"]),
                "cb_name": str(row.get("bond_short_name", "")),
            }


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def calc_performance(trades: list[TradeRecord]) -> dict:
    """计算绩效指标。"""
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0,
            "avg_return": 0, "max_return": 0, "min_return": 0,
            "profit_trades": 0, "loss_trades": 0,
            "avg_profit_pct": 0, "avg_loss_pct": 0,
            "total_pnl": 0,
        }

    profits = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    return {
        "total_trades": len(trades),
        "win_rate": round(len(profits) / len(trades) * 100, 1) if trades else 0,
        "avg_return": round(np.mean([t.pnl_pct for t in trades]), 2) if trades else 0,
        "max_return": round(max(t.pnl_pct for t in trades), 2) if trades else 0,
        "min_return": round(min(t.pnl_pct for t in trades), 2) if trades else 0,
        "profit_trades": len(profits),
        "loss_trades": len(losses),
        "avg_profit_pct": round(np.mean([t.pnl_pct for t in profits]), 2) if profits else 0,
        "avg_loss_pct": round(np.mean([t.pnl_pct for t in losses]), 2) if losses else 0,
        "total_pnl": round(sum(t.pnl_amount for t in trades), 2),
        "sharpe": _calc_sharpe(trades),
    }


def _calc_sharpe(trades: list[TradeRecord]) -> float:
    """简化夏普比（用交易维而不是日维）。"""
    if len(trades) < 5:
        return 0.0
    returns = np.array([t.pnl_pct for t in trades])
    if np.std(returns) < 0.01:
        return 0.0
    return round(np.mean(returns) / np.std(returns) * np.sqrt(252 / np.mean([t.holding_days for t in trades if t.holding_days > 0])), 2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_backtest(
    start: str | None = None,
    end: str | None = None,
    weights: list[float] | None = None,
    thresholds: dict[str, float] | None = None,
    verbose: bool = False,
    hold_max_days: int = 15,
    target_exit_pct: float = 10.0,
    stop_loss_pct: float = -8.0,
    max_positions: int = 5,
    top_k: int = 10,
) -> dict:
    """一键运行回测。"""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    engine = BacktestEngine(
        start_date=start or DEFAULT_BACKTEST_START,
        end_date=end or DEFAULT_BACKTEST_END,
        weights=weights or DEFAULT_WEIGHTS,
        thresholds=thresholds or DEFAULT_THRESHOLDS,
        hold_max_days=hold_max_days,
        target_exit_pct=target_exit_pct,
        stop_loss_pct=stop_loss_pct,
        max_positions=max_positions,
        top_k=top_k,
    )

    t0 = time.time()
    trades = engine.run()
    elapsed = time.time() - t0

    perf = calc_performance(trades)

    result = {
        "performance": perf,
        "trades": [asdict(t) for t in trades],
        "config": {
            "start": start or DEFAULT_BACKTEST_START,
            "end": end or DEFAULT_BACKTEST_END,
            "weights": weights or DEFAULT_WEIGHTS,
            "thresholds": thresholds or DEFAULT_THRESHOLDS,
        },
        "elapsed": round(elapsed, 2),
    }

    if verbose:
        print(f"\n{'='*50}")
        print(f"📊 严格时序回测结果")
        print(f"{'='*50}")
        print(f"区间: {result['config']['start']} ~ {result['config']['end']}")
        print(f"耗时: {elapsed:.1f}s")
        print(f"交易次数: {perf['total_trades']}")
        print(f"胜率: {perf['win_rate']}% ({perf['profit_trades']}/{perf['total_trades']})")
        print(f"平均收益: {perf['avg_return']:+.2f}%")
        print(f"最大盈利: {perf['max_return']:+.2f}%")
        print(f"最大亏损: {perf['min_return']:+.2f}%")
        print(f"平均盈利: {perf['avg_profit_pct']:+.2f}%")
        print(f"平均亏损: {perf['avg_loss_pct']:+.2f}%")
        print(f"总盈亏: ¥{perf['total_pnl']:+.0f}")
        print(f"夏普比: {perf.get('sharpe', 0):.2f}")
        print(f"\n权重: {weights or DEFAULT_WEIGHTS}")
        print(f"阈值: {thresholds or DEFAULT_THRESHOLDS}")

        if trades:
            print(f"\n交易明细 ({len(trades)} 笔):")
            # 按收益排序，显示前10最好和最差
            sorted_trades = sorted(trades, key=lambda t: t.pnl_pct, reverse=True)
            print(f"\n  🏆 TOP 5 盈利:")
            for t in sorted_trades[:5]:
                print(f"    {t.cb_code} {t.cb_name}: {t.entry_date}@{t.entry_price:.2f} → "
                      f"{t.exit_date}@{t.exit_price:.2f} ({t.pnl_pct:+.2f}%, {t.holding_days}d, {t.exit_reason})")
            print(f"\n  💀 BOTTOM 5 亏损:")
            for t in sorted_trades[-5:][::-1]:
                print(f"    {t.cb_code} {t.cb_name}: {t.entry_date}@{t.entry_price:.2f} → "
                      f"{t.exit_date}@{t.exit_price:.2f} ({t.pnl_pct:+.2f}%, {t.holding_days}d, {t.exit_reason})")

        print(f"{'='*50}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="可转债强赎博弈 — 严格时序回测")
    parser.add_argument("--start", type=str, default=DEFAULT_BACKTEST_START, help="起始日 YYYYMMDD")
    parser.add_argument("--end", type=str, default=DEFAULT_BACKTEST_END, help="截止日 YYYYMMDD")
    parser.add_argument("--weights", nargs="*", type=float, help="Logit 权重列表（8个）")
    parser.add_argument("--threshold", type=float, help="信号阈值 (默认 0.6)")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--hold_max_days", type=int, default=15, help="最大持仓天数")
    parser.add_argument("--target_exit_pct", type=float, default=10.0, help="止盈百分比")
    parser.add_argument("--stop_loss_pct", type=float, default=-8.0, help="止损百分比")
    parser.add_argument("--max_positions", type=int, default=5, help="最大持仓数量")
    parser.add_argument("--top_k", type=int, default=10, help="每日候选池大小")
    args = parser.parse_args()

    thresholds = None
    if args.threshold is not None:
        thresholds = {**DEFAULT_THRESHOLDS, "alert": args.threshold}

    result = run_backtest(
        start=args.start,
        end=args.end,
        weights=args.weights,
        thresholds=thresholds,
        verbose=not args.json,
        hold_max_days=args.hold_max_days,
        target_exit_pct=args.target_exit_pct,
        stop_loss_pct=args.stop_loss_pct,
        max_positions=args.max_positions,
        top_k=args.top_k,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
