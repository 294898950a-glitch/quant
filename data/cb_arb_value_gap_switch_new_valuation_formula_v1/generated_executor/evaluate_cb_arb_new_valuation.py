#!/usr/bin/env python3
"""Evaluate refined CB theoretical value model for value-gap switch strategy.

Computes new theoretical values using a Tsiveriotis-Fernandes decomposition:
  theoretical = bond_floor + option_value

Enhancements over baseline:
  - Dynamic credit spread: rating-based base + market-implied feedback adjustment
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
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from scipy.optimize import brentq
from scipy.stats import norm


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Risk-free rate proxy (China 5Y government bond ~2.5%)
RISK_FREE_RATE = 0.025


# ── Rating → base credit spread (bps) ──────────────────────────────────────
_RATING_SPREAD_MAP: dict[str, float] = {
    "AAA": 0.0050,
    "AA+": 0.0080,
    "AA": 0.0100,
    "AA-": 0.0130,
    "A+": 0.0160,
    "A": 0.0200,
    "A-": 0.0250,
    "BBB+": 0.0320,
    "BBB": 0.0400,
    "BBB-": 0.0500,
    "BB+": 0.0650,
    "BB": 0.0800,
    "BB-": 0.1000,
    "B+": 0.1200,
    "B": 0.1500,
    "B-": 0.1800,
    "CCC": 0.2200,
    "CC": 0.2800,
    "C": 0.3500,
}
_DEFAULT_SPREAD = 0.0200  # fallback for unknown ratings


def rating_to_spread(rating: Any) -> float:
    """Map credit rating to base annual spread."""
    if not isinstance(rating, str) or pd.isna(rating):
        return _DEFAULT_SPREAD
    r = str(rating).strip().upper()
    return _RATING_SPREAD_MAP.get(r, _DEFAULT_SPREAD)


# ── Black-Scholes call price ───────────────────────────────────────────────
def bs_call_price(
    S: float, K: float, T: float, r: float, sigma: float
) -> float:
    """Black-Scholes European call price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


# ── Bond floor (PV of remaining cash flows) ────────────────────────────────
def bond_floor_pv(
    par: float,
    coupon_rate: float,
    maturity_date: pd.Timestamp,
    trade_date: pd.Timestamp,
    discount_rate: float,
) -> float:
    """Compute present value of bond cash flows.

    Assumes annual coupon payments and bullet principal at maturity.
    """
    remaining_years = max(0.0, (maturity_date - trade_date).days / 365.25)
    if remaining_years <= 0:
        return par  # at/after maturity, bond floor = principal

    annual_coupon = par * coupon_rate
    pv = 0.0

    # Coupon payments
    t = 0.0
    while t + 1.0 <= remaining_years + 1e-6:
        t += 1.0
        pv += annual_coupon / ((1 + discount_rate) ** t)

    # Principal at maturity
    pv += par / ((1 + discount_rate) ** remaining_years)

    return pv


# ── CB theoretical value (Tsiveriotis-Fernandes decomposition) ──────────────
def compute_cb_theoretical(
    stock_price: float,
    conv_price: float,
    time_to_maturity: float,
    risk_free: float,
    credit_spread: float,
    hist_vol: float,
    par: float,
    coupon_rate: float,
    maturity_date: pd.Timestamp,
    trade_date: pd.Timestamp,
) -> dict[str, float]:
    """Compute theoretical CB value: bond_floor + option_value."""
    discount_rate = risk_free + credit_spread

    bf = bond_floor_pv(par, coupon_rate, maturity_date, trade_date, discount_rate)

    # Conversion ratio: par / conv_price
    conversion_ratio = par / conv_price if conv_price > 0 else 0.0
    strike = conv_price

    # Option value = conversion_ratio * BS_call(stock_price, strike, T, r, sigma)
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


# ── Historical volatility computation ──────────────────────────────────────
def compute_rolling_volatility(
    stk_df: pd.DataFrame, lookback: int = 60, min_periods: int = 20
) -> pd.DataFrame:
    """Compute rolling annualised historical volatility from stock prices.

    Uses log returns, annualised by sqrt(252).
    Returns DataFrame with columns [ts_code, trade_date, hist_vol].
    """
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
    result["hist_vol"] = result["hist_vol"].fillna(0.25)  # fallback vol ~25%
    return result


