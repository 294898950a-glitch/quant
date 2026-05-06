"""
可转债强赎博弈策略 — 严格时序回测引擎

核心设计:
- 单一数据入口：``load_historical_snapshots()`` 读取已持久化的因子快照
- 纯函数核心：``run_backtest_core(snapshots, weights, thresholds, config) -> BacktestResult``
  接受外部 DataFrame，无副作用，便于 IS/OOS 切分、滚动窗口、子线程并行
- ``BacktestEngine`` 保留为薄壳，向下兼容 optimizer.py 的现有接口
- IS / OOS 切分：IS = 2023-01-01 ~ 2024-12-31，OOS = 2025-01-01 起
- ⚠️ 因子时序污染审计见 ``docs/plans/2026-05-07-verifier-audit.md``

Usage:
    python -m strategies.cb_redemption.backtest
    python -m strategies.cb_redemption.backtest --start 20230101 --end 20260424
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from dataclasses import dataclass, asdict, field
from typing import Any

import numpy as np
import pandas as pd

from strategies.cb_redemption import config
from strategies.cb_redemption.data import (
    build_historical_snapshots,
    load_historical_snapshots,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = config.LOGIT_WEIGHTS
DEFAULT_THRESHOLDS = config.DEFAULT_THRESHOLDS_CONFIG

# 默认全样本回测区间
DEFAULT_BACKTEST_START = "20230101"
DEFAULT_BACKTEST_END = "20260424"

# IS / OOS 切分（in-sample = 训练样本，out-of-sample = 验证样本）
IS_START = "20230101"
IS_END = "20241231"
OOS_START = "20250101"


# ---------------------------------------------------------------------------
# 数据结构
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


@dataclass
class BacktestConfig:
    """纯函数 ``run_backtest_core`` 的参数容器。"""
    hold_max_days: int = 15
    target_exit_pct: float = 10.0
    stop_loss_pct: float = -8.0
    max_positions: int = 5
    position_size: float = 20_000
    top_k: int = 10
    # 信号阈值（高于此值才考虑开仓）
    alert_threshold: float = 0.6
    # 转债基础筛选
    min_close: float = 90.0
    max_close: float = 300.0
    max_premium_ratio: float = 30.0


@dataclass
class BacktestResult:
    """回测全量输出。

    Attributes:
        trades:       全部交易记录（list[TradeRecord]）
        all_metrics:  全样本绩效指标
        is_metrics:   样本内（2023-01-01 ~ 2024-12-31）绩效
        oos_metrics:  样本外（2025-01-01 起）绩效
        date_range:   (start, end) 元组
    """
    trades: list[TradeRecord] = field(default_factory=list)
    all_metrics: dict = field(default_factory=dict)
    is_metrics: dict = field(default_factory=dict)
    oos_metrics: dict = field(default_factory=dict)
    date_range: tuple[str, str] = ("", "")


# ---------------------------------------------------------------------------
# Logit 评分（保留原签名，optimizer 间接依赖 DEFAULT_WEIGHTS 维度）
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
    """8 因子 Logit 评分（5 核心 + 3 AI 持有人信号）。"""
    factors = [
        redeem_progress, premium_ratio, remaining_size,
        stock_momentum, market_sentiment,
        ai_signal_score, ai_reduction_score, ai_is_original,
    ]
    logit = sum(wi * fi for wi, fi in zip(weights, factors))
    if logit > 20:
        return 1.0
    if logit < -20:
        return 0.0
    return 1.0 / (1.0 + math.exp(-logit))


def signal_rank(snapshot: pd.DataFrame, weights: list[float]) -> pd.Series:
    """对一日快照向量化评分，返回与 snapshot 同索引的 score Series。"""
    cols = [
        "redeem_progress", "premium_ratio", "remaining_size",
        "stock_momentum", "market_sentiment",
        "ai_signal_score", "ai_reduction_score", "ai_is_original",
    ]
    # 缺列容错
    mat = np.zeros((len(snapshot), len(cols)), dtype=float)
    for j, c in enumerate(cols):
        if c in snapshot.columns:
            mat[:, j] = snapshot[c].fillna(0.0).to_numpy(dtype=float)
    w = np.asarray(weights, dtype=float)
    logit = mat @ w
    logit = np.clip(logit, -20, 20)
    return pd.Series(1.0 / (1.0 + np.exp(-logit)), index=snapshot.index)


# ---------------------------------------------------------------------------
# 纯函数核心 — run_backtest_core
# ---------------------------------------------------------------------------


def run_backtest_core(
    snapshots: pd.DataFrame,
    weights: list[float],
    thresholds: dict[str, float],
    cfg: BacktestConfig,
) -> BacktestResult:
    """严格时序回测的纯函数实现。

    输入 (snapshots, weights, thresholds, cfg) → 输出 BacktestResult。
    无副作用、无外部 IO（snapshots 由调用方提供）。

    回测循环：
      1. 按 date 升序遍历每个交易日 t
      2. 用 t 日 snapshot 评分（向量化），筛选阈值 + 价格区间 + 溢价区间
      3. 已有持仓先按 t 日 close 检查止盈/止损/到期
      4. 剩余空位按 score 降序补仓（top_k 候选池，最多 max_positions 持仓）
      5. 持仓天数 +1
      6. 末日强制平仓
    """
    if snapshots.empty:
        return BacktestResult()

    # 列保护：缺失因子用 0 填补
    snap = snapshots.copy()
    for c in [
        "redeem_progress", "premium_ratio", "remaining_size",
        "stock_momentum", "market_sentiment",
        "ai_signal_score", "ai_reduction_score", "ai_is_original",
        "close", "bond_short_name",
    ]:
        if c not in snap.columns:
            snap[c] = 0.0 if c != "bond_short_name" else ""

    # 整体评分（向量化）
    snap["score"] = signal_rank(snap, weights)

    # 按 date 分组并以 ts_code 索引化
    dates = sorted(snap["date"].unique())
    daily_groups: dict[str, pd.DataFrame] = {
        d: g.set_index("ts_code") for d, g in snap.groupby("date", sort=False)
    }

    holdings: dict[str, dict] = {}
    trades: list[TradeRecord] = []

    alert = cfg.alert_threshold

    for i, current_date in enumerate(dates):
        today = daily_groups.get(current_date)
        if today is None or today.empty:
            continue

        # 1) 检查现有持仓的退出条件
        for code in list(holdings.keys()):
            if code not in today.index:
                continue
            close_px = float(today.loc[code, "close"])
            entry_px = holdings[code]["entry_price"]
            pnl = (close_px - entry_px) / entry_px * 100.0

            if pnl >= cfg.target_exit_pct:
                trades.append(_close(code, holdings.pop(code), current_date,
                                     close_px, "take_profit", cfg.position_size))
            elif pnl <= cfg.stop_loss_pct:
                trades.append(_close(code, holdings.pop(code), current_date,
                                     close_px, "stop_loss", cfg.position_size))
            elif holdings[code]["holding_days"] >= cfg.hold_max_days:
                trades.append(_close(code, holdings.pop(code), current_date,
                                     close_px, "time_exit", cfg.position_size))

        # 2) 评估开仓
        if len(holdings) < cfg.max_positions:
            open_slots = cfg.max_positions - len(holdings)

            already_held = set(holdings.keys())
            cand = today[~today.index.isin(already_held)]

            mask = (
                (cand["score"] >= alert)
                & (cand["premium_ratio"] <= cfg.max_premium_ratio)
                & (cand["close"] >= cfg.min_close)
                & (cand["close"] <= cfg.max_close)
            )
            filt = cand[mask].sort_values("score", ascending=False)
            selected = filt.head(min(open_slots, cfg.top_k))

            for code, row in selected.iterrows():
                holdings[code] = {
                    "entry_date": current_date,
                    "entry_price": float(row["close"]),
                    "holding_days": 0,
                    "prob": float(row["score"]),
                    "premium": float(row["premium_ratio"]),
                    "cb_name": str(row.get("bond_short_name", "")),
                }

        # 3) 持仓天数 +1
        for code in holdings:
            holdings[code]["holding_days"] += 1

    # 末日强制平仓
    if dates and holdings:
        last_date = dates[-1]
        last_snap = daily_groups.get(last_date)
        for code in list(holdings.keys()):
            holding = holdings.pop(code)
            exit_px = holding["entry_price"]
            if last_snap is not None and code in last_snap.index:
                exit_px = float(last_snap.loc[code, "close"])
            trades.append(_close(code, holding, last_date, exit_px,
                                 "end_of_backtest", cfg.position_size))

    # 计算 IS / OOS / 全样本指标
    is_trades = [t for t in trades if IS_START <= t.entry_date <= IS_END]
    oos_trades = [t for t in trades if t.entry_date >= OOS_START]

    return BacktestResult(
        trades=trades,
        all_metrics=calc_performance(trades),
        is_metrics=calc_performance(is_trades),
        oos_metrics=calc_performance(oos_trades),
        date_range=(dates[0], dates[-1]) if dates else ("", ""),
    )


def _close(code: str, holding: dict, exit_date: str,
           exit_price: float, reason: str, position_size: float) -> TradeRecord:
    pnl_pct = round((exit_price - holding["entry_price"]) / holding["entry_price"] * 100, 2)
    return TradeRecord(
        cb_code=code,
        cb_name=holding["cb_name"],
        entry_date=holding["entry_date"],
        entry_price=round(holding["entry_price"], 2),
        prob_entry=round(holding["prob"], 4),
        premium_entry=round(holding["premium"], 2),
        exit_date=exit_date,
        exit_price=round(exit_price, 2),
        pnl_pct=pnl_pct,
        pnl_amount=round(position_size * pnl_pct / 100, 2),
        holding_days=holding["holding_days"],
        exit_reason=reason,
    )


# ---------------------------------------------------------------------------
# BacktestEngine — 向下兼容薄壳（保留 optimizer.py 的现有调用接口）
# ---------------------------------------------------------------------------


class BacktestEngine:
    """旧接口包装：``engine.run(snapshots) -> list[TradeRecord]``。

    新代码请直接用 ``run_backtest_core``。
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
        top_k: int = 10,
        weights: list[float] | None = None,
        thresholds: dict[str, float] | None = None,
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.weights = weights or DEFAULT_WEIGHTS
        self.thresholds = thresholds or DEFAULT_THRESHOLDS
        self.cfg = BacktestConfig(
            hold_max_days=hold_max_days,
            target_exit_pct=target_exit_pct,
            stop_loss_pct=stop_loss_pct,
            max_positions=max_positions,
            position_size=position_size,
            top_k=top_k,
            alert_threshold=self.thresholds.get("alert", 0.6),
        )
        self.trades: list[TradeRecord] = []
        self.last_result: BacktestResult | None = None

    def run(self, snapshots: pd.DataFrame | None = None) -> list[TradeRecord]:
        if snapshots is None:
            try:
                snapshots = load_historical_snapshots(self.start_date, self.end_date)
            except FileNotFoundError:
                logger.warning("快照不存在，回退到 build_historical_snapshots")
                snapshots = build_historical_snapshots(self.start_date, self.end_date)
        else:
            snapshots = snapshots[
                (snapshots["date"] >= self.start_date)
                & (snapshots["date"] <= self.end_date)
            ]

        result = run_backtest_core(snapshots, self.weights, self.thresholds, self.cfg)
        self.last_result = result
        self.trades = result.trades
        return result.trades


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def calc_performance(trades: list[TradeRecord]) -> dict:
    """计算绩效指标。空 trades 返回零值字典。"""
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0,
            "avg_return": 0, "max_return": 0, "min_return": 0,
            "profit_trades": 0, "loss_trades": 0,
            "avg_profit_pct": 0, "avg_loss_pct": 0,
            "total_pnl": 0, "sharpe": 0.0,
        }

    profits = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    return {
        "total_trades": len(trades),
        "win_rate": round(len(profits) / len(trades) * 100, 1),
        "avg_return": round(np.mean([t.pnl_pct for t in trades]), 2),
        "max_return": round(max(t.pnl_pct for t in trades), 2),
        "min_return": round(min(t.pnl_pct for t in trades), 2),
        "profit_trades": len(profits),
        "loss_trades": len(losses),
        "avg_profit_pct": round(np.mean([t.pnl_pct for t in profits]), 2) if profits else 0,
        "avg_loss_pct": round(np.mean([t.pnl_pct for t in losses]), 2) if losses else 0,
        "total_pnl": round(sum(t.pnl_amount for t in trades), 2),
        "sharpe": _calc_sharpe(trades),
    }


