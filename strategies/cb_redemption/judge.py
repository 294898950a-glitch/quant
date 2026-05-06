"""分析师 (Judge) — 单点诊断器

输入：一次 BacktestResult（含 is_metrics / oos_metrics / trades / date_range / all_metrics）
输出：结构化 Diagnosis（描述事实，不出建议、不接 LLM）

视角：单点。它只看这一次回测；"是否在挖数据" 是审计员的职责，分析师不管。

严格不允许：
  - 调用任何 LLM
  - 输出建议（不要写"建议你去掉 w_premium_ratio"这种话）
  - 读 strong_timeline_snapshots.parquet 或任何外部文件

设计：
  - by_quarter / by_year: 从 result.trades 按 entry_date 分组聚合
  - factor_contributions: 简化为 |weight| 排序（深入的 SHAP 这种 P2 再说）
  - weak_factors: 阈值 |w| < 0.1 算弱
  - drawdown: 从交易序列累计 PnL 算 equity 曲线，再算回撤
  - weakness_text: 模板文字，不调 LLM
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from strategies.cb_redemption.backtest import BacktestResult, TradeRecord


WEAK_FACTOR_THRESHOLD = 0.1
DRAWDOWN_PERIOD_THRESHOLD = 5.0  # 单段回撤超 5% 计一次


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _to_quarter(date_str: str) -> str:
    """'20250115' or '2025-01-15' -> '2025Q1'."""
    s = (date_str or "").replace("-", "")
    if len(s) < 6:
        return "unknown"
    year = s[:4]
    try:
        month = int(s[4:6])
    except ValueError:
        return "unknown"
    q = (month - 1) // 3 + 1
    return f"{year}Q{q}"


def _to_year(date_str: str) -> str:
    s = (date_str or "").replace("-", "")
    return s[:4] if len(s) >= 4 else "unknown"


def _aggregate_by_key(trades: list, key_fn) -> list[dict]:
    """按 key_fn(trade) 分组聚合 trades，返回排序后的 list[dict]。"""
    buckets: dict[str, list] = {}
    for t in trades:
        k = key_fn(t)
        buckets.setdefault(k, []).append(t)

    rows: list[dict] = []
    for k in sorted(buckets.keys()):
        ts = buckets[k]
        n = len(ts)
        wins = sum(1 for t in ts if t.pnl_pct > 0)
        avg_ret = sum(t.pnl_pct for t in ts) / n if n else 0.0
        rows.append(
            {
                "period": k,
                "n_trades": n,
                "winrate": round(wins / n * 100, 1) if n else 0.0,
                "avg_return": round(avg_ret, 2),
            }
        )
    return rows


def _equity_curve_from_trades(trades: list) -> list[float]:
    """按 exit_date 排序累加 pnl_amount，得到 equity 曲线（起点 0）。"""
    if not trades:
        return []
    sorted_trades = sorted(trades, key=lambda t: (t.exit_date, t.entry_date))
    equity = 0.0
    curve: list[float] = [0.0]
    for t in sorted_trades:
        equity += float(t.pnl_amount)
        curve.append(equity)
    return curve


def _drawdown_stats(equity: list[float]) -> tuple[float, int]:
    """返回 (max_drawdown_pct, drawdown_periods_count)。

    max_drawdown 定义：相对历史峰值的最大跌幅（百分比，正数表示跌幅大小）。
    drawdown_periods：从峰值开始连续回撤、单段最深 >= DRAWDOWN_PERIOD_THRESHOLD% 的次数。

    若 equity 全为 0 或长度不足 → (0.0, 0)。
    """
    if len(equity) < 2:
        return 0.0, 0

    peak = equity[0]
    max_dd_pct = 0.0
    period_count = 0
    in_drawdown = False
    segment_min_dd = 0.0  # 当前段最深回撤

    for v in equity[1:]:
        if v >= peak:
            # 新高 → 结算上一段
            if in_drawdown and segment_min_dd >= DRAWDOWN_PERIOD_THRESHOLD:
                period_count += 1
            in_drawdown = False
            segment_min_dd = 0.0
            peak = v
            continue

        # 在回撤中
        denom = abs(peak) if abs(peak) > 1e-9 else 1.0
        dd_pct = (peak - v) / denom * 100.0
        in_drawdown = True
        if dd_pct > segment_min_dd:
            segment_min_dd = dd_pct
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    # 收尾：最后一段如果仍在回撤且足够深
    if in_drawdown and segment_min_dd >= DRAWDOWN_PERIOD_THRESHOLD:
        period_count += 1

    return round(max_dd_pct, 2), period_count


def _factor_contributions(
    weights: list[float], factor_names: list[str]
) -> list[dict]:
    """按 |weight| 降序排序，每条带 abs_weight_rank。"""
    pairs = list(zip(factor_names, weights))
    # 按 |w| 降序
    ranked = sorted(enumerate(pairs), key=lambda iw: -abs(iw[1][1]))
    out: list[dict] = []
    for rank, (_, (name, w)) in enumerate(ranked, start=1):
        out.append(
            {
                "name": name,
                "weight": round(float(w), 4),
                "abs_weight": round(abs(float(w)), 4),
                "abs_weight_rank": rank,
            }
        )
    # 恢复成原顺序输出（保持稳定），但 rank 已含序信息
    out.sort(key=lambda r: factor_names.index(r["name"]))
    return out


def _weak_factors(weights: list[float], factor_names: list[str]) -> list[str]:
    return [
        n for n, w in zip(factor_names, weights) if abs(float(w)) < WEAK_FACTOR_THRESHOLD
    ]


def _worst_period(rows: list[dict]) -> dict | None:
    """返回 avg_return 最低的 period 行（n_trades>=3 才考虑）。"""
    cand = [r for r in rows if r["n_trades"] >= 3]
    if not cand:
        return None
    return min(cand, key=lambda r: r["avg_return"])


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #


@dataclass
class Diagnosis:
    """单次回测的结构化诊断结果。

    字段：
      is_oos_gap_sharpe:    is_sharpe - oos_sharpe (>0 表示 IS 强 OOS 弱)
      is_oos_gap_winrate:   is_winrate - oos_winrate (百分点)
      by_quarter:           [{period, n_trades, winrate, avg_return}, ...]
      by_year:              同上按 year
      factor_contributions: [{name, weight, abs_weight, abs_weight_rank}, ...]
      weak_factors:         |w| < 0.1 的因子名
      drawdown_max:         最大回撤百分比（基于交易序列累计 PnL）
      drawdown_periods:     单段回撤超 5% 的次数
      weakness_text:        一段中文人话总结，非空
    """

    is_oos_gap_sharpe: float
    is_oos_gap_winrate: float
    by_quarter: list[dict] = field(default_factory=list)
    by_year: list[dict] = field(default_factory=list)
    factor_contributions: list[dict] = field(default_factory=list)
    weak_factors: list[str] = field(default_factory=list)
    drawdown_max: float = 0.0
    drawdown_periods: int = 0
    weakness_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # --------------------------------------------------------------------- #
    # 主入口
    # --------------------------------------------------------------------- #

    @classmethod
    def from_backtest(
        cls,
        result: "BacktestResult",
        weights: list[float],
        factor_names: list[str],
    ) -> "Diagnosis":
        if len(weights) != len(factor_names):
            raise ValueError(
                f"weights ({len(weights)}) 与 factor_names ({len(factor_names)}) 长度不一致"
            )

        is_m = result.is_metrics or {}
        oos_m = result.oos_metrics or {}

        is_sharpe = float(is_m.get("sharpe", 0.0) or 0.0)
        oos_sharpe = float(oos_m.get("sharpe", 0.0) or 0.0)
        is_winrate = float(is_m.get("win_rate", 0.0) or 0.0)
        oos_winrate = float(oos_m.get("win_rate", 0.0) or 0.0)

        gap_sharpe = round(is_sharpe - oos_sharpe, 3)
        gap_winrate = round(is_winrate - oos_winrate, 2)

        # 季度/年度聚合
        trades = list(getattr(result, "trades", []) or [])
        by_quarter = _aggregate_by_key(trades, lambda t: _to_quarter(t.entry_date))
        by_year = _aggregate_by_key(trades, lambda t: _to_year(t.entry_date))

        # 因子贡献
        contribs = _factor_contributions(weights, factor_names)
        weak = _weak_factors(weights, factor_names)

        # 回撤（BacktestResult 当前不带 equity 序列 → 基于 trades 的累计 PnL 估算）
        equity = _equity_curve_from_trades(trades)
        if equity:
            dd_max, dd_periods = _drawdown_stats(equity)
            equity_note = ""
        else:
            dd_max, dd_periods = 0.0, 0
            equity_note = "（无 equity 序列：trades 为空，回撤指标降级为 0）"

        # 文本总结
        weakness_text = _build_weakness_text(
            gap_sharpe=gap_sharpe,
            gap_winrate=gap_winrate,
            is_metrics=is_m,
            oos_metrics=oos_m,
            by_quarter=by_quarter,
            by_year=by_year,
            weak_factors=weak,
            dd_max=dd_max,
            dd_periods=dd_periods,
            date_range=getattr(result, "date_range", ("", "")),
            equity_note=equity_note,
            n_trades_total=len(trades),
        )

        return cls(
            is_oos_gap_sharpe=gap_sharpe,
            is_oos_gap_winrate=gap_winrate,
            by_quarter=by_quarter,
            by_year=by_year,
            factor_contributions=contribs,
            weak_factors=weak,
            drawdown_max=dd_max,
            drawdown_periods=dd_periods,
            weakness_text=weakness_text,
        )


# --------------------------------------------------------------------------- #
# 函数式入口
# --------------------------------------------------------------------------- #


def diagnose(
    result: "BacktestResult",
    weights: list[float],
    factor_names: list[str],
) -> Diagnosis:
    """与 ``Diagnosis.from_backtest`` 等价的函数式入口。"""
    return Diagnosis.from_backtest(result, weights, factor_names)


# --------------------------------------------------------------------------- #
# 文本模板（不调 LLM）
# --------------------------------------------------------------------------- #


def _build_weakness_text(
    *,
    gap_sharpe: float,
    gap_winrate: float,
    is_metrics: dict,
    oos_metrics: dict,
    by_quarter: list[dict],
    by_year: list[dict],
    weak_factors: list[str],
    dd_max: float,
    dd_periods: int,
    date_range: tuple,
    equity_note: str,
    n_trades_total: int,
) -> str:
    """拼装一段中文事实总结。不出建议。"""
    parts: list[str] = []

    # 区间 / 总量
    start, end = (date_range or ("", ""))
    if start or end:
        parts.append(f"回测区间 {start}~{end}，共 {n_trades_total} 笔交易。")
    else:
        parts.append(f"共 {n_trades_total} 笔交易。")

    # IS / OOS 差距
    is_n = int(is_metrics.get("total_trades", 0) or 0)
    oos_n = int(oos_metrics.get("total_trades", 0) or 0)
    is_sharpe = float(is_metrics.get("sharpe", 0.0) or 0.0)
    oos_sharpe = float(oos_metrics.get("sharpe", 0.0) or 0.0)

    if is_n == 0 and oos_n == 0:
        parts.append("IS/OOS 区间内均无交易。")
    else:
        parts.append(
            f"IS sharpe={is_sharpe:.2f} (n={is_n})，"
            f"OOS sharpe={oos_sharpe:.2f} (n={oos_n})；"
            f"sharpe 差 {gap_sharpe:+.2f}，winrate 差 {gap_winrate:+.1f} 个百分点。"
        )

    # 最差季度（基于 by_quarter，n>=3 的最差段）
    worst_q = _worst_period(by_quarter)
    if worst_q is not None:
        parts.append(
            f"最差季度 {worst_q['period']}："
            f"{worst_q['n_trades']} 笔，胜率 {worst_q['winrate']}%，"
            f"均收益 {worst_q['avg_return']:+.2f}%。"
        )

    # 最差年度
    worst_y = _worst_period(by_year)
    if worst_y is not None and (worst_q is None or worst_y["period"] != worst_q["period"][:4]):
        parts.append(
            f"最差年份 {worst_y['period']}："
            f"{worst_y['n_trades']} 笔，胜率 {worst_y['winrate']}%，"
            f"均收益 {worst_y['avg_return']:+.2f}%。"
        )

    # 弱因子
    if weak_factors:
        parts.append(
            f"权重 |w|<{WEAK_FACTOR_THRESHOLD} 的弱因子：{', '.join(weak_factors)}（贡献近零）。"
        )
    else:
        parts.append(f"无弱因子（所有 |w|≥{WEAK_FACTOR_THRESHOLD}）。")

    # 回撤
    if equity_note:
        parts.append(equity_note)
    else:
        parts.append(
            f"累计 PnL 曲线最大回撤 {dd_max:.2f}%，"
            f"单段超 {DRAWDOWN_PERIOD_THRESHOLD:.0f}% 的回撤共 {dd_periods} 次。"
        )

    return " ".join(parts)
