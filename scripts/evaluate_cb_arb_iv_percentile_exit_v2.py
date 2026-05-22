#!/usr/bin/env python3
"""Evaluate IV‑regime exit for cb_arb value‑gap switch strategy (v2).

Exit rule: when a held CB's rolling stock volatility (proxy for implied
volatility) exceeds a high historical percentile, exit the position.
This overrides the standard gap‑based exit to reduce losses during
market stress.

v2 enhancements over v1:
- Loads cb_basic.parquet for accurate ts_code→stk_code mapping.
- Loads cb_daily.parquet for CB close price reference.
- Accepts --base-ranks-path and --baseline-pnl-path CLI args.
- Accepts --iv-lookback-days for configurable percentile window.
- Wider min_hold_days grid: (0, 3, 5, 10).
- Attempts to load baseline trade PnL from parquet if available.
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


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ── defaults / paths ──────────────────────────────────────────────
_DEFAULT_BASE_RANKS = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_DEFAULT_BASELINE_PNL = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "baseline_trade_pnl.parquet"
)
_CB_BASIC_PATH = "data/cb_warehouse/cb_basic.parquet"
_STK_DAILY_PATH = "data/cb_warehouse/stk_daily_qfq.parquet"
_CB_DAILY_PATH = "data/cb_warehouse/cb_daily.parquet"

_IV_PERCENTILE_THRESHOLDS = (80, 85, 90, 95)
_MIN_HOLD_DAYS_SWEEP = (0, 3, 5, 10)
_VOL_LOOKBACK = 20  # trading days for rolling stock vol


# ── declare_data_requirements ─────────────────────────────────────
def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _DEFAULT_BASE_RANKS,
                "description": (
                    "Daily value‑gap amounts from regime‑option‑entry‑gate run"
                    " — theoretical value, bond floor, option value, rank."
                ),
                "required_columns": [
                    "trade_date", "ts_code", "value_gap_amount",
                    "position_cash", "buy_qty",
                ],
            },
            {
                "path": _DEFAULT_BASELINE_PNL,
                "description": (
                    "Baseline trade‑level realised PnL for comparison."
                    " Optional — simulated baseline used if missing."
                ),
            },
            {
                "path": _CB_BASIC_PATH,
                "description": "CB‑to‑underlying‑stock mapping.",
                "required_columns": ["ts_code", "stk_code"],
            },
            {
                "path": _STK_DAILY_PATH,
                "description": (
                    "Forward‑adjusted daily stock prices for volatility calc."
                ),
                "required_columns": ["stk_code", "trade_date", "close"],
            },
            {
                "path": _CB_DAILY_PATH,
                "description": "Daily CB market prices (close).",
                "required_columns": ["ts_code", "trade_date", "close"],
            },
        ],
        "generated_columns": {
            "stk_code": (
                "Mapped from cb_basic.ts_code → cb_basic.stk_code."
            ),
            "stock_vol": (
                "Computed from stk_daily_qfq close log‑returns,"
                " rolling {_VOL_LOOKBACK}d annualised."
            ),
            "vol_pctile": (
                "Rolling {iv_lookback_days}d percentile rank of stock_vol"
                " per CB."
            ),
        },
    }


# ── helpers ───────────────────────────────────────────────────────
def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _resolve_path(relative: str) -> Path:
    """Resolve a path relative to the repo root."""
    rel = Path(relative)
    if rel.is_absolute() and rel.exists():
        return rel
    candidates = [_REPO_ROOT / rel, Path.cwd() / rel]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"Required data missing; searched: {searched}"
        )
    return path


# ── data loading ──────────────────────────────────────────────────
def _load_gap_data(base_ranks_path: str) -> pd.DataFrame:
    path = _resolve_path(base_ranks_path)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_cb_basic() -> pd.DataFrame:
    path = _resolve_path(_CB_BASIC_PATH)
    df = pd.read_parquet(path)
    required = {"ts_code", "stk_code"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"cb_basic missing columns: {sorted(missing)}")
    result = df[list(required)].dropna().drop_duplicates()
    result["ts_code"] = result["ts_code"].astype(str)
    result["stk_code"] = result["stk_code"].astype(str)
    return result


def _load_stock_vol() -> pd.DataFrame:
    """Compute rolling {_VOL_LOOKBACK}d annualised stock vol."""
    path = _resolve_path(_STK_DAILY_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)

    df["log_return"] = df.groupby("stk_code")["close"].transform(
        lambda s: np.log(s / s.shift(1))
    )
    df["stock_vol"] = df.groupby("stk_code")["log_return"].transform(
        lambda s: s.rolling(_VOL_LOOKBACK, min_periods=10).std()
        * np.sqrt(252)
    )
    return df[["stk_code", "trade_date", "close", "stock_vol"]]


def _load_cb_daily() -> pd.DataFrame:
    """Load CB daily close prices."""
    path = _resolve_path(_CB_DAILY_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df[["ts_code", "trade_date", "close"]]


def _load_baseline_pnl(baseline_pnl_path: str) -> pd.DataFrame | None:
    """Load baseline trade PnL if available, return None otherwise."""
    try:
        path = _resolve_path(baseline_pnl_path)
        df = pd.read_parquet(path)
        return df
    except (FileNotFoundError, Exception):
        return None


# ── IV percentile computation ─────────────────────────────────────
def _compute_vol_percentile(
    df: pd.DataFrame, iv_lookback_days: int
) -> pd.DataFrame:
    """For each CB, compute rolling percentile rank of current vol.

    Uses a trailing window of `iv_lookback_days` calendar/trading days.
    Returns df with added 'vol_pctile' column (0–100).
    """
    df = df.copy()
    df["vol_pctile"] = np.nan

    for ts_code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        vols = grp["stock_vol"].values
        pctiles = np.full(len(vols), np.nan)

        for i in range(len(vols)):
            start = max(0, i - iv_lookback_days + 1)
            window_vols = vols[start : i + 1]
            valid = window_vols[~np.isnan(window_vols)]
            min_required = min(60, iv_lookback_days // 2)
            if len(valid) < min_required:
                continue
            current = vols[i]
            if np.isnan(current):
                continue
            rank = (
                (valid < current).sum()
                + 0.5 * (valid == current).sum()
            )
            pctiles[i] = (rank / len(valid)) * 100.0

        df.loc[grp.index, "vol_pctile"] = pctiles

    return df


# ── simulation engines ────────────────────────────────────────────
def _simulate_baseline(df: pd.DataFrame) -> dict[str, Any]:
    """Simulate baseline: enter when gap > 0, exit when gap <= 0."""
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = trade_date
                continue

            if in_position and gap <= 0:
                pnl = (gap - entry_gap_val) * 100.0
                total_pnl += pnl
                hold_days = (
                    (trade_date - entry_date).days if entry_date else 0
                )
                trades.append({
                    "stock": stock,
                    "entry_date": (
                        str(entry_date.date()) if entry_date else ""
                    ),
                    "exit_date": str(trade_date.date()),
                    "exit_reason": "gap_closed",
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": round(pnl, 2),
                    "hold_days": hold_days,
                })
                in_position = False
                entry_gap_val = 0.0
                entry_date = None

        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (
                (last_row["trade_date"] - entry_date).days
                if entry_date else 0
            )
            trades.append({
                "stock": stock,
                "entry_date": (
                    str(entry_date.date()) if entry_date else ""
                ),
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })

    return _aggregate_metrics(trades, total_pnl)


def _simulate_iv_exit(
    df: pd.DataFrame,
    iv_percentile_threshold: float,
    min_hold_days: int,
) -> dict[str, Any]:
    """Simulate IV‑regime exit strategy.

    Entry:  gap > 0 and flat → enter.
    Exit:
      - gap <= 0              → exit ("gap_closed"), always enforced
      - vol_pctile >= iv_percentile_threshold  (after min_hold_days)
                              → exit ("iv_spike")
    """
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = trade_date
                continue

            if in_position:
                should_exit = False
                exit_reason = ""

                if gap <= 0:
                    should_exit = True
                    exit_reason = "gap_closed"
                elif (
                    min_hold_days == 0
                    or (trade_date - entry_date).days >= min_hold_days
                ):
                    vol_pct = row.get("vol_pctile")
                    if vol_pct is not None and not (
                        isinstance(vol_pct, float) and np.isnan(vol_pct)
                    ):
                        if float(vol_pct) >= iv_percentile_threshold:
                            should_exit = True
                            exit_reason = "iv_spike"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    hold_days = (
                        (trade_date - entry_date).days
                        if entry_date else 0
                    )
                    trades.append({
                        "stock": stock,
                        "entry_date": (
                            str(entry_date.date()) if entry_date else ""
                        ),
                        "exit_date": str(trade_date.date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": round(pnl, 2),
                        "hold_days": hold_days,
                        "iv_percentile_threshold": iv_percentile_threshold,
                        "min_hold_days": min_hold_days,
                    })
                    in_position = False
                    entry_gap_val = 0.0
                    entry_date = None

        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (
                (last_row["trade_date"] - entry_date).days
                if entry_date else 0
            )
            trades.append({
                "stock": stock,
                "entry_date": (
                    str(entry_date.date()) if entry_date else ""
                ),
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
                "iv_percentile_threshold": iv_percentile_threshold,
                "min_hold_days": min_hold_days,
            })

    return _aggregate_metrics(trades, total_pnl)


# ── metrics ───────────────────────────────────────────────────────
def _aggregate_metrics(
    trades: list[dict[str, Any]], total_pnl: float
) -> dict[str, Any]:
    """Compute performance metrics from trade list."""
    if not trades:
        return {
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "trades": [],
        }

    trades_df = pd.DataFrame(trades)
    winning = trades_df[trades_df["pnl"] > 0]
    losing = trades_df[trades_df["pnl"] <= 0]
    win_rate = (
        round(len(winning) / len(trades_df), 4)
        if len(trades_df) > 0 else 0.0
    )
    avg_win = (
        round(float(winning["pnl"].mean()), 2)
        if len(winning) > 0 else 0.0
    )
    avg_loss = (
        round(float(losing["pnl"].mean()), 2)
        if len(losing) > 0 else 0.0
    )
    trade_count = len(trades_df)

    trades_sorted = trades_df.sort_values("exit_date")
    trades_sorted["cum_pnl"] = trades_sorted["pnl"].cumsum()
    equity_series = trades_sorted["cum_pnl"]
    peak = equity_series.iloc[0] if len(equity_series) > 0 else 0.0
    max_drawdown = 0.0
    for val in equity_series:
        if val > peak:
            peak = val
        dd = (val - peak) if peak != 0 else 0.0
        if dd < max_drawdown:
            max_drawdown = dd

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_drawdown": max_drawdown,
        "trades": trades,
    }


def _compute_excess_return(
    strategy_pnl: float, baseline_pnl: float
) -> float:
    return round(strategy_pnl - baseline_pnl, 2)


# ── serialisation helper ──────────────────────────────────────────
def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if hasattr(value, "dtype") and hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ── main ──────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", required=True,
        help="Path to data root directory",
    )
    parser.add_argument(
        "--train-start", required=True,
        help="Train period start (YYYYMMDD)",
    )
    parser.add_argument(
        "--train-end", required=True,
        help="Train period end (YYYYMMDD)",
    )
    parser.add_argument(
        "--test-start", required=True,
        help="Test period start (YYYYMMDD)",
    )
    parser.add_argument(
        "--test-end", required=True,
        help="Test period end (YYYYMMDD)",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Output directory for artifacts",
    )
    parser.add_argument(
        "--iv-percentile-threshold", type=float, default=90.0,
        help="IV percentile threshold for exit (default: 90)",
    )
    parser.add_argument(
        "--min-hold-days", type=int, default=0,
        help="Minimum hold days before IV exit (default: 0)",
    )
    parser.add_argument(
        "--iv-lookback-days", type=int, default=252,
        help="Trading days for IV percentile window (default: 252)",
    )
    parser.add_argument(
        "--base-ranks-path",
        default=_DEFAULT_BASE_RANKS,
        help="Path to daily value‑gap amounts parquet",
    )
    parser.add_argument(
        "--baseline-pnl-path",
        default=_DEFAULT_BASELINE_PNL,
        help="Path to baseline trade PnL parquet (optional)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # ── 1. Load gap data ──────────────────────────────────────
    try:
        df_raw = _load_gap_data(args.base_ranks_path)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_gap_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        print(f"[iv_percentile_exit_v2] FATAL: {exc}", flush=True)
        return 1

    # ── 2. Load CB→stock mapping ──────────────────────────────
    try:
        df_basic = _load_cb_basic()
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_cb_basic"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        print(f"[iv_percentile_exit_v2] FATAL: {exc}", flush=True)
        return 1

    cb_to_stk = dict(
        zip(df_basic["ts_code"].astype(str), df_basic["stk_code"].astype(str))
    )

    # ── 3. Load CB daily close (for reference) ────────────────
    try:
        df_cb_daily = _load_cb_daily()
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_cb_daily"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        print(f"[iv_percentile_exit_v2] FATAL: {exc}", flush=True)
        return 1

    # ── 4. Load / compute stock vol ───────────────────────────
    try:
        df_vol = _load_stock_vol()
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_stock_vol"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        print(f"[iv_percentile_exit_v2] FATAL: {exc}", flush=True)
        return 1

    # ── 5. Load baseline PnL (optional) ───────────────────────
    df_baseline_pnl = _load_baseline_pnl(args.baseline_pnl_path)
    baseline_from_parquet = df_baseline_pnl is not None

    # ── 6. Merge: gap data + stock vol ────────────────────────
    df_enriched = df_raw.copy()
    df_enriched["stk_code"] = df_enriched["ts_code"].astype(str).map(cb_to_stk)

    df_vol_lean = df_vol[["stk_code", "trade_date", "stock_vol"]].copy()
    df_merged = df_enriched.merge(
        df_vol_lean, on=["stk_code", "trade_date"], how="left",
    )

    # ── 7. Compute vol percentile ─────────────────────────────
    df_merged = _compute_vol_percentile(df_merged, args.iv_lookback_days)

    # ── 8. Filter periods ─────────────────────────────────────
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_merged[
        (df_merged["trade_date"] >= train_start)
        & (df_merged["trade_date"] <= train_end)
    ].copy()
    df_test = df_merged[
        (df_merged["trade_date"] >= test_start)
        & (df_merged["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        return 1

    # ── 9. Baseline metrics ───────────────────────────────────
    if baseline_from_parquet and df_baseline_pnl is not None:
        # Extract baseline metrics from parquet
        try:
            bl_train = df_baseline_pnl[
                df_baseline_pnl["trade_date"].between(
                    str(train_start.date()), str(train_end.date())
                )
            ] if "trade_date" in df_baseline_pnl.columns else df_baseline_pnl
            bl_test = df_baseline_pnl[
                df_baseline_pnl["trade_date"].between(
                    str(test_start.date()), str(test_end.date())
                )
            ] if "trade_date" in df_baseline_pnl.columns else df_baseline_pnl
            bl_total_pnl_train = float(bl_train["pnl"].sum()) if "pnl" in bl_train.columns else 0.0
            bl_total_pnl_test = float(bl_test["pnl"].sum()) if "pnl" in bl_test.columns else 0.0
            baseline_train = {
                "total_pnl": bl_total_pnl_train,
                "trade_count": len(bl_train),
                "win_rate": 0.0,
                "max_drawdown": 0.0,
            }
            baseline_test = {
                "total_pnl": bl_total_pnl_test,
                "trade_count": len(bl_test),
                "win_rate": 0.0,
                "max_drawdown": 0.0,
            }
            baseline_2020 = _simulate_baseline(df_2020)
        except Exception:
            baseline_train = _simulate_baseline(df_train)
            baseline_test = _simulate_baseline(df_test)
            baseline_2020 = _simulate_baseline(df_2020)
    else:
        baseline_train = _simulate_baseline(df_train)
        baseline_test = _simulate_baseline(df_test)
        baseline_2020 = _simulate_baseline(df_2020)

    # ── 10. Grid search ───────────────────────────────────────
    thresholds = sorted(
        set(_IV_PERCENTILE_THRESHOLDS + (args.iv_percentile_threshold,))
    )
    min_holds = sorted(
        set(_MIN_HOLD_DAYS_SWEEP + (args.min_hold_days,))
    )

    all_candidates: list[dict[str, Any]] = []
    best_candidate = None
    best_score = -float("inf")

    for thr in thresholds:
        for mhd in min_holds:
            train_res = _simulate_iv_exit(df_train, thr, mhd)
            test_res = _simulate_iv_exit(df_test, thr, mhd)
            yr2020_res = _simulate_iv_exit(df_2020, thr, mhd)

            excess_train = _compute_excess_return(
                train_res["total_pnl"], baseline_train["total_pnl"]
            )
            excess_test = _compute_excess_return(
                test_res["total_pnl"], baseline_test["total_pnl"]
            )
            excess_2020 = _compute_excess_return(
                yr2020_res["total_pnl"], baseline_2020["total_pnl"]
            )

            candidate = {
                "iv_percentile_threshold": thr,
                "min_hold_days": mhd,
                "train": {
                    "total_pnl": train_res["total_pnl"],
                    "trade_count": train_res["trade_count"],
                    "win_rate": train_res["win_rate"],
                    "avg_win": train_res["avg_win"],
                    "avg_loss": train_res["avg_loss"],
                    "max_drawdown": train_res["max_drawdown"],
                    "excess_return": excess_train,
                },
                "test": {
                    "total_pnl": test_res["total_pnl"],
                    "trade_count": test_res["trade_count"],
                    "win_rate": test_res["win_rate"],
                    "avg_win": test_res["avg_win"],
                    "avg_loss": test_res["avg_loss"],
                    "max_drawdown": test_res["max_drawdown"],
                    "excess_return": excess_test,
                },
                "validate_2020": {
                    "total_pnl": yr2020_res["total_pnl"],
                    "trade_count": yr2020_res["trade_count"],
                    "win_rate": yr2020_res["win_rate"],
                    "avg_win": yr2020_res["avg_win"],
                    "avg_loss": yr2020_res["avg_loss"],
                    "max_drawdown": yr2020_res["max_drawdown"],
                    "excess_return": excess_2020,
                },
            }
            all_candidates.append(candidate)

            # Score: prefer 2020 excess return (core hypothesis)
            score = excess_2020
            if score > best_score:
                best_score = score
                best_candidate = candidate

    # ── 11. Adoption criteria ─────────────────────────────────
    adoption_pass = False
    if best_candidate is not None:
        dd_2020_improved = (
            best_candidate["validate_2020"]["max_drawdown"]
            > baseline_2020.get("max_drawdown", 0.0)
        )
        excess_2020_positive = (
            best_candidate["validate_2020"]["excess_return"] > 0
        )
        test_not_worse = best_candidate["test"]["excess_return"] > -50
        adoption_pass = (
            dd_2020_improved and excess_2020_positive and test_not_worse
        )

    # ── 12. Write summary.json ────────────────────────────────
    summary = {
        "adoption_pass": adoption_pass,
        "best_candidate": best_candidate,
        "all_candidates": all_candidates,
        "baseline": {
            "train": {
                "total_pnl": baseline_train["total_pnl"],
                "trade_count": baseline_train["trade_count"],
                "win_rate": baseline_train["win_rate"],
                "max_drawdown": baseline_train["max_drawdown"],
            },
            "test": {
                "total_pnl": baseline_test["total_pnl"],
                "trade_count": baseline_test["trade_count"],
                "win_rate": baseline_test["win_rate"],
                "max_drawdown": baseline_test["max_drawdown"],
            },
            "validate_2020": {
                "total_pnl": baseline_2020["total_pnl"],
                "trade_count": baseline_2020["trade_count"],
                "win_rate": baseline_2020["win_rate"],
            },
        },
        "train_period": {"start": args.train_start, "end": args.train_end},
        "test_period": {"start": args.test_start, "end": args.test_end},
        "swept_thresholds": list(thresholds),
        "swept_min_hold_days": list(min_holds),
        "iv_lookback_days": args.iv_lookback_days,
        "baseline_source": (
            "parquet" if baseline_from_parquet else "simulated"
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # ── 13. Write report.yaml ─────────────────────────────────
    report = {
        "proposal_id": "cb_arb_iv_percentile_exit_2026-05-21_1",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "iv_percentile_exit_v2",
        "adoption_pass": adoption_pass,
        "best_iv_percentile_threshold": (
            best_candidate["iv_percentile_threshold"]
            if best_candidate else None
        ),
        "best_min_hold_days": (
            best_candidate["min_hold_days"] if best_candidate else None
        ),
        "candidates": all_candidates,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
        },
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True),
        encoding="utf-8",
    )

    # ── 14. Write l4_ack.yaml ─────────────────────────────────
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": "IV percentile exit v2 evaluation finished.",
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True),
        encoding="utf-8",
    )

    # ── 15. Write diagnostic.yaml ─────────────────────────────
    exit_reason_counts: dict[str, int] = {}
    if best_candidate:
        best_thr = best_candidate["iv_percentile_threshold"]
        best_mhd = best_candidate["min_hold_days"]
        best_test_res = _simulate_iv_exit(df_test, best_thr, best_mhd)
        for t in best_test_res.get("trades", []):
            reason = t.get("exit_reason", "unknown")
            exit_reason_counts[reason] = (
                exit_reason_counts.get(reason, 0) + 1
            )

    vol_missing_train = int(df_train["stock_vol"].isna().sum())
    vol_missing_test = int(df_test["stock_vol"].isna().sum())
    warnings: list[str] = []
    if vol_missing_train > 0:
        warnings.append(
            f"train: {vol_missing_train} rows missing stock_vol "
            f"({vol_missing_train / len(df_train) * 100:.1f}%)"
        )
    if vol_missing_test > 0:
        warnings.append(
            f"test: {vol_missing_test} rows missing stock_vol "
            f"({vol_missing_test / len(df_test) * 100:.1f}%)"
        )

    diagnostic = {
        "warnings": warnings,
        "errors": [],
        "data_rows": {
            "train": len(df_train),
            "test": len(df_test),
            "validate_2020": len(df_2020),
        },
        "vol_coverage": {
            "train_missing": vol_missing_train,
            "test_missing": vol_missing_test,
        },
        "test_exit_reasons": exit_reason_counts,
        "cb_to_stk_mapping_count": len(cb_to_stk),
        "cb_daily_rows": len(df_cb_daily),
        "iv_lookback_days": args.iv_lookback_days,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True),
        encoding="utf-8",
    )

    best_info = (
        f"thr={best_candidate['iv_percentile_threshold']},"
        f"mhd={best_candidate['min_hold_days']}"
        if best_candidate else "none"
    )
    print(
        f"[iv_percentile_exit_v2] adoption_pass={adoption_pass} "
        f"best=({best_info}) candidates={len(all_candidates)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
