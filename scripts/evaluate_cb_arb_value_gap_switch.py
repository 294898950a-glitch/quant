"""Evaluate cb_arb using absolute value-gap ranking and switch hurdles.

The default cb_arb verifier ranks by percentage deviation. This script keeps
the same valuation inputs, filters, market regimes, and position sizing, but
ranks buy/switch candidates by absolute tradable value gap:

    (theoretical_price - market_price) * buy_quantity

It is an evaluation harness only; it does not replace the default strategy.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.analyze_cb_arb_repair_times import (  # noqa: E402
    _add_value_gap_amounts,
    _compute_daily_ranks,
)
from scripts.evaluate_cb_arb_daily_regime_switch import _build_daily_features  # noqa: E402
from scripts.search_cb_arb_time_split_grid import _base_configs  # noqa: E402
from strategies.cb_arb.verifier import (  # noqa: E402
    apply_cost_model,
    _build_call_index,
    _index_total_return,
    _is_force_redeemed_on_date,
    _load_cb_call,
    _load_cb_daily,
    _load_cb_basic,
    _load_stk_daily,
    _load_trading_days,
)


@dataclass
class Position:
    ts_code: str
    name: str
    entry_date: str
    entry_price: float
    qty: float
    cost: float
    entry_gap_amount: float
    entry_gap_pct: float
    entry_gap_source: str = "unknown"
    entry_position_cash_scale: float = 1.0


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _load_or_build_value_ranks(
    data_root: Path,
    start: str,
    end: str,
    fixed_source: int,
    rule: str,
    ranks_path: Path,
    reuse_ranks: bool,
    config_overrides: dict[str, float] | None = None,
) -> pd.DataFrame:
    if reuse_ranks and ranks_path.exists():
        ranks = pd.read_parquet(ranks_path)
    else:
        ranks = _compute_daily_ranks(
            data_root,
            start,
            end,
            fixed_source,
            rule,
            config_overrides=config_overrides,
        )
        ranks = _add_value_gap_amounts(ranks, data_root, fixed_source)
        ranks_path.parent.mkdir(parents=True, exist_ok=True)
        ranks.to_parquet(ranks_path, index=False)
    ranks = ranks.copy()
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    ranks["ts_code"] = ranks["ts_code"].astype(str)
    return ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)].copy()


def _equity(cash: float, holdings: dict[str, Position], close_map: dict[str, float]) -> float:
    eq = cash
    for ts, pos in holdings.items():
        px = close_map.get(ts, pos.entry_price)
        eq += pos.qty * px
    return float(eq)


def _metrics(
    equity_curve: list[tuple[str, float]],
    trades: list[dict[str, Any]],
    base_capital: float,
) -> dict[str, Any]:
    if not equity_curve:
        return {
            "total_return": 0.0,
            "excess_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "n_days": 0,
        }
    vals = [float(v) for _, v in equity_curve]
    total_return = vals[-1] / base_capital - 1.0 if base_capital > 0 else 0.0
    peak = vals[0]
    max_dd = 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)
    wins = sum(1 for t in trades if float(t["pnl_pct"]) > 0)
    win_rate = wins / len(trades) if trades else 0.0
    benchmark = _index_total_return(equity_curve[0][0], equity_curve[-1][0])
    return {
        "total_return": round(total_return, 6),
        "excess_return": round(total_return - benchmark, 6),
        "max_drawdown": round(max_dd, 6),
        "win_rate": round(win_rate, 4),
        "total_trades": len(trades),
        "n_days": len(equity_curve),
    }


def _score(metrics: dict[str, Any]) -> float:
    excess = float(metrics.get("excess_return") or 0.0)
    dd = abs(float(metrics.get("max_drawdown") or 0.0))
    return round(excess - 0.25 * dd, 6)


def _candidate_grid() -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for min_gap_pct, sell_gap_pct, switch_hurdle_pct, max_hold, stop_gap_ratio_floor in product(
        [0.0, 0.005, 0.01, 0.03],
        [0.0, 0.005],
        [0.0, 0.01, 0.02, 0.03],
        [90, 180, 360],
        [0.0],
    ):
        rows.append(
            {
                "min_gap_pct": min_gap_pct,
                "sell_gap_pct": sell_gap_pct,
                "switch_hurdle_pct": switch_hurdle_pct,
                "max_hold_days": float(max_hold),
                "stop_gap_ratio_floor": stop_gap_ratio_floor,
            }
        )
    return rows


@lru_cache(maxsize=16)
def _build_timely_signal_maps(start: str, end: str) -> dict[str, dict[str, int]]:
    cb = _load_cb_daily().copy()
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb["amount"] = cb["close"].astype(float) * cb["vol"].astype(float)
    cb = cb[(cb["trade_date"] >= start) & (cb["trade_date"] <= end)].sort_values(
        ["ts_code", "trade_date"]
    )
    basic = _load_cb_basic()
    stock_by_cb = {
        row.ts_code: row.stk_code for row in basic[["ts_code", "stk_code"]].itertuples(index=False)
    }
    stk = _load_stk_daily().copy()
    stk["trade_date"] = stk["trade_date"].astype(str)
    stk = stk[(stk["trade_date"] >= start) & (stk["trade_date"] <= end)].sort_values(
        ["stk_code", "trade_date"]
    )

    stock_break: dict[tuple[str, str], bool] = {}
    stock_close: dict[tuple[str, str], float] = {}
    for stk_code, g in stk.groupby("stk_code"):
        g = g.reset_index(drop=True)
        closes = g["close"].astype(float)
        low60 = closes.shift(1).rolling(60, min_periods=20).min()
        for i, row in g.iterrows():
            date = str(row.trade_date)
            close = float(row.close)
            stock_close[(stk_code, date)] = close
            v = low60.iloc[i]
            stock_break[(stk_code, date)] = bool(pd.notna(v) and close < float(v))

    signal_count: dict[str, dict[str, int]] = {}
    for ts, g in cb.groupby("ts_code"):
        g = g.reset_index(drop=True)
        closes = g["close"].astype(float)
        amounts = g["amount"].astype(float)
        low60 = closes.shift(1).rolling(60, min_periods=20).min()
        avg20 = amounts.shift(1).rolling(20, min_periods=5).mean()
        stk_code = stock_by_cb.get(ts)
        signal_count[ts] = {}
        for i, row in g.iterrows():
            date = str(row.trade_date)
            close = float(row.close)
            count = 0
            v_low = low60.iloc[i]
            if pd.notna(v_low) and close < float(v_low):
                count += 1
            if stk_code and stock_break.get((stk_code, date), False):
                count += 1
            cb_down = False
            if i > 0 and float(g.loc[i - 1, "close"]) > 0:
                cb_down = close / float(g.loc[i - 1, "close"]) - 1.0 < 0
            v_amt = avg20.iloc[i]
            if pd.notna(v_amt) and float(v_amt) > 0 and float(row.amount) / float(v_amt) >= 2.0 and cb_down:
                count += 1
            if i >= 5 and stk_code:
                prev_cb = float(g.loc[i - 5, "close"])
                cur_stk = stock_close.get((stk_code, date))
                prev_date = str(g.loc[i - 5, "trade_date"])
                prev_stk = stock_close.get((stk_code, prev_date))
                if prev_cb > 0 and cur_stk is not None and prev_stk is not None and prev_stk > 0:
                    cb_ret = close / prev_cb - 1.0
                    stk_ret = cur_stk / prev_stk - 1.0
                    if cb_ret - stk_ret < -0.03:
                        count += 1
            signal_count[ts][date] = count
    return signal_count


@lru_cache(maxsize=16)
def _load_panic_signal_csv(path_raw: str) -> pd.DataFrame:
    path = Path(path_raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return pd.read_csv(path)


def _build_panic_dates(start: str, end: str, params: dict[str, Any]) -> set[str]:
    panic_dates_file = params.get("panic_dates_file")
    if panic_dates_file:
        path = Path(str(panic_dates_file))
        if not path.is_absolute():
            path = _REPO_ROOT / path
        signals = _load_panic_signal_csv(str(path)).copy()
        date_col = str(params.get("panic_date_column", "trade_date"))
        signal_col = str(params.get("panic_signal_column", "panic_day_trained"))
        if date_col not in signals.columns:
            raise ValueError(f"panic dates file missing date column: {date_col}")
        if signal_col in signals.columns:
            signals = signals[signals[signal_col].astype(bool)]
        signals[date_col] = signals[date_col].astype(str)
        if _has_market_filter(params):
            market_dates = _market_filter_dates_for_signal_dates(signals[date_col], params)
            market_ok = _market_filter_mask(market_dates, params)
            signals = signals[market_ok.to_numpy()]
        signal_dates = sorted(set(signals[date_col].tolist()))
        lag_days = int(params.get("panic_effective_lag_days", 1))
        trading_days = sorted(_load_cb_daily()["trade_date"].astype(str).unique().tolist())
        day_index = {d: i for i, d in enumerate(trading_days)}
        effective_dates: set[str] = set()
        for signal_date in signal_dates:
            idx = day_index.get(signal_date)
            if idx is None:
                continue
            effective_idx = idx + lag_days
            if 0 <= effective_idx < len(trading_days):
                effective_date = trading_days[effective_idx]
                if start <= effective_date <= end:
                    effective_dates.add(effective_date)
        return effective_dates

    cb = _load_cb_daily().copy()
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb = cb.sort_values(["ts_code", "trade_date"])
    cb["prev_close"] = cb.groupby("ts_code")["close"].shift(1)
    cb["ret20_bond"] = cb.groupby("ts_code")["close"].pct_change(20)
    cb["amount_proxy"] = cb["close"].astype(float) * cb["vol"].astype(float)
    cb["positive_20d"] = cb["ret20_bond"] > 0

    by = cb.groupby("trade_date").agg(
        index_level=("close", "mean"),
        amount=("amount_proxy", "sum"),
        breadth20=("positive_20d", "mean"),
    ).sort_index()
    by["day_ret"] = by["index_level"].pct_change()
    by["ret20"] = by["index_level"].pct_change(20)
    by["high120"] = by["index_level"].shift(1).rolling(120, min_periods=40).max()
    by["dd120"] = by["index_level"] / by["high120"] - 1.0
    by["amount_pctile252"] = by["amount"].rolling(252, min_periods=60).apply(
        lambda x: float((x <= x.iloc[-1]).mean()),
        raw=False,
    )

    mode = str(params.get("panic_rule", "agent_combo"))
    if mode == "breadth40":
        panic = by["breadth20"] <= float(params.get("panic_breadth20", 0.40))
    elif mode == "shock_only":
        panic = (
            (by["day_ret"] <= float(params.get("panic_day_ret", -0.018)))
            & (by["dd120"] <= float(params.get("panic_dd120", -0.05)))
            & (
                (by["ret20"] <= float(params.get("panic_ret20_shock", -0.03)))
                | (by["breadth20"] <= float(params.get("panic_breadth20_shock", 0.25)))
            )
        )
    else:
        trend = (
            (by["dd120"] <= float(params.get("panic_trend_dd120", -0.055)))
            & (by["ret20"] <= float(params.get("panic_trend_ret20", -0.05)))
            & (by["breadth20"] <= float(params.get("panic_trend_breadth20", 0.20)))
        )
        shock = (
            (by["day_ret"] <= float(params.get("panic_shock_day_ret", -0.018)))
            & (by["dd120"] <= float(params.get("panic_shock_dd120", -0.05)))
            & (
                (by["ret20"] <= float(params.get("panic_shock_ret20", -0.03)))
                | (by["breadth20"] <= float(params.get("panic_shock_breadth20", 0.25)))
            )
        )
        volume = (
            (by["day_ret"] <= float(params.get("panic_volume_day_ret", -0.012)))
            & (by["amount_pctile252"] >= float(params.get("panic_volume_amount_pctile", 0.85)))
            & (by["ret20"] <= float(params.get("panic_volume_ret20", -0.04)))
            & (by["breadth20"] <= float(params.get("panic_volume_breadth20", 0.25)))
        )
        panic = trend | shock | volume

    if _has_market_filter(params):
        market_ok = _market_filter_mask(pd.Series(by.index.astype(str), index=by.index), params)
        panic = panic & market_ok

    dates = [str(d) for d, v in panic.items() if bool(v) and start <= str(d) <= end]
    return set(dates)


@lru_cache(maxsize=16)
def _load_market_filter(path_raw: str, label: str) -> pd.Series:
    path = Path(path_raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    market = pd.read_parquet(path).copy()
    date_col = "trade_date" if "trade_date" in market.columns else "date"
    if date_col not in market.columns:
        raise ValueError(f"{label} market filter file missing date column: {path}")
    market = market.sort_values(date_col).reset_index(drop=True)
    if "pct_chg" in market.columns:
        pct_chg = market["pct_chg"].astype(float)
        if pct_chg.abs().max(skipna=True) > 1.0:
            pct_chg = pct_chg / 100.0
    elif "close" in market.columns:
        pct_chg = market["close"].astype(float).pct_change()
    else:
        raise ValueError(f"{label} market filter file missing pct_chg/close column: {path}")
    result = pd.Series(pct_chg.to_numpy(), index=market[date_col].astype(str))
    result = result[~result.index.duplicated(keep="last")].sort_index()
    return result.fillna(0.0)


def _has_market_filter(params: dict[str, Any]) -> bool:
    return (
        float(params.get("panic_market_filter_enabled", 0.0)) > 0
        or float(params.get("panic_market_filter_spy_enabled", 0.0)) > 0
    )


def _single_market_filter_mask(
    dates: pd.Series,
    path_raw: str,
    threshold: float,
    label: str,
) -> pd.Series:
    pct_chg = _load_market_filter(path_raw, label)
    return dates.astype(str).map(pct_chg).fillna(0.0).astype(float) <= threshold


def _market_filter_mask(dates: pd.Series, params: dict[str, Any]) -> pd.Series:
    masks: list[pd.Series] = []
    if float(params.get("panic_market_filter_enabled", 0.0)) > 0:
        csi_path = str(params.get("panic_market_filter_csi_path", "data/csi500_grid/raw/510500_daily.parquet"))
        csi_threshold = float(params.get("panic_market_filter_csi_threshold", -0.01))
        masks.append(_single_market_filter_mask(dates, csi_path, csi_threshold, "CSI"))
    if float(params.get("panic_market_filter_spy_enabled", 0.0)) > 0:
        spy_path = str(params.get("panic_market_filter_spy_path", "data/sp500_grid/raw/513500_daily.parquet"))
        spy_threshold = float(params.get("panic_market_filter_spy_threshold", -0.015))
        masks.append(_single_market_filter_mask(dates, spy_path, spy_threshold, "SPY"))
    if not masks:
        return pd.Series(True, index=dates.index)
    result = masks[0].copy()
    for mask in masks[1:]:
        result = result | mask
    return result


def _market_filter_dates_for_signal_dates(dates: pd.Series, params: dict[str, Any]) -> pd.Series:
    mode = str(params.get("panic_market_filter_csi_dating_mode", "raw")).lower()
    if mode == "raw":
        return dates.astype(str)
    if mode != "effective":
        raise ValueError(f"unsupported panic_market_filter_csi_dating_mode: {mode}")

    lag_days = int(params.get("panic_effective_lag_days", 1))
    trading_days = sorted(_load_cb_daily()["trade_date"].astype(str).unique().tolist())
    day_index = {d: i for i, d in enumerate(trading_days)}
    effective_dates: list[str] = []
    for signal_date in dates.astype(str):
        idx = day_index.get(signal_date)
        effective_idx = idx + lag_days if idx is not None else -1
        if 0 <= effective_idx < len(trading_days):
            effective_dates.append(trading_days[effective_idx])
        else:
            effective_dates.append("")
    return pd.Series(effective_dates, index=dates.index)


def _shift_signal_dates(signal_dates: list[str], start: str, end: str, lag_days: int) -> set[str]:
    trading_days = sorted(_load_cb_daily()["trade_date"].astype(str).unique().tolist())
    day_index = {d: i for i, d in enumerate(trading_days)}
    effective_dates: set[str] = set()
    for signal_date in signal_dates:
        idx = day_index.get(signal_date)
        if idx is None:
            continue
        effective_idx = idx + lag_days
        if 0 <= effective_idx < len(trading_days):
            effective_date = trading_days[effective_idx]
            if start <= effective_date <= end:
                effective_dates.add(effective_date)
    return effective_dates


def _build_panic_opportunity_dates(start: str, end: str, params: dict[str, Any]) -> set[str]:
    mode = str(params.get("panic_opportunity_trigger_mode", "panic"))
    if mode not in {"strong", "medium"}:
        return _build_panic_dates(start, end, params)

    panic_dates_file = params.get("panic_dates_file")
    if not panic_dates_file:
        return _build_panic_dates(start, end, params)
    path = Path(str(panic_dates_file))
    if not path.is_absolute():
        path = _REPO_ROOT / path
    signals = _load_panic_signal_csv(str(path)).copy()
    signals["trade_date"] = signals["trade_date"].astype(str)
    signals = signals.sort_values("trade_date").reset_index(drop=True)
    required = {"day_ret", "ret5", "ret20", "breadth1", "breadth20"}
    if not required <= set(signals.columns):
        return _build_panic_dates(start, end, params)

    day_ret = signals["day_ret"].astype(float)
    ret5 = signals["ret5"].astype(float)
    ret20 = signals["ret20"].astype(float)
    breadth1 = signals["breadth1"].astype(float)
    breadth20 = signals["breadth20"].astype(float)
    default_shock_day_ret = -0.016 if mode == "medium" else -0.018
    default_trend_breadth20 = 0.30 if mode == "medium" else 0.25
    hard_day = day_ret <= float(params.get("panic_opportunity_strong_day_ret", -0.03))
    shock_day = (
        (day_ret <= float(params.get("panic_opportunity_shock_day_ret", default_shock_day_ret)))
        & (breadth1 <= float(params.get("panic_opportunity_shock_breadth1", 0.20)))
    )
    trend_day = (
        (ret5 <= float(params.get("panic_opportunity_trend_ret5", -0.04)))
        & (ret20 <= float(params.get("panic_opportunity_trend_ret20", -0.04)))
        & (breadth20 <= float(params.get("panic_opportunity_trend_breadth20", default_trend_breadth20)))
    )
    mask = hard_day | shock_day | trend_day
    if float(params.get("panic_opportunity_realized_vol_filter_enabled", 0.0)) > 0:
        vol_window = int(params.get("panic_opportunity_realized_vol_window", 10))
        vol_threshold = float(params.get("panic_opportunity_realized_vol_threshold", 0.0))
        min_periods = max(2, min(vol_window, vol_window // 2))
        realized_vol = day_ret.rolling(vol_window, min_periods=min_periods).std()
        mask = mask & (realized_vol >= vol_threshold)
    if _has_market_filter(params):
        market_dates = _market_filter_dates_for_signal_dates(signals["trade_date"], params)
        mask = mask & _market_filter_mask(market_dates, params)
    signal_dates = sorted(set(signals.loc[mask, "trade_date"].tolist()))
    return _shift_signal_dates(
        signal_dates,
        start,
        end,
        int(params.get("panic_effective_lag_days", 1)),
    )


def _current_gap(row_by_ts: dict[str, Any], ts: str) -> tuple[float | None, float | None]:
    row = row_by_ts.get(ts)
    if row is None:
        return None, None
    return float(row.value_gap_amount), float(row.value_gap_pct_of_cash)


def _row_positive_gap(row_by_ts: dict[str, Any], ts: str, min_gap_pct: float = 0.0) -> bool:
    gap, gap_pct = _current_gap(row_by_ts, ts)
    return (
        gap is not None
        and gap_pct is not None
        and float(gap) > 0
        and float(gap_pct) >= min_gap_pct
    )


def _stressed_gap(
    row_by_ts: dict[str, Any],
    ts: str,
    params: dict[str, float],
    qty: float | None = None,
) -> tuple[float | None, float | None, str]:
    row = row_by_ts.get(ts)
    if row is None:
        return None, None, "missing"
    if not all(hasattr(row, key) for key in ("bond_floor", "option_value", "close")):
        return float(row.value_gap_amount), float(row.value_gap_pct_of_cash), "normal"

    close = float(row.close)
    bond_floor = float(row.bond_floor)
    option_value = float(row.option_value)
    theoretical = float(row.theoretical)
    base_gap = theoretical - close
    if base_gap <= 0:
        return float(row.value_gap_amount), float(row.value_gap_pct_of_cash), "not_undervalued"

    bond_gap = max(0.0, bond_floor - close)
    option_gap = max(0.0, theoretical - max(close, bond_floor))
    total_source = bond_gap + option_gap
    bond_share = bond_gap / total_source if total_source > 0 else 0.0
    option_share = option_gap / total_source if total_source > 0 else 0.0

    bond_cut = 0.0
    option_cut = 0.0
    source = "mixed"
    bond_threshold = float(params.get("source_bond_threshold", 0.60))
    option_threshold = float(params.get("source_option_threshold", 0.60))
    if bond_share >= bond_threshold:
        source = "bond"
        bond_cut = float(params.get("stress_bond_cut_bond", 0.0))
        option_cut = float(params.get("stress_option_cut_bond", 0.0))
    elif option_share >= option_threshold:
        source = "option"
        bond_cut = float(params.get("stress_bond_cut_option", 0.0))
        option_cut = float(params.get("stress_option_cut_option", 0.0))
    else:
        bond_cut = float(params.get("stress_bond_cut_mixed", 0.0))
        option_cut = float(params.get("stress_option_cut_mixed", 0.0))

    stressed_theoretical = bond_floor * (1.0 - bond_cut) + option_value * (1.0 - option_cut)
    quantity = float(qty) if qty is not None else float(getattr(row, "buy_qty", 0.0) or 0.0)
    if quantity <= 0:
        quantity = float(getattr(row, "buy_qty", 0.0) or 0.0)
    gap_per_bond = stressed_theoretical - close
    gap_amount = gap_per_bond * quantity
    position_cash = float(getattr(row, "position_cash", 0.0) or 0.0)
    gap_pct = gap_amount / position_cash if position_cash > 0 else float(row.value_gap_pct_of_cash)
    return float(gap_amount), float(gap_pct), source


def _gap_source(row_by_ts: dict[str, Any], ts: str, params: dict[str, float]) -> str:
    _, _, source = _stressed_gap(row_by_ts, ts, params)
    return source


def _gap_source_shares(row: Any, params: dict[str, float]) -> tuple[str, float, float]:
    if not all(hasattr(row, key) for key in ("bond_floor", "option_value", "close", "theoretical")):
        return "unknown", 0.0, 0.0
    close = float(row.close)
    bond_floor = float(row.bond_floor)
    option_value = float(row.option_value)
    theoretical = float(row.theoretical)
    if theoretical - close <= 0:
        return "not_undervalued", 0.0, 0.0
    bond_gap = max(0.0, bond_floor - close)
    option_gap = max(0.0, theoretical - max(close, bond_floor))
    total = bond_gap + option_gap
    if total <= 0:
        return "mixed", 0.0, 0.0
    bond_share = bond_gap / total
    option_share = option_gap / total
    if bond_share >= float(params.get("source_bond_threshold", 0.60)):
        return "bond", bond_share, option_share
    if option_share >= float(params.get("source_option_threshold", 0.60)):
        return "option", bond_share, option_share
    return "mixed", bond_share, option_share


def _option_source_pnl_feedback_state(
    trades: list[dict[str, Any]],
    date: str,
    params: dict[str, float],
) -> dict[str, Any]:
    if float(params.get("option_source_pnl_feedback_enabled", 0.0)) <= 0:
        return {"active": False, "count": 0, "sum_pnl_amount": 0.0, "avg_pnl_pct": 0.0}
    lookback_days = int(params.get("option_source_pnl_feedback_lookback_days", 40))
    min_trades = int(params.get("option_source_pnl_feedback_min_trades", 2))
    trigger_sum = float(params.get("option_source_pnl_feedback_trigger_sum_pnl", 0.0))
    trigger_avg = float(params.get("option_source_pnl_feedback_trigger_avg_pnl_pct", 0.0))
    try:
        cur = datetime.strptime(date, "%Y%m%d")
    except Exception:
        return {"active": False, "count": 0, "sum_pnl_amount": 0.0, "avg_pnl_pct": 0.0}

    recent: list[dict[str, Any]] = []
    for trade in trades:
        if str(trade.get("entry_gap_source") or "") != "option":
            continue
        exit_date = str(trade.get("exit_date") or "")
        try:
            exit_dt = datetime.strptime(exit_date, "%Y%m%d")
        except Exception:
            continue
        if exit_dt >= cur:
            continue
        if (cur - exit_dt).days <= lookback_days:
            recent.append(trade)

    pnl_amounts = [float(t.get("pnl_amount", 0.0) or 0.0) for t in recent]
    pnl_pcts = [float(t.get("pnl_pct", 0.0) or 0.0) for t in recent]
    count = len(recent)
    sum_pnl = sum(pnl_amounts)
    avg_pnl = sum(pnl_pcts) / count if count else 0.0
    active = count >= min_trades and (sum_pnl <= trigger_sum or avg_pnl <= trigger_avg)
    return {
        "active": bool(active),
        "count": count,
        "sum_pnl_amount": float(sum_pnl),
        "avg_pnl_pct": float(avg_pnl),
        "lookback_days": lookback_days,
        "scale": float(params.get("option_source_pnl_feedback_scale", 0.5)),
    }


def _apply_option_source_pnl_feedback(
    rows: pd.DataFrame,
    trades: list[dict[str, Any]],
    date: str,
    params: dict[str, float],
) -> pd.DataFrame:
    state = _option_source_pnl_feedback_state(trades, date, params)
    if rows.empty or not bool(state.get("active")):
        return rows
    if not {"value_gap_amount", "value_gap_pct_of_cash", "position_cash"} <= set(rows.columns):
        return rows

    adjusted = rows.copy()
    if "position_cash_scale" not in adjusted.columns:
        adjusted["position_cash_scale"] = 1.0

    option_mask_values: list[bool] = []
    for row in adjusted.itertuples(index=False):
        source, _, _ = _gap_source_shares(row, params)
        option_mask_values.append(source == "option")
    option_mask = pd.Series(option_mask_values, index=adjusted.index)
    if not bool(option_mask.any()):
        return adjusted

    scale = max(0.0, min(1.0, float(state.get("scale", 0.5))))
    adjusted.loc[option_mask, "position_cash_scale"] = (
        adjusted.loc[option_mask, "position_cash_scale"].astype(float) * scale
    )
    adjusted.loc[option_mask, "value_gap_amount"] = (
        adjusted.loc[option_mask, "value_gap_amount"].astype(float) * scale
    )
    adjusted.loc[option_mask, "value_gap_pct_of_cash"] = (
        adjusted.loc[option_mask, "value_gap_amount"].astype(float)
        / adjusted.loc[option_mask, "position_cash"].astype(float)
    )
    adjusted["option_source_pnl_feedback_active"] = bool(state["active"])
    adjusted["option_source_pnl_feedback_count"] = int(state["count"])
    adjusted["option_source_pnl_feedback_sum_pnl"] = float(state["sum_pnl_amount"])
    adjusted["option_source_pnl_feedback_avg_pnl_pct"] = float(state["avg_pnl_pct"])
    return adjusted


def _option_entry_gate_ok(
    row_by_ts: dict[str, Any],
    ts: str,
    date: str,
    params: dict[str, float],
    timely_signal_maps: dict[str, dict[str, int]],
) -> bool:
    if float(params.get("option_entry_gate_enabled", 0.0)) <= 0:
        return True
    row = row_by_ts.get(ts)
    if row is None:
        return False
    source, _, option_share = _gap_source_shares(row, params)
    if source != "option":
        return True

    if not all(hasattr(row, key) for key in ("bond_floor", "close")):
        return False
    bond_floor = float(row.bond_floor)
    close = float(row.close)
    if bond_floor <= 0:
        return False

    max_ratio = float(params.get("option_entry_max_close_to_bond_floor", 999.0))
    if close / bond_floor > max_ratio:
        return False

    max_option_share = float(params.get("option_entry_max_option_share", 999.0))
    if option_share > max_option_share:
        return False

    max_bad_signals = int(params.get("option_entry_max_bad_signals", 999))
    if max_bad_signals < 999:
        signal_count = int(timely_signal_maps.get(ts, {}).get(date, 0))
        if signal_count > max_bad_signals:
            return False

    return True


_OPTION_ENTRY_GATE_KEYS = (
    "option_entry_gate_enabled",
    "option_entry_max_close_to_bond_floor",
    "option_entry_max_option_share",
    "option_entry_max_bad_signals",
)


def _option_entry_params_for_regime(params: dict[str, float], regime: str) -> dict[str, float]:
    effective = dict(params)
    for key in _OPTION_ENTRY_GATE_KEYS:
        regime_key = f"{key}_{regime}"
        if regime_key in params:
            effective[key] = params[regime_key]
    return effective


def _has_option_entry_gate(params: dict[str, float]) -> bool:
    if float(params.get("option_entry_gate_enabled", 0.0)) > 0:
        return True
    return any(
        key.startswith("option_entry_gate_enabled_") and float(value) > 0
        for key, value in params.items()
    )


def _option_entry_needs_timely_signals(params: dict[str, float]) -> bool:
    keys = ["option_entry_max_bad_signals"]
    keys.extend(k for k in params if k.startswith("option_entry_max_bad_signals_"))
    return any(int(params.get(k, 999)) < 999 for k in keys)


def _panic_option_bond_anchor_ok(row_by_ts: dict[str, Any], ts: str, params: dict[str, float]) -> bool:
    if float(params.get("panic_option_bond_anchor_enabled", 0.0)) <= 0:
        return True
    row = row_by_ts.get(ts)
    if row is None:
        return False
    if _gap_source(row_by_ts, ts, params) != "option":
        return True
    if not all(hasattr(row, key) for key in ("bond_floor", "close")):
        return True
    buffer = float(params.get("panic_option_bond_floor_buffer", 0.10))
    return float(row.close) <= float(row.bond_floor) * (1.0 + buffer)


def _apply_panic_option_value_weight(rows: pd.DataFrame, params: dict[str, float]) -> pd.DataFrame:
    if rows.empty or float(params.get("panic_option_value_weight_enabled", 0.0)) <= 0:
        return rows
    required = {"close", "bond_floor", "option_value", "theoretical", "buy_qty", "position_cash"}
    if not required <= set(rows.columns):
        return rows

    adjusted = rows.copy()
    close = adjusted["close"].astype(float)
    bond_floor = adjusted["bond_floor"].astype(float)
    option_value = adjusted["option_value"].astype(float)
    theoretical = adjusted["theoretical"].astype(float)
    base_gap = theoretical - close
    bond_gap = (bond_floor - close).clip(lower=0.0)
    option_gap = theoretical - pd.concat([close, bond_floor], axis=1).max(axis=1)
    option_gap = option_gap.clip(lower=0.0)
    total_gap = bond_gap + option_gap
    option_share = option_gap / total_gap.where(total_gap > 0, pd.NA)
    option_mask = (
        (base_gap > 0)
        & (option_share >= float(params.get("source_option_threshold", 0.60)))
        & (bond_floor > 0)
    )
    if not bool(option_mask.any()):
        return adjusted

    ratio = close / bond_floor
    weights = pd.Series(float(params.get("panic_option_weight_far", 0.10)), index=adjusted.index)
    weights.loc[ratio <= float(params.get("panic_option_weight_level_3", 1.15))] = float(
        params.get("panic_option_weight_15", 0.40)
    )
    weights.loc[ratio <= float(params.get("panic_option_weight_level_2", 1.10))] = float(
        params.get("panic_option_weight_10", 0.70)
    )
    weights.loc[ratio <= float(params.get("panic_option_weight_level_1", 1.05))] = float(
        params.get("panic_option_weight_05", 1.00)
    )

    new_theoretical = bond_floor + option_value * weights
    adjusted.loc[option_mask, "theoretical"] = new_theoretical.loc[option_mask]
    adjusted.loc[option_mask, "deviation"] = (
        (close.loc[option_mask] - new_theoretical.loc[option_mask])
        / new_theoretical.loc[option_mask]
    )
    adjusted.loc[option_mask, "value_gap_amount"] = (
        (new_theoretical.loc[option_mask] - close.loc[option_mask])
        * adjusted.loc[option_mask, "buy_qty"].astype(float)
    )
    adjusted.loc[option_mask, "value_gap_pct_of_cash"] = (
        adjusted.loc[option_mask, "value_gap_amount"]
        / adjusted.loc[option_mask, "position_cash"].astype(float)
    )
    return adjusted


def _stop_gap_ratio_floor_for_day(features: dict[str, Any], params: dict[str, float]) -> float:
    base = float(params.get("stop_gap_ratio_floor", 0.0))
    regime = str(features.get("regime", "neutral"))
    floor = float(params.get(f"stop_gap_ratio_floor_{regime}", base))
    if float(params.get("panic_rebound_enabled", 0.0)) <= 0:
        return floor

    drawdown = float(features.get("drawdown", 0.0))
    ret_20d = float(features.get("ret_20d", 0.0))
    breadth_20d = float(features.get("breadth_20d", 0.5))
    amount_pctile = float(features.get("amount_pctile", 0.5))
    panic_drawdown = float(params.get("panic_drawdown", -0.08))
    panic_ret_20d = float(params.get("panic_ret_20d", 0.0))
    panic_breadth_20d = float(params.get("panic_breadth_20d", 0.5))
    panic_amount_pctile = float(params.get("panic_amount_pctile", 0.5))
    if (
        drawdown <= panic_drawdown
        and ret_20d >= panic_ret_20d
        and breadth_20d >= panic_breadth_20d
        and amount_pctile >= panic_amount_pctile
    ):
        return float(params.get("panic_rebound_stop_gap_ratio_floor", floor))
    return floor


def _switch_hurdle_pct_for_current(
    base_pct: float,
    row_by_ts: dict[str, Any],
    ts: str,
    current_gap: float,
    params: dict[str, float],
    panic_active: bool,
) -> float:
    if (
        not panic_active
        or float(params.get("panic_current_option_switch_hurdle_enabled", 0.0)) <= 0
        or current_gap <= 0
    ):
        return base_pct
    if _gap_source(row_by_ts, ts, params) != "option":
        return base_pct
    panic_pct = float(params.get("panic_current_option_switch_hurdle_pct", base_pct))
    return max(base_pct, panic_pct)


def _panic_rebound_bonus_amount(
    compare_rows: dict[str, Any],
    ts: str,
    current_gap: float | None,
    params: dict[str, float],
    panic_active: bool,
) -> float:
    if (
        not panic_active
        or float(params.get("panic_rebound_bonus_enabled", 0.0)) <= 0
        or current_gap is None
        or current_gap <= 0
        or _gap_source(compare_rows, ts, params) != "option"
    ):
        return 0.0
    row = compare_rows.get(ts)
    if row is None or not all(hasattr(row, key) for key in ("close", "bond_floor", "position_cash")):
        return 0.0
    close = float(row.close)
    bond_floor = float(row.bond_floor)
    position_cash = float(row.position_cash)
    if close <= 0 or bond_floor <= 0 or position_cash <= 0:
        return 0.0

    ratio = close / bond_floor
    pct = 0.0
    if ratio <= float(params.get("panic_rebound_bonus_level_1", 1.05)):
        pct = float(params.get("panic_rebound_bonus_pct_05", 0.12))
    elif ratio <= float(params.get("panic_rebound_bonus_level_2", 1.10)):
        pct = float(params.get("panic_rebound_bonus_pct_10", 0.08))
    elif ratio <= float(params.get("panic_rebound_bonus_level_3", 1.15)):
        pct = float(params.get("panic_rebound_bonus_pct_15", 0.04))
    else:
        pct = float(params.get("panic_rebound_bonus_pct_far", 0.0))
    return max(0.0, position_cash * pct)


def _option_opportunity_active(
    protected_until: dict[str, int],
    ts: str,
    day_i: int,
) -> bool:
    return protected_until.get(ts, -1) >= day_i


def _run_value_gap_backtest(
    ranks: pd.DataFrame,
    start: str,
    end: str,
    data_root: Path,
    fixed_source: int,
    rule: str,
    params: dict[str, float],
    stop_revalue_ranks: pd.DataFrame | None = None,
    conservative_ranks: pd.DataFrame | None = None,
    panic_ranks: pd.DataFrame | None = None,
    opportunity_dates_override: set[str] | None = None,
) -> dict[str, Any]:
    cfgs = _base_configs(data_root, fixed_source)
    if float(params.get("cost_model_enabled", 0.0)) > 0:
        cfgs = {
            name: replace(
                cfg,
                cost_model_enabled=True,
                slippage_pct=float(params.get("slippage_pct", 0.0015)),
                market_impact_coeff=float(params.get("market_impact_coeff", 0.0010)),
                market_impact_cap_pct=float(params.get("market_impact_cap_pct", 0.02)),
                holding_cost_pct=float(params.get("holding_cost_pct", 0.0)),
            )
            for name, cfg in cfgs.items()
        }
    features = _build_daily_features(252, rule)
    cb_daily = _load_cb_daily().copy()
    cb_daily["trade_date"] = cb_daily["trade_date"].astype(str)
    cb_daily = cb_daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    cb_daily["amount_5d"] = (
        cb_daily.groupby("ts_code")["amount_yuan"]
        .rolling(window=5, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )
    cb_daily = cb_daily[(cb_daily["trade_date"] >= start) & (cb_daily["trade_date"] <= end)]
    close_by_date = {
        d: {r.ts_code: float(r.close) for r in g.itertuples(index=False)}
        for d, g in cb_daily.groupby("trade_date")
    }
    avg_amount_5d_by_date = {
        d: {r.ts_code: float(getattr(r, "amount_5d", 0.0) or 0.0) for r in g.itertuples(index=False)}
        for d, g in cb_daily.groupby("trade_date")
    }
    ranks_by_date = {d: g for d, g in ranks.groupby("trade_date")}
    stop_revalue_by_date = (
        {d: g for d, g in stop_revalue_ranks.groupby("trade_date")}
        if stop_revalue_ranks is not None
        else {}
    )
    conservative_by_date = (
        {d: g for d, g in conservative_ranks.groupby("trade_date")}
        if conservative_ranks is not None
        else {}
    )
    panic_by_date = (
        {d: g for d, g in panic_ranks.groupby("trade_date")} if panic_ranks is not None else {}
    )
    call_index = _build_call_index(_load_cb_call())
    days = [d for d in _load_trading_days() if start <= d <= end]

    neutral = cfgs["neutral"]
    cash = float(neutral.initial_capital)
    holdings: dict[str, Position] = {}
    panic_stop_until: dict[str, int] = {}
    protected_opportunity_until: dict[str, int] = {}
    protected_opportunity_bad_days: dict[str, int] = {}
    protected_opportunity_recovery_days: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[str, float]] = []

    min_gap_pct = float(params["min_gap_pct"])
    sell_gap_pct = float(params["sell_gap_pct"])
    switch_hurdle_pct = float(params["switch_hurdle_pct"])
    max_hold_days = int(params["max_hold_days"])
    stop_gap_ratio_floor = float(params.get("stop_gap_ratio_floor", 0.0))
    stop_signal_threshold = int(params.get("stop_signal_threshold", 999))
    timely_signal_maps = (
        _build_timely_signal_maps(start, end)
        if stop_signal_threshold < 999
        or (_has_option_entry_gate(params) and _option_entry_needs_timely_signals(params))
        else {}
    )
    panic_delay_enabled = float(params.get("panic_option_delay_enabled", 0.0)) > 0
    panic_dates = (
        _build_panic_dates(start, end, params)
        if (
            panic_delay_enabled
            or float(params.get("panic_option_bond_anchor_enabled", 0.0)) > 0
            or float(params.get("panic_option_value_weight_enabled", 0.0)) > 0
            or float(params.get("panic_opportunity_protect_enabled", 0.0)) > 0
            or (
                float(params.get("adjusted_value_gate_enabled", 0.0)) > 0
                and panic_ranks is not None
            )
        )
        else set()
    )
    adjusted_value_enabled = float(params.get("adjusted_value_gate_enabled", 0.0)) > 0
    adjusted_gate_min_gap_pct = float(params.get("adjusted_gate_min_gap_pct", 0.0))
    panic_option_weight_scope = str(params.get("panic_option_value_weight_scope", "global"))
    if opportunity_dates_override is not None:
        opportunity_dates = {d for d in opportunity_dates_override if start <= d <= end}
    else:
        opportunity_dates = (
            _build_panic_opportunity_dates(start, end, params)
            if float(params.get("panic_opportunity_protect_enabled", 0.0)) > 0
            else set()
        )

    def sell(
        date: str,
        ts: str,
        reason: str,
        close_map: dict[str, float],
        cfg: Any,
        avg_amount_5d_map: dict[str, float],
    ) -> None:
        pos = holdings.pop(ts, None)
        if pos is None:
            return
        exit_price = float(close_map.get(ts, pos.entry_price))
        try:
            hd = (
                datetime.strptime(date, "%Y%m%d")
                - datetime.strptime(pos.entry_date, "%Y%m%d")
            ).days
        except Exception:
            hd = 0
        sell_cost = apply_cost_model(
            exit_price,
            pos.qty,
            "sell",
            cfg,
            avg_amount_5d=avg_amount_5d_map.get(ts),
            holding_days=hd,
        )
        proceeds = float(sell_cost["cash_amount"])
        nonlocal cash
        cash += proceeds
        pnl_amount = proceeds - pos.cost
        if getattr(cfg, "cost_model_enabled", False):
            pnl_pct = pnl_amount / pos.cost if pos.cost > 0 else 0.0
        else:
            pnl_pct = exit_price / pos.entry_price - 1.0 if pos.entry_price > 0 else 0.0
        trades.append(
            {
                "cb_code": ts,
                "cb_name": pos.name,
                "entry_date": pos.entry_date,
                "exit_date": date,
                "entry_price": round(pos.entry_price, 4),
                "exit_price": round(exit_price, 4),
                "pnl_pct": round(pnl_pct, 6),
                "pnl_amount": round(pnl_amount, 2),
                "holding_days": hd,
                "exit_reason": reason,
                "entry_gap_amount": round(pos.entry_gap_amount, 2),
                "entry_gap_pct": round(pos.entry_gap_pct, 6),
                "entry_gap_source": pos.entry_gap_source,
                "entry_position_cash_scale": round(pos.entry_position_cash_scale, 6),
            }
        )

    for day_i, date in enumerate(days):
        day_features = features.get(date, {})
        regime = day_features.get("regime", "neutral")
        cfg = cfgs.get(regime, neutral)
        day_stop_gap_ratio_floor = _stop_gap_ratio_floor_for_day(day_features, params)
        close_map = close_by_date.get(date, {})
        avg_amount_5d_map = avg_amount_5d_by_date.get(date, {})
        no_trade_today = (
            date in panic_dates
            and float(params.get("panic_no_trade_enabled", 0.0)) > 0
        )
        buy_only_today = (
            date in panic_dates
            and float(params.get("panic_buy_only_enabled", 0.0)) > 0
        )
        ranked_today = ranks_by_date.get(date)
        stop_revalue_today = stop_revalue_by_date.get(date)
        conservative_today = conservative_by_date.get(date)
        panic_today = panic_by_date.get(date)
        row_by_ts: dict[str, Any] = {}
        stop_row_by_ts: dict[str, Any] = {}
        adjusted_row_by_ts: dict[str, Any] = {}
        panic_weight_row_by_ts: dict[str, Any] = {}
        panic_weight_stop_row_by_ts: dict[str, Any] = {}
        use_triggered_panic_weight = (
            date in panic_dates
            and panic_option_weight_scope == "triggered_revalue"
            and float(params.get("panic_option_value_weight_enabled", 0.0)) > 0
        )
        candidates: list[Any] = []
        if ranked_today is not None and not ranked_today.empty:
            ranked_today = ranked_today.copy()
            if date in panic_dates and panic_option_weight_scope != "triggered_revalue":
                ranked_today = _apply_panic_option_value_weight(ranked_today, params)
            ranked_today = _apply_option_source_pnl_feedback(ranked_today, trades, date, params)
            ranked_today = ranked_today.sort_values("value_gap_amount", ascending=False)
            row_by_ts = {r.ts_code: r for r in ranked_today.itertuples(index=False)}
            if use_triggered_panic_weight:
                panic_weight_today = _apply_panic_option_value_weight(ranked_today, params)
                panic_weight_row_by_ts = {
                    r.ts_code: r for r in panic_weight_today.itertuples(index=False)
                }
            candidates = [
                r
                for r in ranked_today.itertuples(index=False)
                if float(r.value_gap_amount) > 0
                and float(r.value_gap_pct_of_cash) >= min_gap_pct
            ]
        if stop_revalue_today is not None and not stop_revalue_today.empty:
            stop_revalue_today = stop_revalue_today.copy()
            if date in panic_dates and panic_option_weight_scope != "triggered_revalue":
                stop_revalue_today = _apply_panic_option_value_weight(stop_revalue_today, params)
            stop_revalue_today = _apply_option_source_pnl_feedback(stop_revalue_today, trades, date, params)
            stop_row_by_ts = {r.ts_code: r for r in stop_revalue_today.itertuples(index=False)}
            if use_triggered_panic_weight:
                panic_weight_stop_today = _apply_panic_option_value_weight(stop_revalue_today, params)
                panic_weight_stop_row_by_ts = {
                    r.ts_code: r for r in panic_weight_stop_today.itertuples(index=False)
                }
        else:
            stop_row_by_ts = row_by_ts
            panic_weight_stop_row_by_ts = panic_weight_row_by_ts
        if adjusted_value_enabled:
            adjusted_today = panic_today if date in panic_dates and panic_today is not None else conservative_today
            if adjusted_today is not None and not adjusted_today.empty:
                adjusted_row_by_ts = {r.ts_code: r for r in adjusted_today.itertuples(index=False)}
            else:
                adjusted_row_by_ts = row_by_ts
            candidates = [
                r
                for r in candidates
                if _row_positive_gap(adjusted_row_by_ts, str(r.ts_code), adjusted_gate_min_gap_pct)
            ]
        if date in panic_dates and float(params.get("panic_option_bond_anchor_enabled", 0.0)) > 0:
            candidates = [
                r
                for r in candidates
                if _panic_option_bond_anchor_ok(row_by_ts, str(r.ts_code), params)
            ]
        option_entry_params = _option_entry_params_for_regime(params, str(regime))
        if _has_option_entry_gate(option_entry_params):
            candidates = [
                r
                for r in candidates
                if _option_entry_gate_ok(
                    row_by_ts,
                    str(r.ts_code),
                    date,
                    option_entry_params,
                    timely_signal_maps,
                )
            ]

        opportunity_protect_enabled = float(params.get("panic_opportunity_protect_enabled", 0.0)) > 0
        if opportunity_protect_enabled:
            max_bad_days = int(params.get("panic_opportunity_bad_days", 5))
            for ts in list(protected_opportunity_until):
                if protected_opportunity_until[ts] < day_i:
                    protected_opportunity_until.pop(ts, None)
                    protected_opportunity_bad_days.pop(ts, None)
                    protected_opportunity_recovery_days.pop(ts, None)
                    continue
                recovered_market = date not in opportunity_dates
                if recovered_market:
                    protected_opportunity_recovery_days[ts] = (
                        protected_opportunity_recovery_days.get(ts, 0) + 1
                    )
                else:
                    protected_opportunity_recovery_days[ts] = 0
                if (
                    float(params.get("panic_opportunity_exit_on_recovery_enabled", 0.0)) > 0
                    and protected_opportunity_recovery_days.get(ts, 0)
                    >= int(params.get("panic_opportunity_recovery_days", 3))
                ):
                    protected_opportunity_until.pop(ts, None)
                    protected_opportunity_bad_days.pop(ts, None)
                    protected_opportunity_recovery_days.pop(ts, None)
                    continue
                if _row_positive_gap(row_by_ts, ts, min_gap_pct):
                    protected_opportunity_bad_days[ts] = 0
                else:
                    protected_opportunity_bad_days[ts] = protected_opportunity_bad_days.get(ts, 0) + 1
                    if protected_opportunity_bad_days[ts] >= max_bad_days:
                        protected_opportunity_until.pop(ts, None)
                        protected_opportunity_bad_days.pop(ts, None)
                        protected_opportunity_recovery_days.pop(ts, None)

            if date in opportunity_dates:
                protect_days = int(params.get("panic_opportunity_protect_days", 20))
                max_candidate_protect = int(params.get("panic_opportunity_protect_top_n", 0))
                protect_ts: set[str] = set(holdings)
                if max_candidate_protect > 0:
                    protect_ts.update(str(r.ts_code) for r in candidates[:max_candidate_protect])
                for ts in protect_ts:
                    if (
                        _row_positive_gap(row_by_ts, ts, min_gap_pct)
                        and _gap_source(row_by_ts, ts, params) == "option"
                    ):
                        protected_opportunity_until[ts] = max(
                            protected_opportunity_until.get(ts, day_i),
                            day_i + protect_days,
                        )
                        protected_opportunity_bad_days[ts] = 0
                        protected_opportunity_recovery_days[ts] = 0

        if no_trade_today:
            equity_curve.append((date, _equity(cash, holdings, close_map)))
            continue

        # Mandatory exits. A price stop is not an automatic sell in the
        # value-gap model; it only forces a fresh opportunity-cost check.
        stop_review: set[str] = set()
        protected_stop_review: set[str] = set()
        if not buy_only_today:
            for ts, pos in list(holdings.items()):
                if ts not in close_map:
                    continue
                if _is_force_redeemed_on_date(ts, date, call_index):
                    sell(date, ts, "force_redemption", close_map, cfg, avg_amount_5d_map)
                    continue
                try:
                    hd = (
                        datetime.strptime(date, "%Y%m%d")
                        - datetime.strptime(pos.entry_date, "%Y%m%d")
                    ).days
                except Exception:
                    hd = 0
                cur_px = float(close_map[ts])
                pnl = cur_px / pos.entry_price - 1.0 if pos.entry_price > 0 else 0.0
                if (
                    date in panic_dates
                    and float(params.get("panic_option_bond_anchor_sell_enabled", 0.0)) > 0
                    and not _panic_option_bond_anchor_ok(row_by_ts, ts, params)
                ):
                    sell(date, ts, "panic_option_bond_anchor_failed", close_map, cfg, avg_amount_5d_map)
                    continue
                if pnl <= cfg.stop_loss_pct:
                    stop_compare_rows = (
                        panic_weight_stop_row_by_ts if use_triggered_panic_weight else stop_row_by_ts
                    )
                    _, _, gap_source = _stressed_gap(stop_compare_rows, ts, params, qty=pos.qty)
                    if float(params.get("source_stress_enabled", 0.0)) > 0:
                        gap, gap_pct, _ = _stressed_gap(stop_compare_rows, ts, params, qty=pos.qty)
                    else:
                        gap, gap_pct = _current_gap(stop_compare_rows, ts)
                    rebound_bonus = _panic_rebound_bonus_amount(
                        stop_compare_rows,
                        ts,
                        gap,
                        params,
                        date in panic_dates,
                    )
                    if rebound_bonus > 0 and gap is not None:
                        gap = float(gap) + rebound_bonus
                        position_cash = float(
                            getattr(stop_compare_rows.get(ts), "position_cash", 0.0) or 0.0
                        )
                        if position_cash > 0:
                            gap_pct = float(gap) / position_cash
                    signal_count = timely_signal_maps.get(ts, {}).get(date, 0)
                    gap_ratio = (
                        float(gap) / pos.entry_gap_amount
                        if gap is not None and pos.entry_gap_amount > 0
                        else 0.0
                    )
                    normal_gap_source = _gap_source(row_by_ts, ts, params)
                    protect_panic_option_stop = (
                        date in panic_dates
                        and float(params.get("panic_option_review_enabled", 0.0)) > 0
                        and normal_gap_source == "option"
                        and signal_count < stop_signal_threshold
                    )
                    protect_opportunity_stop = (
                        opportunity_protect_enabled
                        and _option_opportunity_active(protected_opportunity_until, ts, day_i)
                        and signal_count < stop_signal_threshold
                    )
                    if (
                        panic_delay_enabled
                        and date in panic_dates
                        and gap_source == "option"
                        and gap is not None
                        and gap > 0
                    ):
                        panic_stop_until[ts] = max(
                            panic_stop_until.get(ts, day_i),
                            day_i + int(params.get("panic_option_delay_days", 5)),
                        )
                        stop_review.add(ts)
                        continue
                    if (
                        panic_delay_enabled
                        and panic_stop_until.get(ts, -1) >= day_i
                        and gap_source == "option"
                        and gap is not None
                        and gap > 0
                    ):
                        stop_review.add(ts)
                        continue
                    if signal_count >= stop_signal_threshold:
                        sell(date, ts, "stop_loss_timely_signals", close_map, cfg, avg_amount_5d_map)
                    elif protect_opportunity_stop:
                        stop_review.add(ts)
                        protected_stop_review.add(ts)
                    elif protect_panic_option_stop:
                        stop_review.add(ts)
                        protected_stop_review.add(ts)
                    elif gap is None or gap <= 0 or gap_ratio < day_stop_gap_ratio_floor:
                        sell(date, ts, "stop_loss_value_gone", close_map, cfg, avg_amount_5d_map)
                    else:
                        stop_review.add(ts)
                    continue
                gap, gap_pct = _current_gap(row_by_ts, ts)
                if (
                    adjusted_value_enabled
                    and not _row_positive_gap(adjusted_row_by_ts, ts, adjusted_gate_min_gap_pct)
                ):
                    sell(date, ts, "adjusted_value_gone", close_map, cfg, avg_amount_5d_map)
                    continue
                if gap is not None and gap_pct is not None and gap <= 0 and gap_pct <= sell_gap_pct:
                    sell(date, ts, "value_repaired", close_map, cfg, avg_amount_5d_map)
                    continue
                if hd >= max_hold_days:
                    sell(date, ts, "max_holding_days", close_map, cfg, avg_amount_5d_map)

        cur_equity = _equity(cash, holdings, close_map)

        # Stop-loss review: the loss is sunk. Only switch if a fresh candidate
        # beats this holding's current remaining value gap by the hurdle.
        if not buy_only_today:
            for cand in candidates:
                if not stop_review:
                    break
                if cand.ts_code in holdings:
                    continue
                reviewed_gaps: list[tuple[float, str]] = []
                for ts in list(stop_review):
                    if ts not in holdings:
                        stop_review.discard(ts)
                        continue
                    if ts in protected_stop_review:
                        gap, _ = _current_gap(row_by_ts, ts)
                    elif adjusted_value_enabled:
                        gap, _ = _current_gap(adjusted_row_by_ts, ts)
                    elif float(params.get("source_stress_enabled", 0.0)) > 0:
                        compare_rows = (
                            panic_weight_stop_row_by_ts
                            if use_triggered_panic_weight
                            else stop_row_by_ts
                        )
                        gap, _, _ = _stressed_gap(compare_rows, ts, params, qty=holdings[ts].qty)
                    elif use_triggered_panic_weight:
                        gap, _ = _current_gap(panic_weight_stop_row_by_ts, ts)
                    else:
                        gap, _ = _current_gap(stop_row_by_ts, ts)
                    gap_bonus = _panic_rebound_bonus_amount(
                        panic_weight_stop_row_by_ts if use_triggered_panic_weight else stop_row_by_ts,
                        ts,
                        gap,
                        params,
                        date in panic_dates,
                    )
                    if gap is not None and gap_bonus > 0:
                        gap = float(gap) + gap_bonus
                    reviewed_gaps.append((float(gap) if gap is not None else -1e18, ts))
                if not reviewed_gaps:
                    break
                worst_gap, worst_ts = min(reviewed_gaps, key=lambda x: x[0])
                stop_compare_rows_for_hurdle = (
                    panic_weight_stop_row_by_ts if use_triggered_panic_weight else stop_row_by_ts
                )
                effective_switch_hurdle_pct = _switch_hurdle_pct_for_current(
                    switch_hurdle_pct,
                    stop_compare_rows_for_hurdle,
                    worst_ts,
                    worst_gap,
                    params,
                    date in panic_dates,
                )
                if (
                    worst_ts in protected_stop_review
                    and float(params.get("panic_option_review_switch_hurdle_pct", -1.0)) >= 0
                ):
                    effective_switch_hurdle_pct = max(
                        effective_switch_hurdle_pct,
                        float(params.get("panic_option_review_switch_hurdle_pct", 0.12)),
                    )
                if (
                    _option_opportunity_active(protected_opportunity_until, worst_ts, day_i)
                    and float(params.get("panic_opportunity_switch_hurdle_pct", -1.0)) >= 0
                ):
                    effective_switch_hurdle_pct = max(
                        effective_switch_hurdle_pct,
                        float(params.get("panic_opportunity_switch_hurdle_pct", 0.12)),
                    )
                switch_hurdle_amount = float(cand.position_cash) * effective_switch_hurdle_pct
                if adjusted_value_enabled:
                    cand_gap, _ = _current_gap(adjusted_row_by_ts, str(cand.ts_code))
                elif worst_ts in protected_stop_review:
                    cand_gap, _ = _current_gap(row_by_ts, str(cand.ts_code))
                elif float(params.get("source_stress_enabled", 0.0)) > 0:
                    compare_rows = (
                        panic_weight_stop_row_by_ts
                        if use_triggered_panic_weight
                        else stop_row_by_ts
                    )
                    cand_gap, _, _ = _stressed_gap(compare_rows, str(cand.ts_code), params)
                elif use_triggered_panic_weight:
                    cand_gap, _ = _current_gap(panic_weight_stop_row_by_ts, str(cand.ts_code))
                else:
                    cand_gap, _ = _current_gap(stop_row_by_ts, str(cand.ts_code))
                if cand_gap is not None and float(cand_gap) > worst_gap + switch_hurdle_amount:
                    sell(date, worst_ts, "stop_loss_switch_value_gap", close_map, cfg, avg_amount_5d_map)
                    stop_review.discard(worst_ts)
                    cur_equity = _equity(cash, holdings, close_map)
                else:
                    break

        # Opportunity-cost switch: replace the lowest remaining value gap when
        # a new candidate clears the hurdle.
        if not buy_only_today:
            for cand in candidates:
                if cand.ts_code in holdings:
                    continue
                if len(holdings) < int(cfg.max_holdings):
                    break
                holding_gaps: list[tuple[float, str]] = []
                for ts in holdings:
                    if _option_opportunity_active(protected_opportunity_until, ts, day_i):
                        gap, _ = _current_gap(row_by_ts, ts)
                    elif adjusted_value_enabled:
                        gap, _ = _current_gap(adjusted_row_by_ts, ts)
                    elif use_triggered_panic_weight:
                        gap, _ = _current_gap(panic_weight_row_by_ts, ts)
                    else:
                        gap, _ = _current_gap(row_by_ts, ts)
                    gap_bonus = _panic_rebound_bonus_amount(
                        panic_weight_row_by_ts if use_triggered_panic_weight else row_by_ts,
                        ts,
                        gap,
                        params,
                        date in panic_dates,
                    )
                    if gap is not None and gap_bonus > 0:
                        gap = float(gap) + gap_bonus
                    holding_gaps.append((float(gap) if gap is not None else -1e18, ts))
                if not holding_gaps:
                    break
                worst_gap, worst_ts = min(holding_gaps, key=lambda x: x[0])
                holding_compare_rows_for_hurdle = (
                    panic_weight_row_by_ts if use_triggered_panic_weight else row_by_ts
                )
                effective_switch_hurdle_pct = _switch_hurdle_pct_for_current(
                    switch_hurdle_pct,
                    holding_compare_rows_for_hurdle,
                    worst_ts,
                    worst_gap,
                    params,
                    date in panic_dates,
                )
                if (
                    _option_opportunity_active(protected_opportunity_until, worst_ts, day_i)
                    and float(params.get("panic_opportunity_switch_hurdle_pct", -1.0)) >= 0
                ):
                    effective_switch_hurdle_pct = max(
                        effective_switch_hurdle_pct,
                        float(params.get("panic_opportunity_switch_hurdle_pct", 0.12)),
                    )
                switch_hurdle_amount = float(cand.position_cash) * effective_switch_hurdle_pct
                if use_triggered_panic_weight:
                    cand_gap, _ = _current_gap(panic_weight_row_by_ts, str(cand.ts_code))
                    compare_cand_gap = float(cand_gap) if cand_gap is not None else -1e18
                else:
                    compare_cand_gap = float(cand.value_gap_amount)
                if compare_cand_gap > worst_gap + switch_hurdle_amount:
                    sell(date, worst_ts, "switch_value_gap", close_map, cfg, avg_amount_5d_map)
                    cur_equity = _equity(cash, holdings, close_map)
                else:
                    break

        # Buy by absolute value gap.
        for cand in candidates:
            if len(holdings) >= int(cfg.max_holdings):
                break
            ts = str(cand.ts_code)
            if ts in holdings:
                continue
            if _is_force_redeemed_on_date(ts, date, call_index):
                continue
            mkt = float(cand.close)
            position_scale = 1.0
            if float(params.get("candidate_position_scale_enabled", 0.0)) > 0:
                try:
                    position_scale = float(getattr(cand, "position_cash_scale", 1.0) or 1.0)
                except (TypeError, ValueError):
                    position_scale = 1.0
                position_scale = max(0.0, min(1.0, position_scale))
            pos_cash = min(cash, cur_equity * float(cfg.max_position_pct) * position_scale)
            if pos_cash < mkt:
                continue
            estimated_cost = apply_cost_model(
                mkt,
                1.0,
                "buy",
                cfg,
                avg_amount_5d=avg_amount_5d_map.get(ts),
            )
            unit_cash = max(float(estimated_cost["cash_amount"]), 1e-12)
            qty = math.floor(pos_cash / unit_cash)
            if qty < 1:
                continue
            buy_cost = apply_cost_model(
                mkt,
                qty,
                "buy",
                cfg,
                avg_amount_5d=avg_amount_5d_map.get(ts),
            )
            cost = float(buy_cost["cash_amount"])
            cash_limit = min(pos_cash, cash)
            while cost > cash_limit and qty > 0:
                qty -= 1
                buy_cost = apply_cost_model(
                    mkt,
                    qty,
                    "buy",
                    cfg,
                    avg_amount_5d=avg_amount_5d_map.get(ts),
                )
                cost = float(buy_cost["cash_amount"])
            if qty < 1:
                continue
            if cost > cash_limit:
                continue
            cash -= cost
            holdings[ts] = Position(
                ts_code=ts,
                name=str(cand.name),
                entry_date=date,
                entry_price=mkt,
                qty=float(qty),
                cost=cost,
                entry_gap_amount=float(cand.value_gap_amount),
                entry_gap_pct=float(cand.value_gap_pct_of_cash),
                entry_gap_source=_gap_source(row_by_ts, ts, params),
                entry_position_cash_scale=float(position_scale),
            )
            cur_equity = _equity(cash, holdings, close_map)

        equity_curve.append((date, _equity(cash, holdings, close_map)))

    if days:
        last_date = days[-1]
        last_close = close_by_date.get(last_date, {})
        last_avg_amount_5d = avg_amount_5d_by_date.get(last_date, {})
        last_cfg = cfgs.get(features.get(last_date, {}).get("regime", "neutral"), neutral)
        for ts in list(holdings):
            sell(last_date, ts, "end_of_period", last_close, last_cfg, last_avg_amount_5d)
    metrics = _metrics(equity_curve, trades, float(neutral.initial_capital))
    return {
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity_curve,
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--reuse-ranks", action="store_true")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    return p.parse_args()


def _with_cost_params(params: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not args.cost_model_enabled:
        return params
    out = dict(params)
    out.update(
        {
            "cost_model_enabled": 1.0,
            "slippage_pct": float(args.slippage_pct),
            "market_impact_coeff": float(args.market_impact_coeff),
            "market_impact_cap_pct": float(args.market_impact_cap_pct),
            "holding_cost_pct": float(args.holding_cost_pct),
        }
    )
    return out


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_switch_eval"
    output_dir.mkdir(parents=True, exist_ok=True)
    ranks_path = output_dir / "daily_value_gap_amounts.parquet"
    ranks = _load_or_build_value_ranks(
        args.data_root,
        min(args.train_start, args.test_start),
        max(args.train_end, args.test_end),
        args.fixed_source,
        args.rule,
        ranks_path,
        args.reuse_ranks,
    )

    train_rows: list[dict[str, Any]] = []
    test_rows: list[dict[str, Any]] = []
    candidates = _candidate_grid()
    print(f"[value_gap] candidates={len(candidates)}", flush=True)
    for idx, params in enumerate(candidates, 1):
        run_params = _with_cost_params(params, args)
        train = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= args.train_start) & (ranks["trade_date"] <= args.train_end)],
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            run_params,
        )
        row = {
            "candidate": json.dumps(params, sort_keys=True),
            **params,
            **train["metrics"],
        }
        row["score"] = _score(train["metrics"])
        train_rows.append(row)
        print(
            "[value_gap] "
            f"train {idx}/{len(candidates)} excess={row['excess_return']} "
            f"total={row['total_return']} dd={row['max_drawdown']} score={row['score']} "
            f"params={row['candidate']}",
            flush=True,
        )

    train_rows.sort(key=lambda r: float(r["score"]), reverse=True)
    top = train_rows[: args.top_n]
    top_keys = {r["candidate"] for r in top}
    for row in top:
        params = {
            "min_gap_pct": float(row["min_gap_pct"]),
            "sell_gap_pct": float(row["sell_gap_pct"]),
            "switch_hurdle_pct": float(row["switch_hurdle_pct"]),
            "max_hold_days": float(row["max_hold_days"]),
            "stop_gap_ratio_floor": float(row.get("stop_gap_ratio_floor", 0.0)),
            "stop_signal_threshold": float(row.get("stop_signal_threshold", 999)),
        }
        run_params = _with_cost_params(params, args)
        test = _run_value_gap_backtest(
            ranks[(ranks["trade_date"] >= args.test_start) & (ranks["trade_date"] <= args.test_end)],
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            run_params,
        )
        test_row = {
            "candidate": row["candidate"],
            **params,
            **test["metrics"],
        }
        test_row["score"] = _score(test["metrics"])
        test_rows.append(test_row)
        print(
            "[value_gap] "
            f"test excess={test_row['excess_return']} total={test_row['total_return']} "
            f"dd={test_row['max_drawdown']} score={test_row['score']} "
            f"params={test_row['candidate']}",
            flush=True,
        )

    test_rows.sort(key=lambda r: float(r["score"]), reverse=True)
    summary = {
        "train_start": args.train_start,
        "train_end": args.train_end,
        "test_start": args.test_start,
        "test_end": args.test_end,
        "n_candidates": len(candidates),
        "top_n": args.top_n,
        "cost_model_enabled": bool(args.cost_model_enabled),
        "slippage_pct": float(args.slippage_pct),
        "market_impact_coeff": float(args.market_impact_coeff),
        "market_impact_cap_pct": float(args.market_impact_cap_pct),
        "holding_cost_pct": float(args.holding_cost_pct),
        "train_best": train_rows[0] if train_rows else None,
        "test_best": test_rows[0] if test_rows else None,
    }
    _write_csv(output_dir / "train_summary.csv", train_rows)
    _write_csv(output_dir / "test_summary.csv", test_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[value_gap] summary", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[value_gap] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
