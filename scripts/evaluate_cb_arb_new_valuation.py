#!/usr/bin/env python3
"""Evaluate refined CB theoretical value model for value-gap switch strategy.

Computes new theoretical values using a Tsiveriotis-Fernandes decomposition:
  theoretical = bond_floor + option_value

Enhancements over baseline:
  - Dynamic credit spread: rating-based base + market-implied calibration
  - Historical volatility: 60-day rolling from forward-adjusted stock prices
  - Proper bond cash-flow discounting with dynamic spread

Then feeds the new value gaps into the baseline entry/exit logic
(enter when gap > 0, exit when gap <= 0) and compares against the
existing theoretical-value-based gaps.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root & sys.path — must come before any `from scripts.X import Y`.
# The compliance import-reachability probe runs with -E in /tmp, so all
# non-stdlib imports that follow must resolve from the venv site-packages
# (numpy/pandas/yaml) or from REPO_ROOT (scripts.*).
# ---------------------------------------------------------------------------

def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ---------------------------------------------------------------------------
# Lazy third-party imports — not available at module level in isolated probe.
# ---------------------------------------------------------------------------

def _get_np():
    """Lazy import numpy."""
    import numpy as _np
    return _np


def _get_pd():
    """Lazy import pandas."""
    import pandas as _pd
    return _pd


def _get_yaml():
    """Lazy import yaml."""
    import yaml as _yaml
    return _yaml


def _get_scipy_opt():
    """Lazy import scipy.optimize.brentq."""
    from scipy.optimize import brentq as _brentq
    return _brentq


def _get_scipy_stats():
    """Lazy import scipy.stats.norm."""
    from scipy.stats import norm as _norm
    return _norm


# YAML numpy representer registration (once)
_YAML_REPRS_REGISTERED = False


def _ensure_yaml_np_reprs():
    global _YAML_REPRS_REGISTERED
    if _YAML_REPRS_REGISTERED:
        return
    yaml = _get_yaml()
    np = _get_np()

    def _yaml_repr_np_float(dumper, data):
        return dumper.represent_float(float(data))

    def _yaml_repr_np_int(dumper, data):
        return dumper.represent_int(int(data))

    yaml.SafeDumper.add_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_representer(np.integer, _yaml_repr_np_int)
    yaml.SafeDumper.add_multi_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_multi_representer(np.integer, _yaml_repr_np_int)
    _YAML_REPRS_REGISTERED = True


# Risk-free rate proxy (China 5Y government bond ~2.5%)
RISK_FREE_RATE = 0.025

# Rating -> base credit spread (bps)
_RATING_SPREAD_MAP: dict[str, float] = {
    "AAA": 0.0050, "AA+": 0.0080, "AA": 0.0100, "AA-": 0.0130,
    "A+": 0.0160, "A": 0.0200, "A-": 0.0250,
    "BBB+": 0.0320, "BBB": 0.0400, "BBB-": 0.0500,
    "BB+": 0.0650, "BB": 0.0800, "BB-": 0.1000,
    "B+": 0.1200, "B": 0.1500, "B-": 0.1800,
    "CCC": 0.2200, "CC": 0.2800, "C": 0.3500,
}
_DEFAULT_SPREAD = 0.0200


def rating_to_spread(rating: Any) -> float:
    """Map credit rating to base annual spread."""
    pd = _get_pd()
    if not isinstance(rating, str) or pd.isna(rating):
        return _DEFAULT_SPREAD
    r = str(rating).strip().upper()
    return _RATING_SPREAD_MAP.get(r, _DEFAULT_SPREAD)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes European call price."""
    np = _get_np()
    norm = _get_scipy_stats()
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def bond_floor_pv(
    par: float,
    coupon_rate: float,
    maturity_date: _dt,
    trade_date: _dt,
    discount_rate: float,
) -> float:
    """Present value of bond cash flows (annual coupons + bullet principal)."""
    remaining_years = max(0.0, (maturity_date - trade_date).days / 365.25)
    if remaining_years <= 0:
        return par
    annual_coupon = par * coupon_rate
    pv = 0.0
    t = 0.0
    while t + 1.0 <= remaining_years + 1e-6:
        t += 1.0
        pv += annual_coupon / ((1 + discount_rate) ** t)
    pv += par / ((1 + discount_rate) ** remaining_years)
    return pv