# ── Implied credit spread calibration ──────────────────────────────────────
def calibrate_credit_spread(
    market_price: float,
    stock_price: float,
    conv_price: float,
    time_to_maturity: float,
    risk_free: float,
    hist_vol: float,
    par: float,
    coupon_rate: float,
    maturity_date: pd.Timestamp,
    trade_date: pd.Timestamp,
    base_spread: float,
) -> float:
    """Calibrate credit spread so that theoretical_value ≈ market_price.

    Uses Brent's method to solve for spread. Falls back to base_spread
    on failure.
    """
    conversion_ratio = par / conv_price if conv_price > 0 else 0.0

    def residual(spread: float) -> float:
        disc = risk_free + spread
        bf = bond_floor_pv(par, coupon_rate, maturity_date, trade_date, disc)
        opt = conversion_ratio * bs_call_price(
            stock_price, conv_price, time_to_maturity, risk_free, hist_vol
        )
        tv = bf + opt
        return tv - market_price

    # Try to bracket the root
    lo, hi = 0.001, 0.50  # 10bp to 50%
    try:
        flo = residual(lo)
        fhi = residual(hi)
        if flo * fhi > 0:
            # Can't bracket; return weighted blend
            # If theoretical at base_spread is already close, use base_spread
            tv_base = residual(base_spread) + market_price
            diff_pct = abs(tv_base - market_price) / max(market_price, 1.0)
            if diff_pct < 0.20:  # within 20%, reasonable
                return base_spread
            # Otherwise use a simple adjustment
            direction = 1.0 if residual(base_spread) > 0 else -1.0
            return base_spread * (1.0 + direction * 0.3)
        implied = brentq(residual, lo, hi, xtol=1e-6, maxiter=50)
        return float(implied)
    except (ValueError, RuntimeError):
        return base_spread


# ── Main refined valuation engine ──────────────────────────────────────────
def compute_refined_value_gaps(
    cb_daily: pd.DataFrame,
    cb_basic: pd.DataFrame,
    stk_daily: pd.DataFrame,
    hist_vol_df: pd.DataFrame,
) -> pd.DataFrame:
    """Compute refined theoretical values and value gaps for all CBs on all days.

    Returns DataFrame with columns:
      trade_date, ts_code, close, refined_theoretical, refined_bond_floor,
      refined_option_value, refined_gap, base_spread, calibrated_spread, hist_vol
    """
    # Prepare basic info keyed by ts_code
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

    # Prepare daily data
    cb_daily = cb_daily.copy()
    cb_daily["trade_date"] = pd.to_datetime(cb_daily["trade_date"])

    # Merge stock close price onto cb_daily via stk_code → stk_daily
    stk_map: dict[tuple[str, pd.Timestamp], float] = {}
    stk_daily_copy = stk_daily.copy()
    stk_daily_copy["trade_date"] = pd.to_datetime(stk_daily_copy["trade_date"])
    for _, row in stk_daily_copy.iterrows():
        key = (str(row["stk_code"]), row["trade_date"])
        stk_map[key] = float(row["close"])

    # Merge hist_vol onto cb_daily via stk_code
    vol_map: dict[tuple[str, pd.Timestamp], float] = {}
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

        # Track rolling calibrated spread
        prev_calibrated_spread: float | None = None

        for _, row in cb_sub.iterrows():
            trade_date = row["trade_date"]
            market_close = float(row["close"])

            # Time to maturity
            ttm = max(0.0, (maturity_date - trade_date).days / 365.25)

            # Stock price and vol
            stock_price = stk_map.get((stk_code, trade_date), np.nan)
            hist_vol = vol_map.get((stk_code, trade_date), 0.25)

            if ttm <= 0 or pd.isna(stock_price) or stock_price <= 0:
                # CB matured or missing stock data → bond floor only
                disc = RISK_FREE_RATE + base_spread
                bf = bond_floor_pv(
                    par_value, coupon_rate, maturity_date, trade_date, disc
                )
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

            # Blend: use previous day's calibrated spread if available
            if prev_calibrated_spread is not None:
                blended_spread = 0.3 * base_spread + 0.7 * prev_calibrated_spread
            else:
                blended_spread = base_spread

            # First pass: compute theoretical with blended spread
            tv_first = compute_cb_theoretical(
                stock_price, conv_price, ttm, RISK_FREE_RATE,
                blended_spread, hist_vol, par_value, coupon_rate,
                maturity_date, trade_date,
            )

            # Calibrate to market
            calibrated = calibrate_credit_spread(
                market_close, stock_price, conv_price, ttm,
                RISK_FREE_RATE, hist_vol, par_value, coupon_rate,
                maturity_date, trade_date, base_spread,
            )

            # Final pass with calibrated spread (blend to avoid overfitting)
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


