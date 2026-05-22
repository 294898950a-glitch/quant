#!/usr/bin/env python3
"""Regime-adaptive exit evaluator for cb_arb_value_gap_switch.

按每日市场状态（高波/低波）选择不同的 duration-adaptive exit 衰减参数。
用 CSI 300 滚动 N 日收益率百分位对每个交易日做二分类：
  - 高波状态（收益率绝对值在 top 50%）→ 用 high_vol_decay_factor
  - 低波状态（收益率绝对值在 bottom 50%）→ 用 low_vol_decay_factor

退出阈值线性衰减公式：
  threshold = initial_threshold_fraction * (1 - held_days / (effective_max_hold_days * decay_factor))
若 current_gap / entry_gap > threshold 且 held_days >= min_hold_days 则退出。
"""

import argparse
import json
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import yaml


# ── data requirements ────────────────────────────────────────────────

def declare_data_requirements(command, spec):
    """声明执行所需的所有数据文件。"""
    return {
        "required_files": [
            {"path": "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet"},
            {"path": "data/cb_warehouse/csi300_daily.parquet"},
            {"path": "data/cb_warehouse/cb_basic.parquet"},
            {"path": "data/cb_warehouse/stk_daily_qfq.parquet"},
        ]
    }


# ── regime classifier ─────────────────────────────────────────────────

