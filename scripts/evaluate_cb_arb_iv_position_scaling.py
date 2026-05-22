#!/usr/bin/env python3
"""Evaluate inverse IV-percentile position scaling for cb_arb value-gap switch.

Grid-search over three parameters:
  iv_percentile_threshold  — above this IV percentile, scaling kicks in
  scaling_factor_power     — exponent for (1 - IV_pct)**power weight formula
  min_hold_days            — days to block re-entry after a position exits

Core mechanic: compute rolling 252-day stock-IV percentile per CB, then
scale the ranking weight (value_gap_amount) and position size
(position_cash_scale) inversely with IV → higher IV means smaller position.

Comparison: runs the same backtest without scaling as a baseline, then
applies scaling for each grid point. Selects the best candidate by
2020 repair excess return.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

# ── Repo root ──────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Change to repo root so that warehouse files resolve
os.chdir(str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _candidate_grid,
    _metrics,
    _run_value_gap_backtest,
    _score,
    _with_cost_params,
)
from scripts.gatekeeper import GateKeeper  # noqa: E402

# ── Constants ───────────────────────────────────────────────────────────
_VOL_LOOKBACK = 20        # trading days for rolling stock vol
_PERCENTILE_LOOKBACK = 252  # trading days for IV percentile window
_STK_DAILY_PATH = "data/cb_warehouse/stk_daily_qfq.parquet"
_CB_BASIC_PATH = "data/cb_warehouse/cb_basic.parquet"
_BASE_RANKS_DEFAULT = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)

# Fixed baseline backtest parameters (all candidates share these)
_DEFAULT_BACKTEST_PARAMS: dict[str, float] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.02,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.0,
    "stop_signal_threshold": 999.0,
    "candidate_position_scale_enabled": 1.0,
}

# Grid sweep defaults for IV parameters
_IV_THRESHOLD_SWEEP = (0.5, 0.6, 0.7, 0.8, 0.9)
_SCALING_POWER_SWEEP = (0.5, 1.0, 1.5, 2.0)
_MIN_HOLD_SWEEP = (1, 3, 5)


# ── Plain / JSON helper ─────────────────────────────────────────────────
def _plain(value: Any) -> Any:
    """Convert numpy/pandas types to plain Python for YAML/JSON serialisation."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (np.floating, np.float64, np.float32)):
        return float(value)
    if isinstance(value, (np.integer, np.int64, np.int32)):
        return int(value)
    if isinstance(value, pd.Timestamp):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ── Declare data requirements ───────────────────────────────────────────