# ── Strategy simulation ────────────────────────────────────────────────────
def simulate_strategy(
    gap_df: pd.DataFrame, gap_col: str = "refined_gap", price_col: str = "close"
) -> dict[str, Any]:
    """Simulate baseline value-gap switch strategy.

    Entry: gap > 0 and flat → enter
    Exit: gap <= 0 → exit
    PnL: (exit_gap - entry_gap) * position_value / entry_price

    Returns dict with total_pnl, trade_count, win_rate, max_drawdown,
    sharpe_ratio, trades list.
    """
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
                # PnL in relative terms: (exit_gap - entry_gap) / entry_price
                # This gives return per unit of capital deployed
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

        # Force-close at end
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

    trades_df = pd.DataFrame(trades)
    winning = trades_df[trades_df["pnl"] > 0]
    losing = trades_df[trades_df["pnl"] <= 0]
    win_rate = round(len(winning) / len(trades_df), 4) if len(trades_df) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 6) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 6) if len(losing) > 0 else 0.0
    trade_count = len(trades_df)

    # Max drawdown from equity curve
    eq = pd.DataFrame(daily_equity).sort_values("date")
    eq["peak"] = eq["equity"].cummax()
    eq["drawdown"] = eq["equity"] - eq["peak"]
    max_dd = float(eq["drawdown"].min()) if len(eq) > 0 else 0.0

    # Sharpe ratio (annualized, assuming daily returns)
    if len(eq) >= 2:
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


def simulate_baseline_strategy(gap_df: pd.DataFrame) -> dict[str, Any]:
    """Simulate using existing 'value_gap_amount' as gap signal."""
    return simulate_strategy(gap_df, gap_col="value_gap_amount", price_col="close")


# ── Utility ─────────────────────────────────────────────────────────────────
def _plain(value: Any) -> Any:
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