def compute_cb_theoretical(
    stock_price: float,
    conv_price: float,
    time_to_maturity: float,
    risk_free: float,
    credit_spread: float,
    hist_vol: float,
    par: float,
    coupon_rate: float,
    maturity_date: _dt,
    trade_date: _dt,
) -> dict[str, float]:
    """Compute theoretical CB value: bond_floor + option_value."""
    discount_rate = risk_free + credit_spread
    bf = bond_floor_pv(par, coupon_rate, maturity_date, trade_date, discount_rate)
    conversion_ratio = par / conv_price if conv_price > 0 else 0.0
    strike = conv_price
    opt_val = conversion_ratio * bs_call_price(
        stock_price, strike, time_to_maturity, risk_free, hist_vol
    )
    theoretical = bf + opt_val
    return {
        "bond_floor": round(bf, 6),
        "option_value": round(opt_val, 6),
        "theoretical": round(theoretical, 6),
        "credit_spread_used": round(credit_spread, 6),
        "hist_vol_used": round(hist_vol, 6),
    }


def compute_rolling_volatility(
    stk_df: _dt, lookback: int = 60, min_periods: int = 20
):
    """Compute rolling annualised historical volatility from stock prices.

    Uses log returns, annualised by sqrt(252). Returns DataFrame with
    columns [ts_code, trade_date, hist_vol].
    """
    pd = _get_pd()
    np = _get_np()
    stk = stk_df.copy()
    stk["trade_date"] = pd.to_datetime(stk["trade_date"])
    stk = stk.sort_values(["ts_code", "trade_date"])
    stk["log_ret"] = stk.groupby("ts_code")["close"].transform(
        lambda s: np.log(s / s.shift(1))
    )
    stk["hist_vol"] = (
        stk.groupby("ts_code")["log_ret"]
        .transform(lambda s: s.rolling(lookback, min_periods=min_periods).std())
        * np.sqrt(252)
    )
    result = stk[["ts_code", "trade_date", "hist_vol"]].copy()
    result["hist_vol"] = result["hist_vol"].fillna(0.25)
    return result


def calibrate_credit_spread(
    market_price: float,
    stock_price: float,
    conv_price: float,
    time_to_maturity: float,
    risk_free: float,
    hist_vol: float,
    par: float,
    coupon_rate: float,
    maturity_date: _dt,
    trade_date: _dt,
    base_spread: float,
) -> float:
    """Calibrate credit spread so that theoretical_value approx = market_price.

    Uses Brent's method. Falls back to base_spread on failure.
    """
    brentq = _get_scipy_opt()
    conversion_ratio = par / conv_price if conv_price > 0 else 0.0

    def residual(spread: float) -> float:
        disc = risk_free + spread
        bf = bond_floor_pv(par, coupon_rate, maturity_date, trade_date, disc)
        opt = conversion_ratio * bs_call_price(
            stock_price, conv_price, time_to_maturity, risk_free, hist_vol
        )
        return (bf + opt) - market_price

    lo, hi = 0.001, 0.50
    try:
        flo = residual(lo)
        fhi = residual(hi)
        if flo * fhi > 0:
            tv_base = residual(base_spread) + market_price
            diff_pct = abs(tv_base - market_price) / max(market_price, 1.0)
            if diff_pct < 0.20:
                return base_spread
            direction = 1.0 if residual(base_spread) > 0 else -1.0
            return base_spread * (1.0 + direction * 0.3)
        implied = brentq(residual, lo, hi, xtol=1e-6, maxiter=50)
        return float(implied)
    except (ValueError, RuntimeError):
        return base_spread