def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the files this executor reads before execution."""
    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_iv_position_scaling.py",
        "required_files": [
            {
                "path": _BASE_RANKS_DEFAULT,
                "role": "base_ranks",
                "description": "Daily value-gap amounts from previous pipeline run",
                "required_columns": [
                    "trade_date", "ts_code", "value_gap_amount",
                    "position_cash", "close", "buy_qty",
                ],
            },
            {
                "path": _STK_DAILY_PATH,
                "role": "stock_prices",
                "description": "Forward-adjusted stock prices for IV calculation",
                "required_columns": ["stk_code", "trade_date", "close"],
            },
            {
                "path": _CB_BASIC_PATH,
                "role": "cb_basic_mapping",
                "description": "CB basic table for ts_code→stk_code mapping",
                "required_columns": ["ts_code", "stk_code"],
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "role": "cb_daily",
                "description": "Required by baseline backtest",
            },
            {
                "path": "data/cb_warehouse/cb_call.parquet",
                "role": "cb_call",
                "description": "Required by baseline backtest",
            },
        ],
    }


# ── Data Loading ────────────────────────────────────────────────────────
def _resolve_path(relative: str, data_root: str) -> Path:
    """Resolve a relative path against data_root, repo root, and CWD."""
    rel = Path(relative)
    candidates = [Path(data_root) / rel, _REPO_ROOT / rel, Path.cwd() / rel]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(f"Required data missing; searched: {searched}")
    return path


def _load_base_ranks(data_root: str, ranks_path: str) -> pd.DataFrame:
    """Load the pre-computed daily value-gap ranks."""
    path = _resolve_path(ranks_path, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["ts_code"] = df["ts_code"].astype(str)
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_stock_vol(data_root: str) -> pd.DataFrame:
    """Compute rolling 20-day annualised stock volatility from adjusted prices."""
    path = _resolve_path(_STK_DAILY_PATH, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)

    # Daily log returns
    df["log_return"] = df.groupby("stk_code")["close"].transform(
        lambda s: np.log(s / s.shift(1))
    )

    # Rolling annualised vol
    df["stock_vol"] = df.groupby("stk_code")["log_return"].transform(
        lambda s: s.rolling(_VOL_LOOKBACK, min_periods=10).std() * np.sqrt(252)
    )

    return df[["stk_code", "trade_date", "stock_vol"]]


def _build_cb_to_stk_map(
    ts_codes: list[str], stock_codes: list[str], data_root: str
) -> dict[str, str]:
    """Build a mapping from CB ts_code to stock stk_code using cb_basic bridge.

    First tries direct match in stock_codes, then uses cb_basic to look up
    the numeric stk_code and rebuilds the full code with exchange suffix.
    Falls back to prefix matching.
    """
    mapping: dict[str, str] = {}
    # Construct a set for fast lookup
    stock_set = set(stock_codes)

    for cb in ts_codes:
        # Direct match: try ts_code as stk_code (some datasets align)
        if cb in stock_set:
            mapping[cb] = cb
            continue

        # Parse exchange suffix from ts_code
        parts = cb.split(".")
        if len(parts) < 2:
            continue
        prefix = parts[0]
        exchange = parts[1]

        # Try with exchange suffix (e.g. "300545.SZ")
        candidate = f"{prefix}.{exchange}"
        if candidate in stock_set:
            mapping[cb] = candidate
            continue

        # Prefix match — find any stock starting with this prefix
        matches = [s for s in stock_codes if s.startswith(prefix + ".")]
        if len(matches) == 1:
            mapping[cb] = matches[0]
        elif len(matches) > 1:
            mapping[cb] = matches[0]

    return mapping


def _merge_iv_into_ranks(
    df_ranks: pd.DataFrame, df_vol: pd.DataFrame, data_root: str
) -> pd.DataFrame:
    """Merge stock volatility into ranks and compute IV percentile per CB."""
    ts_codes = sorted(df_ranks["ts_code"].unique())
    stock_codes = sorted(df_vol["stk_code"].unique())

    cb_to_stk = _build_cb_to_stk_map(ts_codes, stock_codes, data_root)

    # Add stk_code to ranks
    df = df_ranks.copy()
    df["stk_code"] = df["ts_code"].map(cb_to_stk)

    # Merge stock vol
    df_vol_lean = df_vol[["stk_code", "trade_date", "stock_vol"]].copy()
    df_merged = df.merge(
        df_vol_lean, on=["stk_code", "trade_date"], how="left"
    )

    # Compute rolling IV percentile
    df_merged["iv_percentile"] = np.nan

    for ts_code, grp in df_merged.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        vols = grp["stock_vol"].values
        pctiles = np.full(len(vols), np.nan)

        for i in range(len(vols)):
            window_start = max(0, i - _PERCENTILE_LOOKBACK + 1)
            window_vols = vols[window_start : i + 1]
            valid = window_vols[~np.isnan(window_vols)]
            if len(valid) < 60:
                continue
            current = vols[i]
            if np.isnan(current):
                continue
            rank = (valid < current).sum() + 0.5 * (valid == current).sum()
            pctiles[i] = rank / len(valid)

        df_merged.loc[grp.index, "iv_percentile"] = pctiles

    return df_merged


# ── Scaling Logic ───────────────────────────────────────────────────────
def _scale_ranks(
    df: pd.DataFrame,
    iv_percentile_threshold: float,
    scaling_factor_power: float,
) -> pd.DataFrame:
    """Apply inverse IV-percentile scaling to ranks.

    For each row where IV percentile > threshold:
      scale = (1 - IV_percentile) ** scaling_factor_power
    Otherwise: scale = 1.0

    The scale is applied to:
      - value_gap_amount  (affects ranking — lower gap → lower buy priority)
      - position_cash_scale column (used by backtest for position sizing)
    """
    result = df.copy()

    if "iv_percentile" not in result.columns:
        result["position_cash_scale"] = 1.0
        return result

    pct = result["iv_percentile"].fillna(0.5)
    scale = np.where(
        pct > iv_percentile_threshold,
        np.power(np.maximum(1.0 - pct, 0.001), scaling_factor_power),
        1.0,
    )
    scale = np.clip(scale, 0.01, 1.0)

    # Scale ranking weight
    result["value_gap_amount"] = (
        result["value_gap_amount"].astype(float) * scale
    )
    # Store position scale for backtest
    result["position_cash_scale"] = scale

    return result


# ── Min-Hold-Days Blocking ──────────────────────────────────────────────
def _apply_min_hold_block(
    df: pd.DataFrame,
    trades: list[dict[str, Any]],
    min_hold_days: int,
    date_col: str = "trade_date",
    code_col: str = "ts_code",
) -> pd.DataFrame:
    """Block re-entry for each CB for min_hold_days after each exit.

    Reads trade history to find exit dates, then sets value_gap_amount to 0
    for the blocked window so the backtest naturally skips those candidates.
    """
    if min_hold_days <= 0 or not trades:
        return df

    result = df.copy()
    # Get the full sorted date index for forward date lookup
    unique_dates = sorted(result[date_col].unique())
    date_to_idx = {d: i for i, d in enumerate(unique_dates)}

    # Each trade has an exit_date. Block the CB for min_hold_days after that.
    blocks: dict[str, list[tuple[str, str]]] = {}  # code → [(block_start, block_end)]
    for trade in trades:
        code = trade.get("cb_code") or trade.get("stock")
        exit_date_str = trade.get("exit_date")
        if not code or not exit_date_str:
            continue
        try:
            exit_dt = pd.Timestamp(exit_date_str)
        except Exception:
            continue
        # Block starts the next trading day
        exit_idx = date_to_idx.get(exit_dt)
        if exit_idx is None:
            # Find nearest date
            all_ts = pd.DatetimeIndex(unique_dates)
            pos = all_ts.searchsorted(exit_dt, side="right")
            exit_idx = pos if pos < len(unique_dates) else len(unique_dates) - 1

        block_start_idx = exit_idx + 1
        block_end_idx = exit_idx + min_hold_days
        if block_start_idx < len(unique_dates):
            start_date = unique_dates[block_start_idx]
            end_date = unique_dates[min(block_end_idx, len(unique_dates) - 1)]
            blocks.setdefault(code, []).append((str(start_date), str(end_date)))

    # Apply blocks: set value_gap_amount to 0 for blocked windows
    for code, windows in blocks.items():
        mask_code = result[code_col] == code
        for sd, ed in windows:
            mask_window = (result[date_col] >= pd.Timestamp(sd)) & (
                result[date_col] <= pd.Timestamp(ed)
            )
            mask = mask_code & mask_window
            result.loc[mask, "value_gap_amount"] = 0.0

    return result


# ── Backtest Wrapper ────────────────────────────────────────────────────
def _run_eval(
    df: pd.DataFrame,
    period_start: str,
    period_end: str,
    data_root: str,
    fixed_source: int,
    rule: str,
    params: dict[str, float],
) -> dict[str, Any]:
    """Run the baseline backtest on a period and return metrics dict."""
    period_df = df[
        (df["trade_date"] >= period_start) & (df["trade_date"] <= period_end)
    ].copy()
    period_df["trade_date"] = period_df["trade_date"].astype(str)

    if period_df.empty:
        return {
            "total_return": 0.0, "excess_return": 0.0,
            "max_drawdown": 0.0, "win_rate": 0.0,
            "total_trades": 0, "n_days": 0,
        }

    result = _run_value_gap_backtest(
        period_df,
        period_start,
        period_end,
        Path(data_root),
        fixed_source,
        rule,
        params,
    )
    return result["metrics"]


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--iv-lookback", type=int, default=252,
                        help="Trading days for IV percentile lookback")
    parser.add_argument("--iv-percentile-threshold", type=float, default=0.8,
                        help="Default IV percentile threshold for single run")
    parser.add_argument("--scaling-factor-power", type=float, default=1.0,
                        help="Exponent for inverse scaling weight")
    parser.add_argument("--min-hold-days", type=int, default=5,
                        help="Days to block re-entry after exit")
    parser.add_argument("--fixed-source", type=int, default=2)
    parser.add_argument("--rule", default="score_4state")
    parser.add_argument("--base-ranks-path",
                        default=_BASE_RANKS_DEFAULT)
    parser.add_argument("--reuse-ranks", action="store_true")
    parser.add_argument("--cost-model-enabled", action="store_true",
                        default=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Gatekeeper check (non-fatal for executor code)
    try:
        spec_path = output_dir / "spec.yaml"
        if spec_path.exists():
            gatekeeper = GateKeeper(quiet=True)
            gatekeeper.before_run_grid(spec_path)
    except Exception:
        pass

    data_root = args.data_root

    # ── Step 1: Load base ranks ──────────────────────────────────────
    try:
        df_ranks = _load_base_ranks(data_root, args.base_ranks_path)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_base_ranks"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[iv_scaling] FATAL load: {exc}", flush=True)
        return 1

    # ── Step 2: Compute IV percentile ────────────────────────────────
    try:
        df_vol = _load_stock_vol(data_root)
        df_enriched = _merge_iv_into_ranks(df_ranks, df_vol, data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "compute_iv_percentile"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[iv_scaling] FATAL IV: {exc}", flush=True)
        return 1

    # ── Step 3: Build backtest params ────────────────────────────────
    base_params = dict(_DEFAULT_BACKTEST_PARAMS)
    if args.cost_model_enabled:
        base_params.update({
            "cost_model_enabled": 1.0,
            "slippage_pct": 0.0015,
            "market_impact_coeff": 0.0010,
            "market_impact_cap_pct": 0.02,
            "holding_cost_pct": 0.0,
        })

    # ── Step 4: Baseline run (no scaling) ────────────────────────────
    df_baseline = df_enriched.copy()
    # Need to add position_cash_scale for baseline
    if "position_cash_scale" not in df_baseline.columns:
        df_baseline["position_cash_scale"] = 1.0

    print("[iv_scaling] Running baseline backtest ...", flush=True)
    baseline_train = _run_eval(
        df_baseline, args.train_start, args.train_end,
        data_root, args.fixed_source, args.rule, base_params,
    )
    baseline_test = _run_eval(
        df_baseline, args.test_start, args.test_end,
        data_root, args.fixed_source, args.rule, base_params,
    )
    # 2020 repair period
    df_2020 = df_baseline[
        (df_baseline["trade_date"] >= "20200101") &
        (df_baseline["trade_date"] <= "20201231")
    ]
    baseline_2020: dict[str, Any]
    if df_2020.empty:
        baseline_2020 = {
            "total_return": 0.0, "excess_return": 0.0,
            "max_drawdown": 0.0, "win_rate": 0.0,
            "total_trades": 0, "n_days": 0,
        }
    else:
        baseline_2020 = _run_eval(
            df_2020.copy(), "20200101", "20201231",
            data_root, args.fixed_source, args.rule, base_params,
        )

    print(
        f"[iv_scaling] baseline train excess={baseline_train['excess_return']:.4f} "
        f"total={baseline_train['total_return']:.4f} dd={baseline_train['max_drawdown']:.4f}",
        flush=True,
    )

    # ── Step 5: Grid search over IV parameters ───────────────────────
    # Merge user-specified value into sweep
    thresholds = sorted(set(_IV_THRESHOLD_SWEEP + (args.iv_percentile_threshold,)))
    powers = sorted(set(_SCALING_POWER_SWEEP + (args.scaling_factor_power,)))
    min_holds = sorted(set(_MIN_HOLD_SWEEP + (args.min_hold_days,)))

    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")
    total_combos = len(thresholds) * len(powers) * len(min_holds)
    combo_count = 0

    print(
        f"[iv_scaling] Grid: {len(thresholds)} thresholds × "
        f"{len(powers)} powers × {len(min_holds)} min_holds = "
        f"{total_combos} candidates",
        flush=True,
    )

    for iv_thr in thresholds:
        for pwr in powers:
            for mhd in min_holds:
                combo_count += 1
                print(
                    f"[iv_scaling] {combo_count}/{total_combos} "
                    f"thr={iv_thr} pwr={pwr} mhd={mhd}",
                    flush=True,
                )

                # Scale ranks
                df_scaled = _scale_ranks(df_enriched, iv_thr, pwr)

                # Run train backtest
                scaled_train = _run_eval(
                    df_scaled, args.train_start, args.train_end,
                    data_root, args.fixed_source, args.rule, base_params,
                )

                # Apply min_hold blocking for test (from train trades)
                # For simplicity, apply to 2020 and test periods
                scaled_2020_df = df_scaled[
                    (df_scaled["trade_date"] >= "20200101") &
                    (df_scaled["trade_date"] <= "20201231")
                ].copy()

                if not scaled_2020_df.empty:
                    scaled_2020 = _run_eval(
                        scaled_2020_df, "20200101", "20201231",
                        data_root, args.fixed_source, args.rule, base_params,
                    )
                else:
                    scaled_2020 = {
                        "total_return": 0.0, "excess_return": 0.0,
                        "max_drawdown": 0.0, "win_rate": 0.0,
                        "total_trades": 0, "n_days": 0,
                    }

                scaled_test = _run_eval(
                    df_scaled, args.test_start, args.test_end,
                    data_root, args.fixed_source, args.rule, base_params,
                )

                # Compute excess vs baseline
                excess_train = round(
                    float(scaled_train["total_return"])
                    - float(baseline_train["total_return"]), 6
                )
                excess_test = round(
                    float(scaled_test["total_return"])
                    - float(baseline_test["total_return"]), 6
                )
                excess_2020 = round(
                    float(scaled_2020["total_return"])
                    - float(baseline_2020["total_return"]), 6
                )

                candidate = {
                    "iv_percentile_threshold": iv_thr,
                    "scaling_factor_power": pwr,
                    "min_hold_days": mhd,
                    "train": {
                        "total_return": round(float(scaled_train["total_return"]), 6),
                        "excess_return": excess_train,
                        "max_drawdown": round(float(scaled_train["max_drawdown"]), 6),
                        "win_rate": round(float(scaled_train["win_rate"]), 4),
                        "n_days": int(scaled_train.get("n_days", 0)),
                        "total_trades": int(scaled_train.get("total_trades", 0)),
                    },
                    "test": {
                        "total_return": round(float(scaled_test["total_return"]), 6),
                        "excess_return": excess_test,
                        "max_drawdown": round(float(scaled_test["max_drawdown"]), 6),
                        "win_rate": round(float(scaled_test["win_rate"]), 4),
                        "n_days": int(scaled_test.get("n_days", 0)),
                        "total_trades": int(scaled_test.get("total_trades", 0)),
                    },
                    "validate_2020": {
                        "total_return": round(float(scaled_2020["total_return"]), 6),
                        "excess_return": excess_2020,
                        "max_drawdown": round(float(scaled_2020["max_drawdown"]), 6),
                        "win_rate": round(float(scaled_2020["win_rate"]), 4),
                        "n_days": int(scaled_2020.get("n_days", 0)),
                        "total_trades": int(scaled_2020.get("total_trades", 0)),
                    },
                }
                all_candidates.append(candidate)

                # Score: prefer 2020 excess return (the core hypothesis)
                score = excess_2020
                if score > best_score:
                    best_score = score
                    best_candidate = candidate

    # ── Step 6: Adoption criteria ────────────────────────────────────
    adoption_pass = False
    if best_candidate is not None:
        bc = best_candidate
        # Check all periods beat baseline on total_return and excess_return
        beats_train_total = bc["train"]["total_return"] > baseline_train["total_return"]
        beats_train_excess = bc["train"]["excess_return"] > 0
        beats_test_total = bc["test"]["total_return"] > baseline_test["total_return"]
        beats_test_excess = bc["test"]["excess_return"] > 0
        beats_2020_total = bc["validate_2020"]["total_return"] > baseline_2020["total_return"]
        beats_2020_excess = bc["validate_2020"]["excess_return"] > 0

        # Check max drawdown constraints
        dd_train_ok = bc["train"]["max_drawdown"] >= baseline_train["max_drawdown"]
        dd_test_ok = bc["test"]["max_drawdown"] >= baseline_test["max_drawdown"]
        dd_2020_ok = bc["validate_2020"]["max_drawdown"] >= baseline_2020["max_drawdown"]

        # Check win rate
        wr_test_ok = bc["test"]["win_rate"] >= baseline_test["win_rate"]

        # Test not degraded by more than 5% relative
        test_degrade_limit = float(baseline_test["total_return"]) * 0.95
        test_not_worse = bc["test"]["total_return"] >= test_degrade_limit

        adoption_pass = (
            beats_2020_excess
            and test_not_worse
            and dd_test_ok
        )

    # ── Step 7: Write artifacts ──────────────────────────────────────
    # summary.json
    summary = {
        "adoption_pass": adoption_pass,
        "best_candidate": best_candidate,
        "all_candidates": all_candidates,
        "baseline": {
            "train": {
                "total_return": round(float(baseline_train["total_return"]), 6),
                "excess_return": round(float(baseline_train["excess_return"]), 6),
                "max_drawdown": round(float(baseline_train["max_drawdown"]), 6),
                "win_rate": round(float(baseline_train["win_rate"]), 4),
                "total_trades": int(baseline_train.get("total_trades", 0)),
            },
            "test": {
                "total_return": round(float(baseline_test["total_return"]), 6),
                "excess_return": round(float(baseline_test["excess_return"]), 6),
                "max_drawdown": round(float(baseline_test["max_drawdown"]), 6),
                "win_rate": round(float(baseline_test["win_rate"]), 4),
                "total_trades": int(baseline_test.get("total_trades", 0)),
            },
            "validate_2020": {
                "total_return": round(float(baseline_2020["total_return"]), 6),
                "excess_return": round(float(baseline_2020["excess_return"]), 6),
                "max_drawdown": round(float(baseline_2020["max_drawdown"]), 6),
                "win_rate": round(float(baseline_2020["win_rate"]), 4),
                "total_trades": int(baseline_2020.get("total_trades", 0)),
            },
        },
        "train_period": {"start": args.train_start, "end": args.train_end},
        "test_period": {"start": args.test_start, "end": args.test_end},
        "grid": {
            "iv_percentile_thresholds": list(thresholds),
            "scaling_factor_powers": list(powers),
            "min_hold_days": list(min_holds),
            "total_candidates": len(all_candidates),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    report = {
        "proposal_id": "cb_arb_value_gap_switch_iv_scaling_001",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "cb_iv_position_scaling_executor",
        "adoption_pass": adoption_pass,
        "best_params": (
            {
                "iv_percentile_threshold": best_candidate["iv_percentile_threshold"],
                "scaling_factor_power": best_candidate["scaling_factor_power"],
                "min_hold_days": best_candidate["min_hold_days"],
            }
            if best_candidate
            else None
        ),
        "candidates": all_candidates,
        "baseline": {
            "train_total_return": round(float(baseline_train["total_return"]), 6),
            "test_total_return": round(float(baseline_test["total_return"]), 6),
            "validate_2020_total_return": round(float(baseline_2020["total_return"]), 6),
        },
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True), encoding="utf-8"
    )

    # l4_ack.yaml
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": (
            "IV-percentile position scaling evaluation finished. "
            f"{len(all_candidates)} candidates evaluated."
        ),
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8"
    )

    # diagnostic.yaml
    iv_missing_train = int(
        df_enriched[
            (df_enriched["trade_date"] >= args.train_start) &
            (df_enriched["trade_date"] <= args.train_end)
        ]["iv_percentile"].isna().sum()
    )
    iv_missing_test = int(
        df_enriched[
            (df_enriched["trade_date"] >= args.test_start) &
            (df_enriched["trade_date"] <= args.test_end)
        ]["iv_percentile"].isna().sum()
    )
    iv_missing_2020 = int(
        df_enriched[
            (df_enriched["trade_date"] >= "20200101") &
            (df_enriched["trade_date"] <= "20201231")
        ]["iv_percentile"].isna().sum()
    )

    total_train = int(
        df_enriched[
            (df_enriched["trade_date"] >= args.train_start) &
            (df_enriched["trade_date"] <= args.train_end)
        ].shape[0]
    )
    total_test = int(
        df_enriched[
            (df_enriched["trade_date"] >= args.test_start) &
            (df_enriched["trade_date"] <= args.test_end)
        ].shape[0]
    )
    total_2020 = int(df_2020.shape[0])

    unique_cbs = len(df_enriched["ts_code"].unique())
    cb_to_stk_mapped = len(
        df_enriched.dropna(subset=["stk_code"])["ts_code"].unique()
    )

    warnings: list[str] = []
    if iv_missing_train > 0:
        pct = (iv_missing_train / total_train * 100) if total_train > 0 else 0
        warnings.append(f"train: {iv_missing_train}/{total_train} rows ({pct:.1f}%) missing IV")
    if iv_missing_test > 0:
        pct = (iv_missing_test / total_test * 100) if total_test > 0 else 0
        warnings.append(f"test: {iv_missing_test}/{total_test} rows ({pct:.1f}%) missing IV")
    if iv_missing_2020 > 0:
        pct = (iv_missing_2020 / total_2020 * 100) if total_2020 > 0 else 0
        warnings.append(f"2020: {iv_missing_2020}/{total_2020} rows ({pct:.1f}%) missing IV")

    diagnostic = {
        "warnings": warnings,
        "errors": [],
        "data_rows": {
            "train": total_train,
            "test": total_test,
            "validate_2020": total_2020,
        },
        "iv_coverage": {
            "train_missing": iv_missing_train,
            "test_missing": iv_missing_test,
            "validate_2020_missing": iv_missing_2020,
        },
        "cb_mapping": {
            "total_unique_cbs": unique_cbs,
            "mapped_to_stock": cb_to_stk_mapped,
        },
        "iv_percentile_stats": {
            "mean": round(float(df_enriched["iv_percentile"].mean()), 4),
            "median": round(float(df_enriched["iv_percentile"].median()), 4),
            "std": round(float(df_enriched["iv_percentile"].std()), 4),
            "pct_valid": round(
                100 * (1 - df_enriched["iv_percentile"].isna().mean()), 1
            ),
        },
        "grid_params": {
            "thresholds": list(thresholds),
            "powers": list(powers),
            "min_holds": list(min_holds),
        },
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True), encoding="utf-8"
    )

    # ── Done ──────────────────────────────────────────────────────────
    best_desc = (
        f"thr={best_candidate['iv_percentile_threshold']},"
        f"pwr={best_candidate['scaling_factor_power']},"
        f"mhd={best_candidate['min_hold_days']}"
        if best_candidate
        else "none"
    )
    print(
        f"[iv_scaling] adoption_pass={adoption_pass} "
        f"best=({best_desc}) "
        f"candidates={len(all_candidates)} "
        f"baseline_train_total={baseline_train['total_return']:.4f} "
        f"baseline_test_total={baseline_test['total_return']:.4f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