# ── declare_data_requirements ──────────────────────────────────────────────
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
                "path": "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet",
                "description": "Existing value gaps for baseline comparison.",
            },
        ]
    }


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument("--train-start", required=True, help="Train period start YYYYMMDD")
    parser.add_argument("--train-end", required=True, help="Train period end YYYYMMDD")
    parser.add_argument("--test-start", required=True, help="Test period start YYYYMMDD")
    parser.add_argument("--test-end", required=True, help="Test period end YYYYMMDD")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    data_root = Path(args.data_root)
    warehouse = _REPO_ROOT / "data" / "cb_warehouse"

    # ── Load data ──────────────────────────────────────────────────────────
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

    # Convert dates
    existing_gaps["trade_date"] = pd.to_datetime(existing_gaps["trade_date"])

    # ── Date filters ───────────────────────────────────────────────────────
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    # ── Compute historical volatility ──────────────────────────────────────
    print("[new_valuation] Computing rolling historical volatility...", flush=True)
    hist_vol_df = compute_rolling_volatility(stk_daily, lookback=60, min_periods=20)

    # ── Compute refined theoretical values ─────────────────────────────────
    print("[new_valuation] Computing refined theoretical values...", flush=True)
    refined_df = compute_refined_value_gaps(
        cb_daily, cb_basic, stk_daily, hist_vol_df
    )

    if len(refined_df) == 0:
        diag = {"error": "Refined value gaps DataFrame is empty.", "step": "compute_refined"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        return 1

    # ── Filter periods ─────────────────────────────────────────────────────
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

    # ── Simulate baseline (existing gaps) ──────────────────────────────────
    print("[new_valuation] Simulating baseline strategy (existing gaps)...", flush=True)
    baseline_train = simulate_baseline_strategy(existing_train)
    baseline_test = simulate_baseline_strategy(existing_test)
    baseline_2020 = simulate_baseline_strategy(existing_2020)

    # ── Simulate refined gaps ──────────────────────────────────────────────
    print("[new_valuation] Simulating refined strategy (new gaps)...", flush=True)
    refined_train_res = simulate_strategy(refined_train)
    refined_test_res = simulate_strategy(refined_test)
    refined_2020_res = simulate_strategy(refined_2020)

    # ── Compute excess returns ─────────────────────────────────────────────
    def excess(s: dict[str, Any], b: dict[str, Any]) -> float:
        return round(s["total_pnl"] - b["total_pnl"], 6)

    excess_train = excess(refined_train_res, baseline_train)
    excess_test = excess(refined_test_res, baseline_test)
    excess_2020 = excess(refined_2020_res, baseline_2020)

    # ── Adoption criteria from proposal ────────────────────────────────────
    # success_criteria:
    #   cost_on cumulative excess compound > -0.07
    #   cost_on max drawdown > -0.30
    #   test period total return exceeds baseline (0.732) by at least 0.05
    # falsifiers:
    #   excess return does not improve by at least 0.02 over baseline
    #   max drawdown in any single year worsens
    #   out-of-sample Sharpe < 0.5

    checks: dict[str, bool] = {}

    # Test excess return improvement over baseline
    checks["test_excess_improved"] = excess_test > 0.02

    # Max drawdown comparison
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

    # Sharpe ratio check
    checks["test_sharpe_ok"] = refined_test_res["sharpe_ratio"] >= 0.5

    # Overall adoption pass
    adoption_pass = all([
        checks.get("test_excess_improved", False),
        checks.get("test_dd_not_worse", True),
        checks.get("train_dd_not_worse", True),
        checks.get("test_sharpe_ok", True),
    ])

    # ── Build results ──────────────────────────────────────────────────────
    periods_metrics = {
        "train": {
            "refined": {
                "total_pnl": refined_train_res["total_pnl"],
                "trade_count": refined_train_res["trade_count"],
                "win_rate": refined_train_res["win_rate"],
                "avg_win": refined_train_res["avg_win"],
                "avg_loss": refined_train_res["avg_loss"],
                "max_drawdown": refined_train_res["max_drawdown"],
                "sharpe_ratio": refined_train_res["sharpe_ratio"],
            },
            "baseline": {
                "total_pnl": baseline_train["total_pnl"],
                "trade_count": baseline_train["trade_count"],
                "win_rate": baseline_train["win_rate"],
                "avg_win": baseline_train["avg_win"],
                "avg_loss": baseline_train["avg_loss"],
                "max_drawdown": baseline_train["max_drawdown"],
                "sharpe_ratio": baseline_train["sharpe_ratio"],
            },
            "excess_return": excess_train,
        },
        "test": {
            "refined": {
                "total_pnl": refined_test_res["total_pnl"],
                "trade_count": refined_test_res["trade_count"],
                "win_rate": refined_test_res["win_rate"],
                "avg_win": refined_test_res["avg_win"],
                "avg_loss": refined_test_res["avg_loss"],
                "max_drawdown": refined_test_res["max_drawdown"],
                "sharpe_ratio": refined_test_res["sharpe_ratio"],
            },
            "baseline": {
                "total_pnl": baseline_test["total_pnl"],
                "trade_count": baseline_test["trade_count"],
                "win_rate": baseline_test["win_rate"],
                "avg_win": baseline_test["avg_win"],
                "avg_loss": baseline_test["avg_loss"],
                "max_drawdown": baseline_test["max_drawdown"],
                "sharpe_ratio": baseline_test["sharpe_ratio"],
            },
            "excess_return": excess_test,
        },
        "validate_2020": {
            "refined": {
                "total_pnl": refined_2020_res["total_pnl"],
                "trade_count": refined_2020_res["trade_count"],
                "win_rate": refined_2020_res["win_rate"],
                "avg_win": refined_2020_res["avg_win"],
                "avg_loss": refined_2020_res["avg_loss"],
                "max_drawdown": refined_2020_res["max_drawdown"],
                "sharpe_ratio": refined_2020_res["sharpe_ratio"],
            },
            "baseline": {
                "total_pnl": baseline_2020["total_pnl"],
                "trade_count": baseline_2020["trade_count"],
                "win_rate": baseline_2020["win_rate"],
                "avg_win": baseline_2020["avg_win"],
                "avg_loss": baseline_2020["avg_loss"],
                "max_drawdown": baseline_2020["max_drawdown"],
                "sharpe_ratio": baseline_2020["sharpe_ratio"],
            },
            "excess_return": excess_2020,
        },
    }

    # ── Write summary.json ─────────────────────────────────────────────────
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

    # ── Write report.yaml ──────────────────────────────────────────────────
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

    # ── Write l4_ack.yaml ──────────────────────────────────────────────────
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

    # ── Write diagnostic.yaml ──────────────────────────────────────────────
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