def compute_refined_value_gaps(
    cb_daily: _dt,
    cb_basic: _dt,
    stk_daily: _dt,
    hist_vol_df: _dt,
):
    """Compute refined theoretical values and value gaps for all CBs.

    Returns DataFrame with:
      trade_date, ts_code, close, refined_theoretical, refined_bond_floor,
      refined_option_value, refined_gap, base_spread, calibrated_spread, hist_vol
    """
    pd = _get_pd()
    np = _get_np()

    basic_map: dict[str, dict[str, Any]] = {}
    for _, row in cb_basic.iterrows():
        ts = str(row["ts_code"])
        basic_map[ts] = {
            "stk_code": str(row.get("stk_code", "")),
            "conv_price": float(row.get("conv_price", 100.0)),
            "maturity_date": pd.Timestamp(str(row.get("maturity_date", "20991231"))),
            "rating": row.get("rating", ""),
            "coupon_rate": float(row.get("coupon_rate", 0.02)),
            "par_value": float(row.get("par_value", 100.0)),
        }

    cb_daily = cb_daily.copy()
    cb_daily["trade_date"] = pd.to_datetime(cb_daily["trade_date"])

    stk_map: dict[tuple[str, _dt], float] = {}
    stk_daily_copy = stk_daily.copy()
    stk_daily_copy["trade_date"] = pd.to_datetime(stk_daily_copy["trade_date"])
    for _, row in stk_daily_copy.iterrows():
        key = (str(row["stk_code"]), row["trade_date"])
        stk_map[key] = float(row["close"])

    vol_map: dict[tuple[str, _dt], float] = {}
    for _, row in hist_vol_df.iterrows():
        key = (str(row["ts_code"]), row["trade_date"])
        vol_map[key] = float(row["hist_vol"])

    results: list[dict[str, Any]] = []
    unique_codes = cb_daily["ts_code"].unique()

    for ts_code in unique_codes:
        if ts_code not in basic_map:
            continue
        info = basic_map[ts_code]
        stk_code = info["stk_code"]
        conv_price = info["conv_price"]
        maturity_date = info["maturity_date"]
        rating = info["rating"]
        coupon_rate = info["coupon_rate"]
        par_value = info["par_value"]
        base_spread = rating_to_spread(rating)

        cb_sub = cb_daily[cb_daily["ts_code"] == ts_code].sort_values("trade_date")

        prev_calibrated_spread: float | None = None

        for _, row in cb_sub.iterrows():
            trade_date = row["trade_date"]
            market_close = float(row["close"])
            ttm = max(0.0, (maturity_date - trade_date).days / 365.25)
            stock_price = stk_map.get((stk_code, trade_date), np.nan)
            hist_vol = vol_map.get((stk_code, trade_date), 0.25)

            if ttm <= 0 or pd.isna(stock_price) or stock_price <= 0:
                disc = RISK_FREE_RATE + base_spread
                bf = bond_floor_pv(par_value, coupon_rate, maturity_date, trade_date, disc)
                results.append({
                    "trade_date": trade_date,
                    "ts_code": ts_code,
                    "close": market_close,
                    "refined_theoretical": round(bf, 6),
                    "refined_bond_floor": round(bf, 6),
                    "refined_option_value": 0.0,
                    "refined_gap": round(market_close - bf, 6),
                    "base_spread": round(base_spread, 6),
                    "calibrated_spread": round(base_spread, 6),
                    "hist_vol": round(hist_vol, 6),
                })
                continue

            blended_spread = (
                0.3 * base_spread + 0.7 * prev_calibrated_spread
                if prev_calibrated_spread is not None
                else base_spread
            )

            calibrated = calibrate_credit_spread(
                market_close, stock_price, conv_price, ttm,
                RISK_FREE_RATE, hist_vol, par_value, coupon_rate,
                maturity_date, trade_date, base_spread,
            )

            final_spread = 0.3 * base_spread + 0.7 * calibrated
            tv_final = compute_cb_theoretical(
                stock_price, conv_price, ttm, RISK_FREE_RATE,
                final_spread, hist_vol, par_value, coupon_rate,
                maturity_date, trade_date,
            )

            prev_calibrated_spread = calibrated
            gap = market_close - tv_final["theoretical"]

            results.append({
                "trade_date": trade_date,
                "ts_code": ts_code,
                "close": market_close,
                "refined_theoretical": tv_final["theoretical"],
                "refined_bond_floor": tv_final["bond_floor"],
                "refined_option_value": tv_final["option_value"],
                "refined_gap": round(gap, 6),
                "base_spread": round(base_spread, 6),
                "calibrated_spread": round(final_spread, 6),
                "hist_vol": round(hist_vol, 6),
            })

    return pd.DataFrame(results)