def _calc_sharpe(trades: list[TradeRecord]) -> float:
    """简化夏普比（按交易序列、年化到 252 交易日）。"""
    if len(trades) < 5:
        return 0.0
    returns = np.array([t.pnl_pct for t in trades])
    if np.std(returns) < 0.01:
        return 0.0
    avg_hold = np.mean([t.holding_days for t in trades if t.holding_days > 0]) or 1.0
    return round(np.mean(returns) / np.std(returns) * np.sqrt(252 / avg_hold), 2)


# ---------------------------------------------------------------------------
# 顶层入口
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
    """一键运行回测，返回 dict（含 IS / OOS / 全样本三组指标）。"""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    start = start or DEFAULT_BACKTEST_START
    end = end or DEFAULT_BACKTEST_END
    weights = weights or DEFAULT_WEIGHTS
    thresholds = thresholds or DEFAULT_THRESHOLDS

    try:
        snapshots = load_historical_snapshots(start, end)
    except FileNotFoundError:
        logger.warning("快照不存在，构建中...")
        snapshots = build_historical_snapshots(start, end)
        snapshots = snapshots[(snapshots["date"] >= start) & (snapshots["date"] <= end)]

    cfg = BacktestConfig(
        hold_max_days=hold_max_days,
        target_exit_pct=target_exit_pct,
        stop_loss_pct=stop_loss_pct,
        max_positions=max_positions,
        top_k=top_k,
        alert_threshold=thresholds.get("alert", 0.6),
    )

    t0 = time.time()
    result = run_backtest_core(snapshots, weights, thresholds, cfg)
    elapsed = time.time() - t0

    out = {
        "performance": result.all_metrics,
        "is_metrics": result.is_metrics,
        "oos_metrics": result.oos_metrics,
        "trades": [asdict(t) for t in result.trades],
        "config": {
            "start": start, "end": end,
            "weights": weights, "thresholds": thresholds,
            "is_window": f"{IS_START}~{IS_END}",
            "oos_window": f"{OOS_START}~{end}",
        },
        "elapsed": round(elapsed, 2),
    }

    if verbose:
        _print_summary(out, weights, thresholds)

    return out


