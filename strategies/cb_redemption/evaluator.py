"""Strategy evaluation helpers.

This module is the first slice of the shared evaluation framework. It turns
daily strategy returns and benchmark returns into comparable cumulative curves,
metrics, threshold checks, and a coarse deployment tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import math

import numpy as np
import pandas as pd


Tier = Literal["明确好", "值得真上", "底线档", "不上"]


DEFAULT_BENCHMARKS = ("cash", "cb_equal", "csi300", "dividend", "sixty_forty")


@dataclass(frozen=True)
class EvaluationConfig:
    """Thresholds and benchmark names for strategy evaluation."""

    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS
    cash_name: str = "cash"
    primary_benchmark: str = "cb_equal"
    stretch_benchmark: str = "dividend"
    max_drawdown_ratio_limit: float = 1.2
    yearly_win_rate_min: float = 0.60
    information_ratio_good: float = 0.50
    trading_days_per_year: int = 250


@dataclass
class EvaluationResult:
    """Full evaluation output for one strategy against configured benchmarks."""

    cumulative_curves: pd.DataFrame
    metrics_table: pd.DataFrame
    yearly_consistency: pd.DataFrame
    thresholds: dict[str, bool]
    tier: Tier
    reasons: list[str] = field(default_factory=list)


def evaluate(
    strategy_returns: pd.Series,
    benchmarks: dict[str, pd.Series],
    config: EvaluationConfig | None = None,
) -> EvaluationResult:
    """Evaluate a strategy against benchmark daily returns.

    Inputs are daily decimal returns, indexed by date-like values. The evaluator
    aligns all series by date intersection and starts every cumulative curve at
    ``1.0`` on the first aligned date.
    """

    cfg = config or EvaluationConfig()
    returns = _aligned_returns(strategy_returns, benchmarks, cfg)
    cumulative = _cumulative_curves(returns)
    metrics = _metrics_table(returns, cumulative, cfg)
    yearly = _yearly_consistency(returns, cfg)
    thresholds = _thresholds(metrics, yearly, cfg)
    tier = determine_tier(thresholds)
    reasons = _reasons(thresholds, tier)
    return EvaluationResult(
        cumulative_curves=cumulative,
        metrics_table=metrics,
        yearly_consistency=yearly,
        thresholds=thresholds,
        tier=tier,
        reasons=reasons,
    )


def determine_tier(thresholds_passed: dict[str, bool]) -> Tier:
    """Map threshold booleans to the four RFC tiers."""

    baseline = (
        thresholds_passed.get("beats_cash", False)
        and thresholds_passed.get("beats_primary", False)
        and thresholds_passed.get("drawdown_ok", False)
        and thresholds_passed.get("yearly_consistency_ok", False)
    )
    if not baseline:
        return "不上"

    stretch = thresholds_passed.get("beats_stretch", False)
    good = (
        stretch
        and thresholds_passed.get("information_ratio_ok", False)
        and thresholds_passed.get("no_year_worse_than_primary", False)
    )
    if good:
        return "明确好"
    if stretch:
        return "值得真上"
    return "底线档"


def _aligned_returns(
    strategy_returns: pd.Series,
    benchmarks: dict[str, pd.Series],
    cfg: EvaluationConfig,
) -> pd.DataFrame:
    series: dict[str, pd.Series] = {"strategy": _clean_series(strategy_returns)}
    for name in cfg.benchmarks:
        if name not in benchmarks:
            raise ValueError(f"missing benchmark returns: {name}")
        series[name] = _clean_series(benchmarks[name])

    df = pd.DataFrame(series).dropna(how="any").sort_index()
    if df.empty:
        raise ValueError("no overlapping return dates for strategy and benchmarks")
    return df.astype(float)


def _clean_series(s: pd.Series) -> pd.Series:
    if not isinstance(s, pd.Series):
        raise TypeError("returns must be pandas Series")
    cleaned = s.copy()
    cleaned.index = pd.to_datetime(cleaned.index)
    cleaned = pd.to_numeric(cleaned, errors="coerce")
    return cleaned[~cleaned.index.duplicated(keep="last")].sort_index()


def _cumulative_curves(returns: pd.DataFrame) -> pd.DataFrame:
    curves = (1.0 + returns).cumprod()
    if not curves.empty:
        curves.iloc[0] = 1.0
    return curves


def _metrics_table(
    returns: pd.DataFrame, cumulative: pd.DataFrame, cfg: EvaluationConfig
) -> pd.DataFrame:
    rows: dict[str, dict[str, float]] = {}
    strategy = returns["strategy"]
    for name in returns.columns:
        r = returns[name]
        curve = cumulative[name]
        total_return = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
        ann_return = _annualized_return(total_return, len(r), cfg.trading_days_per_year)
        max_dd = _max_drawdown(curve)
        vol = float(r.std(ddof=0) * math.sqrt(cfg.trading_days_per_year))
        sharpe = ann_return / vol if vol > 0 else 0.0
        if name == "strategy":
            information_ratio = 0.0
        else:
            active = strategy - r
            tracking_error = float(
                active.std(ddof=0) * math.sqrt(cfg.trading_days_per_year)
            )
            information_ratio = (
                (ann_return - rows["strategy"]["annualized_return"]) / tracking_error
                if tracking_error > 0 and "strategy" in rows
                else 0.0
            )
        rows[name] = {
            "total_return": total_return,
            "annualized_return": ann_return,
            "max_drawdown": max_dd,
            "volatility": vol,
            "sharpe": sharpe,
            "information_ratio": information_ratio,
        }

    # Strategy information ratio is measured against the primary benchmark.
    if cfg.primary_benchmark in returns:
        active = returns["strategy"] - returns[cfg.primary_benchmark]
        tracking_error = float(active.std(ddof=0) * math.sqrt(cfg.trading_days_per_year))
        rows["strategy"]["information_ratio"] = (
            (
                rows["strategy"]["annualized_return"]
                - rows[cfg.primary_benchmark]["annualized_return"]
            )
            / tracking_error
            if tracking_error > 0
            else 0.0
        )
    return pd.DataFrame.from_dict(rows, orient="index")


def _annualized_return(total_return: float, n_days: int, trading_days: int) -> float:
    if n_days <= 0 or total_return <= -1.0:
        return 0.0
    return float((1.0 + total_return) ** (trading_days / n_days) - 1.0)


def _max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    peak = curve.cummax()
    dd = curve / peak - 1.0
    return float(dd.min())


def _yearly_consistency(returns: pd.DataFrame, cfg: EvaluationConfig) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for year, group in returns.groupby(returns.index.year):
        if group.empty:
            continue
        strategy_ret = float((1.0 + group["strategy"]).prod() - 1.0)
        row: dict[str, object] = {"year": int(year), "strategy_return": strategy_ret}
        for name in cfg.benchmarks:
            bench_ret = float((1.0 + group[name]).prod() - 1.0)
            row[f"{name}_return"] = bench_ret
            row[f"beats_{name}"] = strategy_ret > bench_ret
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("year")


def _thresholds(
    metrics: pd.DataFrame, yearly: pd.DataFrame, cfg: EvaluationConfig
) -> dict[str, bool]:
    strategy_total = float(metrics.loc["strategy", "total_return"])
    primary_total = float(metrics.loc[cfg.primary_benchmark, "total_return"])
    cash_total = float(metrics.loc[cfg.cash_name, "total_return"])
    stretch_total = float(metrics.loc[cfg.stretch_benchmark, "total_return"])

    primary_dd = abs(float(metrics.loc[cfg.primary_benchmark, "max_drawdown"]))
    strategy_dd = abs(float(metrics.loc["strategy", "max_drawdown"]))
    if primary_dd == 0.0:
        drawdown_ok = strategy_dd == 0.0
    else:
        drawdown_ok = strategy_dd <= primary_dd * cfg.max_drawdown_ratio_limit

    primary_col = f"beats_{cfg.primary_benchmark}"
    yearly_rate = float(yearly[primary_col].mean()) if primary_col in yearly else 0.0
    no_year_worse = bool(yearly[primary_col].all()) if primary_col in yearly else False

    return {
        "beats_cash": strategy_total > cash_total,
        "beats_primary": strategy_total > primary_total,
        "drawdown_ok": drawdown_ok,
        "yearly_consistency_ok": yearly_rate >= cfg.yearly_win_rate_min,
        "beats_stretch": strategy_total > stretch_total,
        "information_ratio_ok": (
            float(metrics.loc["strategy", "information_ratio"])
            >= cfg.information_ratio_good
        ),
        "no_year_worse_than_primary": no_year_worse,
    }


def _reasons(thresholds: dict[str, bool], tier: Tier) -> list[str]:
    labels = {
        "beats_cash": "跑赢现金",
        "beats_primary": "跑赢主基准",
        "drawdown_ok": "回撤约束",
        "yearly_consistency_ok": "跨年一致性",
        "beats_stretch": "跑赢高门槛基准",
        "information_ratio_ok": "信息比率达标",
        "no_year_worse_than_primary": "完整年份均不弱于主基准",
    }
    failed = [label for key, label in labels.items() if not thresholds.get(key, False)]
    if not failed:
        return [f"tier={tier}; all thresholds passed"]
    return [f"tier={tier}; failed: {', '.join(failed)}"]


__all__ = [
    "DEFAULT_BENCHMARKS",
    "EvaluationConfig",
    "EvaluationResult",
    "Tier",
    "determine_tier",
    "evaluate",
]