def simulate_strategy(
    gap_df: _dt, gap_col: str = "refined_gap", price_col: str = "close"
) -> dict[str, Any]:
    """Simulate baseline value-gap switch strategy.

    Entry: gap > 0 and flat -> enter
    Exit:  gap <= 0 -> exit
    PnL: (exit_gap - entry_gap) * position_value / entry_price

    Returns dict with total_pnl, trade_count, win_rate, max_drawdown,
    sharpe_ratio, trades list.
    """
    pd = _get_pd()
    np = _get_np()

    df = gap_df.copy()
    df = df.sort_values(["ts_code", "trade_date"])

    trades: list[dict[str, Any]] = []
    daily_equity: list[dict[str, Any]] = []
    total_equity = 0.0

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_price = 0.0
        entry_date: Any = None

        for _, row in grp.iterrows():
            gap = float(row[gap_col])
            price = float(row[price_col])
            td = row["trade_date"]

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_price = price
                entry_date = td
                continue

            if in_position and gap <= 0:
                pnl = (gap - entry_gap_val) / max(entry_price, 0.01)
                total_equity += pnl
                hold_days = (td - entry_date).days if entry_date else 0
                trades.append({
                    "stock": stock,
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(td.date()),
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": round(pnl, 6),
                    "hold_days": hold_days,
                })
                daily_equity.append({
                    "date": td,
                    "equity": round(total_equity, 6),
                })
                in_position = False
                entry_gap_val = 0.0
                entry_price = 0.0
                entry_date = None

        # Force-close at end of data for current stock
        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row[gap_col])
            final_price = float(last_row[price_col])
            pnl = (final_gap - entry_gap_val) / max(final_price, 0.01)
            total_equity += pnl
            hold_days = (last_row["trade_date"] - entry_date).days if entry_date else 0
            trades.append({
                "stock": stock,
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last_row["trade_date"].date()),
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 6),
                "hold_days": hold_days,
            })

    if not trades:
        return {
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "trades": [],
        }

    trade_count = len(trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / trade_count if trade_count > 0 else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    # Max drawdown from equity curve
    if daily_equity:
        eq = pd.DataFrame(daily_equity).sort_values("date")
        eq["peak"] = eq["equity"].cummax()
        eq["dd"] = (eq["equity"] - eq["peak"]) / (eq["peak"].abs() + 1e-10)
        max_dd = float(eq["dd"].min())
    else:
        max_dd = 0.0

    # Sharpe (annualised, using daily equity changes)
    if daily_equity:
        eq = pd.DataFrame(daily_equity).sort_values("date")
        eq["daily_ret"] = eq["equity"].diff()
        valid_rets = eq["daily_ret"].dropna()
        if valid_rets.std() > 0:
            sharpe = float(valid_rets.mean() / valid_rets.std() * np.sqrt(252))
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    return {
        "total_pnl": round(float(total_equity), 6),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_drawdown": round(max_dd, 6),
        "sharpe_ratio": round(sharpe, 4),
        "trades": trades,
    }


def simulate_baseline_strategy(gap_df: _dt) -> dict[str, Any]:
    """Simulate using existing 'value_gap_amount' as gap signal."""
    return simulate_strategy(gap_df, gap_col="value_gap_amount", price_col="close")


def _plain(value: Any) -> Any:
    """Recursively convert numpy types to plain Python types."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _gatekeeper_before_run(output_dir: Path) -> None:
    """Initialize GateKeeper compliance for this executor run."""
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "description": "Daily CB market prices.",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic terms, ratings, conversion prices.",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices for vol computation.",
            },
            {
                "path": "data/cb_warehouse/credit_spreads.parquet",
                "description": "Credit spread data (CDS or synthetic spreads).",
            },
            {
                "path": (
                    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
                    "daily_value_gap_amounts.parquet"
                ),
                "description": "Existing value gaps for baseline comparison.",
            },
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument("--train-start", required=True, help="Train period start YYYYMMDD")
    parser.add_argument("--train-end", required=True, help="Train period end YYYYMMDD")
    parser.add_argument("--test-start", required=True, help="Test period start YYYYMMDD")
    parser.add_argument("--test-end", required=True, help="Test period end YYYYMMDD")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    args = parser.parse_args()

    _ensure_yaml_np_reprs()
    pd = _get_pd()
    np = _get_np()
    yaml = _get_yaml()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    data_root = Path(args.data_root)
    warehouse = _REPO_ROOT / "data" / "cb_warehouse"

    # Load data
    try:
        cb_daily = pd.read_parquet(warehouse / "cb_daily.parquet")
        cb_basic = pd.read_parquet(warehouse / "cb_basic.parquet")
        stk_daily = pd.read_parquet(warehouse / "stk_daily_qfq.parquet")
        existing_gaps = pd.read_parquet(
            _REPO_ROOT
            / "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17"
            / "daily_value_gap_amounts.parquet"
        )
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[new_valuation] FATAL load_data: {exc}", flush=True)
        return 1

    existing_gaps["trade_date"] = pd.to_datetime(existing_gaps["trade_date"])

    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    # Compute historical volatility
    print("[new_valuation] Computing rolling historical volatility...", flush=True)
    hist_vol_df = compute_rolling_volatility(stk_daily, lookback=60, min_periods=20)

    # Compute refined theoretical values
    print("[new_valuation] Computing refined theoretical values...", flush=True)
    refined_df = compute_refined_value_gaps(cb_daily, cb_basic, stk_daily, hist_vol_df)

    if len(refined_df) == 0:
        diag = {"error": "Refined value gaps DataFrame is empty.", "step": "compute_refined"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        return 1

    # Filter periods
    refined_train = refined_df[
        (refined_df["trade_date"] >= train_start) & (refined_df["trade_date"] <= train_end)
    ].copy()
    refined_test = refined_df[
        (refined_df["trade_date"] >= test_start) & (refined_df["trade_date"] <= test_end)
    ].copy()
    refined_2020 = refined_train[refined_train["trade_date"].dt.year == 2020].copy()

    existing_train = existing_gaps[
        (existing_gaps["trade_date"] >= train_start) & (existing_gaps["trade_date"] <= train_end)
    ].copy()
    existing_test = existing_gaps[
        (existing_gaps["trade_date"] >= test_start) & (existing_gaps["trade_date"] <= test_end)
    ].copy()
    existing_2020 = existing_train[existing_train["trade_date"].dt.year == 2020].copy()

    # Simulate baseline (existing gaps)
    print("[new_valuation] Simulating baseline strategy (existing gaps)...", flush=True)
    baseline_train = simulate_baseline_strategy(existing_train)
    baseline_test = simulate_baseline_strategy(existing_test)
    baseline_2020 = simulate_baseline_strategy(existing_2020)

    # Simulate refined gaps
    print("[new_valuation] Simulating refined strategy (new gaps)...", flush=True)
    refined_train_res = simulate_strategy(refined_train)
    refined_test_res = simulate_strategy(refined_test)
    refined_2020_res = simulate_strategy(refined_2020)

    # Excess returns
    def excess(s: dict[str, Any], b: dict[str, Any]) -> float:
        return round(s["total_pnl"] - b["total_pnl"], 6)

    excess_train = excess(refined_train_res, baseline_train)
    excess_test = excess(refined_test_res, baseline_test)
    excess_2020 = excess(refined_2020_res, baseline_2020)

    # Adoption criteria
    checks: dict[str, bool] = {}
    checks["test_excess_improved"] = excess_test > 0.02
    checks["test_dd_not_worse"] = (
        refined_test_res["max_drawdown"] >= baseline_test["max_drawdown"]
        if baseline_test["max_drawdown"] < 0
        else True
    )
    checks["train_dd_not_worse"] = (
        refined_train_res["max_drawdown"] >= baseline_train["max_drawdown"]
        if baseline_train["max_drawdown"] < 0
        else True
    )
    checks["drawdown_2020_not_worse"] = (
        refined_2020_res["max_drawdown"] >= baseline_2020["max_drawdown"]
        if baseline_2020["max_drawdown"] < 0
        else True
    )
    checks["test_sharpe_ok"] = refined_test_res["sharpe_ratio"] >= 0.5

    adoption_pass = all([
        checks.get("test_excess_improved", False),
        checks.get("test_dd_not_worse", True),
        checks.get("train_dd_not_worse", True),
        checks.get("test_sharpe_ok", True),
    ])

    # Build period metrics
    def _period_metrics(ref: dict[str, Any], base: dict[str, Any], ex: float) -> dict[str, Any]:
        return {
            "refined": {
                "total_pnl": ref["total_pnl"],
                "trade_count": ref["trade_count"],
                "win_rate": ref["win_rate"],
                "avg_win": ref["avg_win"],
                "avg_loss": ref["avg_loss"],
                "max_drawdown": ref["max_drawdown"],
                "sharpe_ratio": ref["sharpe_ratio"],
            },
            "baseline": {
                "total_pnl": base["total_pnl"],
                "trade_count": base["trade_count"],
                "win_rate": base["win_rate"],
                "avg_win": base["avg_win"],
                "avg_loss": base["avg_loss"],
                "max_drawdown": base["max_drawdown"],
                "sharpe_ratio": base["sharpe_ratio"],
            },
            "excess_return": ex,
        }

    periods_metrics = {
        "train": _period_metrics(refined_train_res, baseline_train, excess_train),
        "test": _period_metrics(refined_test_res, baseline_test, excess_test),
        "validate_2020": _period_metrics(refined_2020_res, baseline_2020, excess_2020),
    }

    # Write summary.json
    summary: dict[str, Any] = {
        "adoption_pass": adoption_pass,
        "checks": checks,
        "periods": periods_metrics,
        "config": {
            "train_start": args.train_start,
            "train_end": args.train_end,
            "test_start": args.test_start,
            "test_end": args.test_end,
        },
        "refined_data_summary": {
            "total_rows": len(refined_df),
            "train_rows": len(refined_train),
            "test_rows": len(refined_test),
            "avg_gap_train": round(float(refined_train["refined_gap"].mean()), 6) if len(refined_train) > 0 else 0.0,
            "avg_gap_test": round(float(refined_test["refined_gap"].mean()), 6) if len(refined_test) > 0 else 0.0,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Write report.yaml
    report = {
        "proposal_id": "cb_arb_value_gap_switch_new_valuation_formula_v1",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "new_valuation_executor",
        "adoption_pass": adoption_pass,
        "checks": checks,
        "periods": periods_metrics,
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True), encoding="utf-8"
    )

    # Write l4_ack.yaml
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": (
            "Refined CB valuation with dynamic credit spread and historical volatility. "
            f"adoption_pass={adoption_pass}, test_excess={excess_test}, "
            f"test_sharpe={refined_test_res['sharpe_ratio']}"
        ),
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8"
    )

    # Write diagnostic.yaml
    diagnostic = {
        "warnings": [],
        "errors": [],
        "data_rows": {
            "refined_total": len(refined_df),
            "train": len(refined_train),
            "test": len(refined_test),
            "validate_2020": len(refined_2020),
        },
        "check_results": checks,
        "adoption_pass": adoption_pass,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True), encoding="utf-8"
    )

    print(
        f"[new_valuation] adoption_pass={adoption_pass} "
        f"excess_train={excess_train} excess_test={excess_test} "
        f"excess_2020={excess_2020} "
        f"sharpe_test={refined_test_res['sharpe_ratio']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