def _print_summary(out: dict, weights: list[float], thresholds: dict) -> None:
    perf = out["performance"]
    is_m = out["is_metrics"]
    oos_m = out["oos_metrics"]
    print(f"\n{'='*60}")
    print(f"📊 严格时序回测结果（含 IS / OOS）")
    print(f"{'='*60}")
    print(f"区间: {out['config']['start']} ~ {out['config']['end']}")
    print(f"耗时: {out['elapsed']:.1f}s")
    print(f"\n[全样本] trades={perf['total_trades']} winrate={perf['win_rate']}% "
          f"avg={perf['avg_return']:+.2f}% sharpe={perf.get('sharpe', 0):.2f} "
          f"pnl=¥{perf['total_pnl']:+.0f}")
    print(f"[IS  ] {out['config']['is_window']}: trades={is_m['total_trades']} "
          f"winrate={is_m['win_rate']}% avg={is_m['avg_return']:+.2f}% "
          f"sharpe={is_m.get('sharpe', 0):.2f}")
    print(f"[OOS ] {out['config']['oos_window']}: trades={oos_m['total_trades']} "
          f"winrate={oos_m['win_rate']}% avg={oos_m['avg_return']:+.2f}% "
          f"sharpe={oos_m.get('sharpe', 0):.2f}")
    print(f"\n权重: {weights}")
    print(f"阈值: {thresholds}")

    trades = out["trades"]
    if trades:
        sorted_trades = sorted(trades, key=lambda t: t["pnl_pct"], reverse=True)
        print(f"\n  🏆 TOP 5 盈利:")
        for t in sorted_trades[:5]:
            print(f"    {t['cb_code']} {t['cb_name']}: "
                  f"{t['entry_date']}@{t['entry_price']:.2f} → "
                  f"{t['exit_date']}@{t['exit_price']:.2f} "
                  f"({t['pnl_pct']:+.2f}%, {t['holding_days']}d, {t['exit_reason']})")
        print(f"\n  💀 BOTTOM 5 亏损:")
        for t in sorted_trades[-5:][::-1]:
            print(f"    {t['cb_code']} {t['cb_name']}: "
                  f"{t['entry_date']}@{t['entry_price']:.2f} → "
                  f"{t['exit_date']}@{t['exit_price']:.2f} "
                  f"({t['pnl_pct']:+.2f}%, {t['holding_days']}d, {t['exit_reason']})")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="可转债强赎博弈 — 严格时序回测")
    parser.add_argument("--start", type=str, default=DEFAULT_BACKTEST_START)
    parser.add_argument("--end", type=str, default=DEFAULT_BACKTEST_END)
    parser.add_argument("--weights", nargs="*", type=float)
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hold_max_days", type=int, default=15)
    parser.add_argument("--target_exit_pct", type=float, default=10.0)
    parser.add_argument("--stop_loss_pct", type=float, default=-8.0)
    parser.add_argument("--max_positions", type=int, default=5)
    parser.add_argument("--top_k", type=int, default=10)
    args = parser.parse_args()

    thresholds = None
    if args.threshold is not None:
        thresholds = {**DEFAULT_THRESHOLDS, "alert": args.threshold}

    result = run_backtest(
        start=args.start, end=args.end,
        weights=args.weights, thresholds=thresholds,
        verbose=not args.json,
        hold_max_days=args.hold_max_days,
        target_exit_pct=args.target_exit_pct,
        stop_loss_pct=args.stop_loss_pct,
        max_positions=args.max_positions,
        top_k=args.top_k,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