def build_regime_labels(csi300_df, lookback):
    """用 CSI 300 滚动 lookback 日收益率百分位生成每日高波/低波标签。

    逻辑：
    - 计算每日简单收益率
    - 取滚动 lookback 日的收益率序列，计算当前收益率在该窗口内的百分位
    - 百分位 >= 50%（即收益率绝对值偏高）→ high_vol
    - 百分位 < 50% → low_vol

    Returns: DataFrame with columns [trade_date, regime]
    """
    df = csi300_df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["ret"] = df["close"].pct_change()

    # 滚动百分位：当前收益率在最近 lookback 天中的排位
    df["ret_abs"] = df["ret"].abs()
    df["ret_abs_pct"] = (
        df["ret_abs"]
        .rolling(window=lookback, min_periods=max(20, lookback // 2))
        .apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
    )

    df["regime"] = np.where(df["ret_abs_pct"] >= 0.50, "high_vol", "low_vol")
    return df[["trade_date", "regime"]].copy()


# ── portfolio simulator ───────────────────────────────────────────────

def simulate_regime_adaptive_exit(
    gap_df,
    regime_df,
    initial_threshold_fraction,
    high_vol_decay_factor,
    low_vol_decay_factor,
    min_hold_days,
    effective_max_hold_days,
    max_rank=10,
):
    """模拟基于市场状态的 adaptive exit 策略。

    对每个交易日：
    1. 取 value_gap_amount > 0 且 rank <= max_rank 的转债作为候选持仓
    2. 新出现的转债开仓（记录 entry_gap, entry_date）
    3. 已有持仓按当日 regime 选 decay_factor，计算退出阈值
    4. 若 current_gap / entry_gap > threshold 且 held_days >= min_hold_days，平仓
    5. 记录每笔交易的 entry_date, exit_date, entry_gap, exit_gap, held_days, pnl_pct

    Returns: list of trade dicts
    """
    gap_df = gap_df.copy()
    gap_df["trade_date"] = pd.to_datetime(gap_df["trade_date"])
    gap_df = gap_df.sort_values(["trade_date", "rank"]).reset_index(drop=True)

    regime_df["trade_date"] = pd.to_datetime(regime_df["trade_date"])
    regime_map = dict(zip(regime_df["trade_date"], regime_df["regime"]))

    # 仅保留有正缺口且排名靠前
    candidates = gap_df[(gap_df["value_gap_amount"] > 0) & (gap_df["rank"] <= max_rank)].copy()

    trades = []
    positions = {}  # ts_code -> {entry_date, entry_gap}

    all_dates = sorted(candidates["trade_date"].unique())
    if not len(all_dates):
        return trades

    for date in all_dates:
        regime = regime_map.get(date, "low_vol")  # 默认低波
        decay_factor = high_vol_decay_factor if regime == "high_vol" else low_vol_decay_factor

        today_candidates = candidates[candidates["trade_date"] == date]
        today_codes = set(today_candidates["ts_code"])

        # 开仓：新出现的转债
        for _, row in today_candidates.iterrows():
            code = row["ts_code"]
            if code not in positions:
                positions[code] = {
                    "entry_date": date,
                    "entry_gap": row["value_gap_amount"],
                }

        # 检查退出条件
        exited_codes = []
        for code, pos in list(positions.items()):
            held_days = (date - pos["entry_date"]).days
            if held_days < min_hold_days:
                continue

            # 获取当前快照
            current_snap = today_candidates[today_candidates["ts_code"] == code]
            if len(current_snap) == 0:
                # 不在今日候选里 → 自然退出（跌出排名）
                current_gap = 0.0
            else:
                current_gap = current_snap["value_gap_amount"].iloc[0]

            if current_gap == 0.0 or pos["entry_gap"] == 0.0:
                gap_ratio = 0.0
            else:
                gap_ratio = current_gap / pos["entry_gap"]

            # 阈值线性衰减
            effective_hold = effective_max_hold_days * decay_factor
            if effective_hold <= 0:
                threshold = 0.0
            else:
                threshold = initial_threshold_fraction * max(0.0, 1.0 - held_days / effective_hold)

            # 退出条件
            exit_triggered = gap_ratio > threshold

            # 强制到期退出
            force_exit = held_days >= effective_max_hold_days

            if exit_triggered or force_exit or len(current_snap) == 0:
                exit_gap = current_gap if len(current_snap) > 0 else 0.0
                trades.append({
                    "ts_code": code,
                    "entry_date": pos["entry_date"],
                    "exit_date": date,
                    "held_days": held_days,
                    "entry_gap": pos["entry_gap"],
                    "exit_gap": exit_gap,
                    "exit_regime": regime,
                    "decay_factor_used": decay_factor,
                    "threshold_at_exit": float(threshold),
                    "gap_ratio_at_exit": float(gap_ratio),
                    "exit_reason": (
                        "force_max_hold" if force_exit
                        else "dropped_out" if len(current_snap) == 0
                        else "threshold_breach"
                    ),
                })
                exited_codes.append(code)

        for code in exited_codes:
            del positions[code]

    # 关闭仍持有的仓位（期末强制平仓）
    final_date = all_dates[-1]
    for code, pos in positions.items():
        held_days = (final_date - pos["entry_date"]).days
        trades.append({
            "ts_code": code,
            "entry_date": pos["entry_date"],
            "exit_date": final_date,
            "held_days": held_days,
            "entry_gap": pos["entry_gap"],
            "exit_gap": 0.0,
            "exit_regime": "end_of_period",
            "decay_factor_used": None,
            "threshold_at_exit": 0.0,
            "gap_ratio_at_exit": 0.0,
            "exit_reason": "end_of_period",
        })

    return trades


# ── metrics ────────────────────────────────────────────────────────────

def compute_excess_compound(trades, index_df, start_date, end_date):
    """计算累计超额复合收益率。

    策略收益：每笔交易用 (exit_gap - entry_gap) / entry_gap 近似盈亏
    基准收益：同期 CSI 300 的累计收益率
    超额 = 策略累计收益 - 基准累计收益
    """
    if not trades:
        return 0.0, 0.0, 0.0

    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date)

    period_trades = [t for t in trades
                     if t["exit_date"] >= start_dt and t["entry_date"] <= end_dt]

    if not period_trades:
        return 0.0, 0.0, 0.0

    # 策略收益：每笔用 (exit_gap - entry_gap) / entry_gap（缺口收敛收益）
    strategy_returns = []
    for t in period_trades:
        if t["entry_gap"] > 0:
            trade_ret = (t["exit_gap"] - t["entry_gap"]) / t["entry_gap"]
            strategy_returns.append(trade_ret)

    if not strategy_returns:
        strategy_cum = 0.0
    else:
        strategy_cum = np.prod([1 + r for r in strategy_returns]) - 1

    # 基准收益：CSI 300 同期
    bench_start_row = index_df[index_df["trade_date"] == str(start_dt.date())]
    bench_end_row = index_df[index_df["trade_date"] == str(end_dt.date())]
    if len(bench_start_row) > 0 and len(bench_end_row) > 0:
        bench_cum = bench_end_row["close"].iloc[0] / bench_start_row["close"].iloc[0] - 1
    else:
        bench_cum = 0.0

    excess = strategy_cum - bench_cum
    return float(strategy_cum), float(bench_cum), float(excess)


def compute_sharpe(trades, index_df, year):
    """计算某年的年化夏普比率（近似）。"""
    start = pd.Timestamp(f"{year}-01-01")
    end = pd.Timestamp(f"{year}-12-31")

    year_trades = [t for t in trades
                   if t["exit_date"] >= start and t["entry_date"] <= end]

    if len(year_trades) < 3:
        return 0.0

    returns = []
    for t in year_trades:
        if t["entry_gap"] > 0:
            returns.append((t["exit_gap"] - t["entry_gap"]) / t["entry_gap"])

    if len(returns) < 3:
        return 0.0

    mean_ret = np.mean(returns)
    std_ret = np.std(returns, ddof=1)
    if std_ret == 0:
        return 0.0

    # 年化（假设交易均匀分布）
    ann_factor = np.sqrt(250 / (len(returns) / len(year_trades) * 250)) if len(year_trades) > 0 else 1
    return float(mean_ret / std_ret * np.sqrt(252))


def compute_max_drawdown(returns_series):
    """计算最大回撤。"""
    if len(returns_series) == 0:
        return 0.0
    cum = (1 + returns_series).cumprod()
    peak = cum.expanding(min_periods=1).max()
    dd = (peak - cum) / peak
    return float(dd.max())


def build_returns_series(trades):
    """从交易列表构造日频收益率序列。"""
    if not trades:
        return pd.Series(dtype=float)

    daily_ret = {}
    for t in trades:
        if t["entry_gap"] > 0:
            ret = (t["exit_gap"] - t["entry_gap"]) / t["entry_gap"]
            # 将收益摊到持有天数上
            held = max(1, t["held_days"])
            daily = (1 + ret) ** (1 / held) - 1
            exit_date = t["exit_date"]
            daily_ret[exit_date] = daily_ret.get(exit_date, 0.0) + daily

    if not daily_ret:
        return pd.Series(dtype=float)

    s = pd.Series(daily_ret).sort_index()
    return s


# ── main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Regime-adaptive exit evaluator")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--regime-lookback", type=int, default=126)
    parser.add_argument("--high-vol-decay-factor", type=float, default=0.5)
    parser.add_argument("--low-vol-decay-factor", type=float, default=0.7)
    parser.add_argument("--initial-threshold-fraction", type=float, default=0.7)
    parser.add_argument("--min-hold-days", type=int, default=5)
    parser.add_argument("--effective-max-hold-days", type=int, default=45)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    data_root = Path(args.data_root)

    # ── 1. 加载数据 ──
    gap_path = data_root / "cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet"
    csi300_path = data_root / "cb_warehouse/csi300_daily.parquet"

    gap_df = pd.read_parquet(gap_path)
    csi300_df = pd.read_parquet(csi300_path)

    # ── 2. 建立市场状态分类器 ──
    regime_df = build_regime_labels(csi300_df, args.regime_lookback)

    # ── 3. 按时间段切片模拟 ──
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    # 训练集
    train_gap = gap_df[
        (pd.to_datetime(gap_df["trade_date"]) >= train_start)
        & (pd.to_datetime(gap_df["trade_date"]) <= train_end)
    ]
    train_trades = simulate_regime_adaptive_exit(
        train_gap, regime_df,
        args.initial_threshold_fraction,
        args.high_vol_decay_factor,
        args.low_vol_decay_factor,
        args.min_hold_days,
        args.effective_max_hold_days,
    )

    # 测试集
    test_gap = gap_df[
        (pd.to_datetime(gap_df["trade_date"]) >= test_start)
        & (pd.to_datetime(gap_df["trade_date"]) <= test_end)
    ]
    test_trades = simulate_regime_adaptive_exit(
        test_gap, regime_df,
        args.initial_threshold_fraction,
        args.high_vol_decay_factor,
        args.low_vol_decay_factor,
        args.min_hold_days,
        args.effective_max_hold_days,
    )

    # ── 4. 计算指标 ──
    train_strat, train_bench, train_excess = compute_excess_compound(
        train_trades, csi300_df, args.train_start, args.train_end
    )
    test_strat, test_bench, test_excess = compute_excess_compound(
        test_trades, csi300_df, args.test_start, args.test_end
    )

    # 各年指标
    train_years = range(int(args.train_start[:4]), int(args.train_end[:4]) + 1)
    test_years = range(int(args.test_start[:4]), int(args.test_end[:4]) + 1)

    year_metrics = {}
    for y in list(train_years) + list(test_years):
        year_trades = [t for t in train_trades + test_trades
                       if str(t["exit_date"].year) == str(y)]
        year_strat, year_bench, year_excess = compute_excess_compound(
            year_trades, csi300_df, f"{y}-01-01", f"{y}-12-31"
        )
        year_sharpe = compute_sharpe(year_trades, csi300_df, y)
        year_metrics[str(y)] = {
            "strategy_return": year_strat,
            "benchmark_return": year_bench,
            "excess_return": year_excess,
            "sharpe": year_sharpe,
            "trade_count": len(year_trades),
        }

    # 最大回撤（用收益率序列近似）
    all_returns = build_returns_series(train_trades + test_trades)
    train_returns = all_returns.loc[args.train_start:args.train_end]
    test_returns = all_returns.loc[args.test_start:args.test_end] if len(all_returns) > 0 else pd.Series(dtype=float)

    train_max_dd = compute_max_drawdown(train_returns) if len(train_returns) > 0 else 0.0
    test_max_dd = compute_max_drawdown(test_returns) if len(test_returns) > 0 else 0.0

    # 最大年回撤（测试集各年）
    max_test_year_dd = 0.0
    for y in test_years:
        y_ret = all_returns.loc[f"{y}-01-01":f"{y}-12-31"] if len(all_returns) > 0 else pd.Series(dtype=float)
        if len(y_ret) > 0:
            dd = compute_max_drawdown(y_ret)
            max_test_year_dd = max(max_test_year_dd, dd)

    # ── 5. 基线对比（静态 duration-adaptive exit）──
    # 用 low_vol = high_vol = decay 构建具有单一衰减因子的退化基线
    static_decay = 0.5  # accepted best decay_period_factor
    train_trades_static = simulate_regime_adaptive_exit(
        train_gap, regime_df,
        args.initial_threshold_fraction,
        static_decay,  # 高波和低波用相同衰减
        static_decay,
        args.min_hold_days,
        args.effective_max_hold_days,
    )
    test_trades_static = simulate_regime_adaptive_exit(
        test_gap, regime_df,
        args.initial_threshold_fraction,
        static_decay,
        static_decay,
        args.min_hold_days,
        args.effective_max_hold_days,
    )

    _, _, train_excess_static = compute_excess_compound(
        train_trades_static, csi300_df, args.train_start, args.train_end
    )
    _, _, test_excess_static = compute_excess_compound(
        test_trades_static, csi300_df, args.test_start, args.test_end
    )

    # ── 6. 成功条件判断 ──
    adoption_criteria = {
        "cumulative_excess_compound_test_set_gt_0": test_excess > 0,
        "cumulative_excess_compound_improvement_vs_baseline_gt_2pp": (test_excess - test_excess_static) > 0.02,
        "max_drawdown_vs_benchmark_any_year_le_15pct": max_test_year_dd <= 0.15,
        "any_holdout_year_sharpe_ge_0_5": any(
            year_metrics[str(y)].get("sharpe", 0) >= 0.5 for y in test_years
        ),
    }
    adoption_pass = all(adoption_criteria.values())

    # ── 7. 输出文件 ──

    # summary.json
    summary = {
        "adoption_pass": bool(adoption_pass),
        "adoption_criteria": {k: bool(v) for k, v in adoption_criteria.items()},
        "regime_adaptive": {
            "train_excess_return": train_excess,
            "test_excess_return": test_excess,
            "train_max_drawdown": train_max_dd,
            "test_max_drawdown": test_max_dd,
            "max_test_year_drawdown": max_test_year_dd,
            "trade_count_train": len(train_trades),
            "trade_count_test": len(test_trades),
        },
        "static_baseline": {
            "train_excess_return": train_excess_static,
            "test_excess_return": test_excess_static,
        },
        "year_metrics": year_metrics,
        "params": {
            "regime_lookback": args.regime_lookback,
            "high_vol_decay_factor": args.high_vol_decay_factor,
            "low_vol_decay_factor": args.low_vol_decay_factor,
            "initial_threshold_fraction": args.initial_threshold_fraction,
            "min_hold_days": args.min_hold_days,
            "effective_max_hold_days": args.effective_max_hold_days,
        },
    }
    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    # report.yaml
    report = {
        "train_excess_return_regime_adaptive": train_excess,
        "test_excess_return_regime_adaptive": test_excess,
        "train_excess_return_static_baseline": train_excess_static,
        "test_excess_return_static_baseline": test_excess_static,
        "excess_improvement_test": test_excess - test_excess_static,
        "train_max_drawdown": train_max_dd,
        "test_max_drawdown": test_max_dd,
        "max_test_year_drawdown": max_test_year_dd,
        "train_trades": len(train_trades),
        "test_trades": len(test_trades),
        "adoption_pass": adoption_pass,
    }
    with open(output_dir / "report.yaml", "w") as f:
        yaml.dump(report, f, default_flow_style=False, allow_unicode=True)

    # l4_ack.yaml
    l4_ack = {
        "acknowledged": True,
        "status": "completed",
        "adoption_pass": adoption_pass,
        "reason": (
            "Regime-adaptive exit evaluated with "
            f"high_vol_decay={args.high_vol_decay_factor}, "
            f"low_vol_decay={args.low_vol_decay_factor}, "
            f"lookback={args.regime_lookback}"
        ),
    }
    with open(output_dir / "l4_ack.yaml", "w") as f:
        yaml.dump(l4_ack, f, default_flow_style=False, allow_unicode=True)

    # diagnostic.yaml
    diagnostic = {
        "status": "completed",
        "data_ranges": {
            "train": [args.train_start, args.train_end],
            "test": [args.test_start, args.test_end],
        },
        "data_files_used": {
            "daily_value_gap_amounts": str(gap_path),
            "csi300_daily": str(csi300_path),
        },
        "regime_stats": {
            "train_period": {
                "high_vol_days": int((regime_df["regime"] == "high_vol").sum()),
                "low_vol_days": int((regime_df["regime"] == "low_vol").sum()),
            },
        },
        "trade_summary": {
            "train_trade_count": len(train_trades),
            "test_trade_count": len(test_trades),
            "avg_hold_days_train": (
                float(np.mean([t["held_days"] for t in train_trades]))
                if train_trades else 0.0
            ),
            "avg_hold_days_test": (
                float(np.mean([t["held_days"] for t in test_trades]))
                if test_trades else 0.0
            ),
        },
        "exit_reason_breakdown": {
            "train": {
                "threshold_breach": sum(1 for t in train_trades if t.get("exit_reason") == "threshold_breach"),
                "force_max_hold": sum(1 for t in train_trades if t.get("exit_reason") == "force_max_hold"),
                "dropped_out": sum(1 for t in train_trades if t.get("exit_reason") == "dropped_out"),
                "end_of_period": sum(1 for t in train_trades if t.get("exit_reason") == "end_of_period"),
            },
            "test": {
                "threshold_breach": sum(1 for t in test_trades if t.get("exit_reason") == "threshold_breach"),
                "force_max_hold": sum(1 for t in test_trades if t.get("exit_reason") == "force_max_hold"),
                "dropped_out": sum(1 for t in test_trades if t.get("exit_reason") == "dropped_out"),
                "end_of_period": sum(1 for t in test_trades if t.get("exit_reason") == "end_of_period"),
            },
        },
    }
    with open(output_dir / "diagnostic.yaml", "w") as f:
        yaml.dump(diagnostic, f, default_flow_style=False, allow_unicode=True)

    print(f"Regime-adaptive exit evaluation complete.")
    print(f"  Train excess: {train_excess:.4f} (static baseline: {train_excess_static:.4f})")
    print(f"  Test excess:  {test_excess:.4f} (static baseline: {test_excess_static:.4f})")
    print(f"  Adoption pass: {adoption_pass}")
    print(f"  Output written to: {output_dir}")


if __name__ == "__main__":
    main()
