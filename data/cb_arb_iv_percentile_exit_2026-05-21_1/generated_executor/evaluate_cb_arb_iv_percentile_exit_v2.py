#!/usr/bin/env python3
"""Evaluate IV-regime exit for cb_arb value-gap switch strategy — V2.

Exit rule: when a held CB's rolling stock volatility (proxy for implied
volatility) exceeds a high historical percentile, exit the position.
This overrides the standard gap-based exit to reduce losses during
market stress.

V2 improvements over V1:
- Wider min_hold_days grid: (0, 3, 5, 10) instead of (0, 1)
- Loads actual baseline trade PnL from baseline_trade_pnl.parquet
- Accepts --base-ranks-path / --baseline-pnl-path / --iv-lookback-days
- Sharpe ratio computation for train/test periods

IV proxy: rolling 20-day annualised stock volatility computed from
forward-adjusted stock prices in stk_daily_qfq.parquet.

Grid search over iv_percentile_threshold (80, 85, 90, 95) and
min_hold_days (0, 3, 5, 10). Compares against baseline (gap <= 0 exit only)
on train, 2020 repair, and test periods.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path
from typing import Any


# ── Repo root & sys.path (must precede all third-party imports) ──
# The compliance import-reachability probe runs with -I in /tmp,
# so numpy/pandas/yaml cannot be imported at module level.


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ── Lazy third-party imports ────────────────────────────────────

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


# YAML numpy representer registration runs once at first yaml write.
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


# ── Constants ───────────────────────────────────────────────────────────
_IV_PERCENTILE_THRESHOLDS = (80, 85, 90, 95)
_MIN_HOLD_DAYS_SWEEP = (0, 3, 5, 10)
_VOL_LOOKBACK = 20       # trading days for rolling stock vol
_PERCENTILE_LOOKBACK = 252  # trading days for IV percentile rank

# Default data paths
_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_BASELINE_PNL_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "baseline_trade_pnl.parquet"
)
_CB_BASIC_PATH = "data/cb_warehouse/cb_basic.parquet"
_STK_DAILY_PATH = "data/cb_warehouse/stk_daily_qfq.parquet"
_CB_DAILY_PATH = "data/cb_warehouse/cb_daily.parquet"


# ═══════════════════════════════════════════════════════════════════════
#  Data requirements
# ═══════════════════════════════════════════════════════════════════════
def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
                "required_columns": [
                    "trade_date", "ts_code", "value_gap_amount"
                ],
            },
            {
                "path": _BASELINE_PNL_PATH,
                "description": "Baseline strategy trade PnL for comparison.",
                "required_columns": [
                    "ts_code", "entry_date", "exit_date", "entry_gap",
                    "exit_gap", "pnl", "exit_reason"
                ],
            },
            {
                "path": _CB_BASIC_PATH,
                "description": "CB-to-underlying-stock mapping.",
                "required_columns": ["ts_code", "stk_code"],
            },
            {
                "path": _STK_DAILY_PATH,
                "description": "Forward-adjusted daily stock prices for vol calculation.",
                "required_columns": ["stk_code", "trade_date", "close"],
            },
            {
                "path": _CB_DAILY_PATH,
                "description": "Daily CB market prices (reserved for future IV inversion).",
                "required_columns": ["ts_code", "trade_date", "close"],
            },
        ],
        "generated_columns": {
            "stk_code": "Derived from cb_basic.ts_code → cb_basic.stk_code mapping.",
            "stock_vol": "Computed from stk_daily_qfq close log returns.",
            "vol_pctile": "Rolling 252-day percentile of stock_vol per bond.",
        },
    }


# ═══════════════════════════════════════════════════════════════════════
#  Gatekeeper
# ═══════════════════════════════════════════════════════════════════════
def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ═══════════════════════════════════════════════════════════════════════
#  Path resolution
# ═══════════════════════════════════════════════════════════════════════
def _resolve_path(relative: str, data_root: str) -> Path:
    rel = Path(relative)
    candidates = [
        Path(data_root) / rel,
        _REPO_ROOT / rel,
        Path.cwd() / rel,
    ]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"Required data missing; searched: {searched}"
        )
    return path


# ═══════════════════════════════════════════════════════════════════════
#  Data loading
# ═══════════════════════════════════════════════════════════════════════
def _load_gap_data(data_root: str, gap_path: str) -> _get_pd().DataFrame:
    path = _resolve_path(gap_path, data_root)
    df = _get_pd().read_parquet(path)
    df["trade_date"] = _get_pd().to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_cb_basic(data_root: str) -> _get_pd().DataFrame:
    path = _resolve_path(_CB_BASIC_PATH, data_root)
    df = _get_pd().read_parquet(path)
    missing = {"ts_code", "stk_code"} - set(df.columns)
    if missing:
        raise ValueError(f"cb_basic missing columns: {sorted(missing)}")
    return df[["ts_code", "stk_code"]].dropna().drop_duplicates()


def _load_and_compute_stock_vol(data_root: str) -> _get_pd().DataFrame:
    path = _resolve_path(_STK_DAILY_PATH, data_root)
    df = _get_pd().read_parquet(path)
    df["trade_date"] = _get_pd().to_datetime(df["trade_date"], errors="coerce")
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)

    # Daily log returns
    df["log_return"] = df.groupby("stk_code")["close"].transform(
        lambda s: _get_np().log(s / s.shift(1))
    )
    # Rolling annualised vol
    df["stock_vol"] = df.groupby("stk_code")["log_return"].transform(
        lambda s: s.rolling(_VOL_LOOKBACK, min_periods=10).std()
        * _get_np().sqrt(252)
    )
    return df[["stk_code", "trade_date", "close", "stock_vol"]]


def _load_baseline_pnl(
    data_root: str, baseline_path: str
) -> _get_pd().DataFrame:
    """Load baseline trade PnL from parquet and compute per-period metrics."""
    path = _resolve_path(baseline_path, data_root)
    df = _get_pd().read_parquet(path)
    # Normalise date columns
    for col in ("entry_date", "exit_date"):
        if col in df.columns:
            df[col] = _get_pd().to_datetime(df[col], errors="coerce")
    # Ensure required columns exist
    rcols = {"pnl", "exit_date"}
    missing = rcols - set(df.columns)
    if missing:
        raise ValueError(
            f"baseline_trade_pnl missing columns: {sorted(missing)}"
        )
    return df


def _baseline_metrics_from_pnl(
    df_pnl: _get_pd().DataFrame, period_start: _get_pd().Timestamp, period_end: _get_pd().Timestamp
) -> dict[str, Any]:
    """Extract baseline metrics from actual trade PnL for a date range."""
    mask = (
        (df_pnl["exit_date"] >= period_start)
        & (df_pnl["exit_date"] <= period_end)
    )
    period = df_pnl[mask].copy()
    if period.empty:
        return {
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_hold_days": 0.0,
            "max_drawdown": 0.0,
            "gap_closed_exits": 0,
            "iv_spike_exits": 0,
            "force_closes": 0,
        }
    period = period.sort_values("exit_date")
    total_pnl = round(float(period["pnl"].sum()), 2)
    trade_count = len(period)
    winning = period[period["pnl"] > 0]
    losing = period[period["pnl"] <= 0]
    win_rate = round(len(winning) / trade_count, 4) if trade_count > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0

    # Avg hold days
    if "entry_date" in period.columns and "exit_date" in period.columns:
        avg_hold = round(
            float((period["exit_date"] - period["entry_date"]).dt.days.mean()), 1
        )
    else:
        avg_hold = 0.0

    # Max drawdown from cumulative PnL
    cum = period["pnl"].cumsum().values
    peak = cum[0] if len(cum) > 0 else 0.0
    max_dd = 0.0
    for val in cum:
        if val > peak:
            peak = float(val)
        dd = float(val) - peak
        if dd < max_dd:
            max_dd = dd

    # Exit reason counts
    reasons = period["exit_reason"].value_counts().to_dict() if "exit_reason" in period.columns else {}
    gap_closed = int(reasons.get("gap_closed", 0))
    iv_spike = int(reasons.get("iv_spike", 0))
    force_close = int(reasons.get("force_close", 0))

    return {
        "total_pnl": total_pnl,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "gap_closed_exits": gap_closed,
        "iv_spike_exits": iv_spike,
        "force_closes": force_close,
    }


def _compute_sharpe(df_pnl: _get_pd().DataFrame, period_start: _get_pd().Timestamp, period_end: _get_pd().Timestamp) -> float:
    """Compute annualised Sharpe from trade PnL within a period."""
    mask = (
        (df_pnl["exit_date"] >= period_start)
        & (df_pnl["exit_date"] <= period_end)
    )
    period = df_pnl[mask].sort_values("exit_date")
    if len(period) < 2:
        return 0.0
    daily = period.groupby("exit_date")["pnl"].sum().sort_index()
    daily_mean = float(daily.mean())
    daily_std = float(daily.std(ddof=1))
    if daily_std == 0 or _get_np().isnan(daily_std):
        return 0.0
    return round(daily_mean / daily_std * _get_np().sqrt(252), 4)


# ═══════════════════════════════════════════════════════════════════════
#  Volatility & percentile computation
# ═══════════════════════════════════════════════════════════════════════
def _merge_vol_into_gap(
    df_gap: _get_pd().DataFrame,
    df_vol: _get_pd().DataFrame,
    cb_to_stk: dict[str, str],
) -> _get_pd().DataFrame:
    """Map CB ts_code → stk_code, then merge stock vol."""
    df = df_gap.copy()
    df["stk_code"] = df["ts_code"].astype(str).map(cb_to_stk)
    df_vol_lean = df_vol[["stk_code", "trade_date", "stock_vol"]].copy()
    merged = df.merge(df_vol_lean, on=["stk_code", "trade_date"], how="left")
    return merged


def _compute_vol_percentile(df: _get_pd().DataFrame) -> _get_pd().DataFrame:
    """Compute rolling 252-day percentile of stock_vol per CB.

    Returns df with added 'vol_pctile' column (0-100).
    """
    df = df.copy()
    df["vol_pctile"] = _get_np().nan

    for ts_code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        vols = grp["stock_vol"].values
        pctiles = _get_np().full(len(vols), _get_np().nan)

        for i in range(len(vols)):
            window_start = max(0, i - _PERCENTILE_LOOKBACK + 1)
            window_vols = vols[window_start : i + 1]
            valid = window_vols[~_get_np().isnan(window_vols)]
            if len(valid) < 60:
                continue
            current = vols[i]
            if _get_np().isnan(current):
                continue
            rank = (valid < current).sum() + 0.5 * (valid == current).sum()
            pctiles[i] = (rank / len(valid)) * 100.0

        df.loc[grp.index, "vol_pctile"] = pctiles

    return df


# ═══════════════════════════════════════════════════════════════════════
#  Simulation engine
# ═══════════════════════════════════════════════════════════════════════
def _simulate_baseline(df: _get_pd().DataFrame) -> dict[str, Any]:
    """Re-simulate baseline: enter when gap > 0, exit when gap <= 0."""
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
                    "entry_date": str(entry_date.date()) if entry_date else "",
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
                (last_row["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append({
                "stock": stock,
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })

    return _aggregate_metrics(trades, total_pnl)


def _simulate_iv_exit(
    df: _get_pd().DataFrame,
    iv_percentile_threshold: float,
    min_hold_days: int,
) -> dict[str, Any]:
    """Simulate IV-regime exit.

    Entry: gap > 0 and flat → enter.
    Exit:
      - gap <= 0 → "gap_closed" (always enforced)
      - vol_pctile > threshold (after min_hold_days) → "iv_spike"
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
                        isinstance(vol_pct, float) and _get_np().isnan(vol_pct)
                    ):
                        if float(vol_pct) > iv_percentile_threshold:
                            should_exit = True
                            exit_reason = "iv_spike"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    hold_days = (
                        (trade_date - entry_date).days if entry_date else 0
                    )
                    trades.append({
                        "stock": stock,
                        "entry_date": str(entry_date.date()) if entry_date else "",
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
                (last_row["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append({
                "stock": stock,
                "entry_date": str(entry_date.date()) if entry_date else "",
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
            "avg_hold_days": 0.0,
            "max_drawdown": 0.0,
            "gap_closed_exits": 0,
            "iv_spike_exits": 0,
            "force_closes": 0,
            "trades": [],
        }

    tdf = _get_pd().DataFrame(trades)
    winning = tdf[tdf["pnl"] > 0]
    losing = tdf[tdf["pnl"] <= 0]
    win_rate = round(len(winning) / len(tdf), 4) if len(tdf) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(tdf)
    avg_hold = round(float(tdf["hold_days"].mean()), 1)

    # Max drawdown
    tdf_sorted = tdf.sort_values("exit_date")
    tdf_sorted["cum_pnl"] = tdf_sorted["pnl"].cumsum()
    equity = tdf_sorted["cum_pnl"].values
    peak = equity[0] if len(equity) > 0 else 0.0
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = float(val)
        dd = float(val) - peak
        if dd < max_dd:
            max_dd = dd

    # Exit reason counts
    exit_counts = tdf["exit_reason"].value_counts().to_dict()
    gap_closed = int(exit_counts.get("gap_closed", 0))
    iv_spike = int(exit_counts.get("iv_spike", 0))
    force_close = int(exit_counts.get("force_close", 0))

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "gap_closed_exits": gap_closed,
        "iv_spike_exits": iv_spike,
        "force_closes": force_close,
        "trades": trades,
    }


def _compute_excess_return(strategy_pnl: float, baseline_pnl: float) -> float:
    return round(strategy_pnl - baseline_pnl, 2)


# ═══════════════════════════════════════════════════════════════════════
#  Serialisation helpers
# ═══════════════════════════════════════════════════════════════════════
def _plain(value: Any) -> Any:
    """Recursively convert numpy/pandas types to native Python."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, _get_np().floating):
        return float(value)
    if isinstance(value, _get_np().integer):
        return int(value)
    if isinstance(value, _get_np().bool_):
        return bool(value)
    if hasattr(value, "dtype") and hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ═══════════════════════════════════════════════════════════════════════
#  Artifact writers
# ═══════════════════════════════════════════════════════════════════════
def _write_artifacts(
    output_dir: Path,
    best_candidate: dict[str, Any] | None,
    all_candidates: list[dict[str, Any]],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    baseline_2020: dict[str, Any],
    thresholds: list[float],
    min_holds: list[int],
    train_period: dict[str, str],
    test_period: dict[str, str],
    df_train: _get_pd().DataFrame,
    df_test: _get_pd().DataFrame,
    df_2020: _get_pd().DataFrame,
    cb_to_stk: dict[str, str],
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.now(_tz.utc)
    now_str = now.isoformat(timespec="seconds")
    today_str = now_str.split("T", 1)[0]

    # ── Adoption criteria ──
    # Proposal success_criteria:
    #   test Sharpe >= baseline Sharpe
    #   test max drawdown < baseline max drawdown
    #   2020 excess return > baseline 2020 excess return
    adoption_pass = False
    if best_candidate is not None:
        bc = best_candidate
        test_sharpe_ok = bc["test"].get("sharpe", 0.0) >= baseline_test.get("sharpe", 0.0)
        test_dd_better = bc["test"]["max_drawdown"] > baseline_test["max_drawdown"]
        excess_2020_ok = bc["validate_2020"].get("excess_return", 0.0) > 0
        adoption_pass = test_sharpe_ok and test_dd_better and excess_2020_ok

    # ── summary.json ──
    summary = {
        "adoption_pass": adoption_pass,
        "best_candidate": best_candidate,
        "all_candidates": all_candidates,
        "baseline": {
            "train": baseline_train,
            "test": baseline_test,
            "validate_2020": baseline_2020,
        },
        "train_period": train_period,
        "test_period": test_period,
        "swept_thresholds": thresholds,
        "swept_min_hold_days": min_holds,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # ── report.yaml (framework schema) ──
    _ensure_yaml_np_reprs()
    l6_decision = "adopt" if adoption_pass else "reject"
    best_thr = best_candidate["iv_percentile_threshold"] if best_candidate else None
    best_mhd = best_candidate["min_hold_days"] if best_candidate else None

    evaluator_report = {
        "proposal_id": "cb_arb_iv_percentile_exit_2026-05-21_1",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "iv_percentile_exit_v2",
        "adoption_pass": adoption_pass,
        "best_iv_percentile_threshold": best_thr,
        "best_min_hold_days": best_mhd,
        "candidates": all_candidates,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
            "train_sharpe": baseline_train.get("sharpe", 0.0),
            "test_sharpe": baseline_test.get("sharpe", 0.0),
        },
    }
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": today_str,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "best_iv_percentile_threshold": best_thr,
            "best_min_hold_days": best_mhd,
            "evaluator": "iv_percentile_exit_v2",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [f"candidates below {output_dir.name} best by adoption criteria — evidence only"]
            if adoption_pass
            else [f"{output_dir.name}: rejected by mechanical thresholds; review.yaml must finalize."]
        ),
        "learnings": [
            "IV percentile exit grid evaluated end-to-end (V2 with baseline PnL loading).",
        ],
        "follow_up_actions": (
            ["evidence-only record; do not promote without user approval"]
            if adoption_pass
            else ["review reject reason; do not revive without new mechanism"]
        ),
        "status": "COMPLETE",
        "generated_at": now_str,
        "evaluator_report": evaluator_report,
    }
    (output_dir / "report.yaml").write_text(
        _get_yaml().safe_dump(_plain(report), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # ── l4_ack.yaml ──
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": (
            "IV percentile exit evaluation finished (V2). "
            f"Candidates: {len(all_candidates)}, "
            f"adoption_pass: {adoption_pass}."
        ),
    }
    (output_dir / "l4_ack.yaml").write_text(
        _get_yaml().safe_dump(_plain(l4_ack), allow_unicode=True),
        encoding="utf-8",
    )

    # ── diagnostic.yaml ──
    exit_reason_counts: dict[str, int] = {}
    if best_candidate:
        best_thr_v = best_candidate["iv_percentile_threshold"]
        best_mhd_v = best_candidate["min_hold_days"]
        best_test_res = _simulate_iv_exit(df_test, best_thr_v, best_mhd_v)
        for t in best_test_res.get("trades", []):
            reason = t.get("exit_reason", "unknown")
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1

    vol_missing_train = int(df_train["stock_vol"].isna().sum())
    vol_missing_test = int(df_test["stock_vol"].isna().sum())
    warnings: list[str] = []
    if vol_missing_train > 0:
        pct = vol_missing_train / len(df_train) * 100
        warnings.append(
            f"train: {vol_missing_train} rows missing stock_vol ({pct:.1f}%)"
        )
    if vol_missing_test > 0:
        pct = vol_missing_test / len(df_test) * 100
        warnings.append(
            f"test: {vol_missing_test} rows missing stock_vol ({pct:.1f}%)"
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
    }
    (output_dir / "diagnostic.yaml").write_text(
        _get_yaml().safe_dump(_plain(diagnostic), allow_unicode=True),
        encoding="utf-8",
    )

    return adoption_pass


# ═══════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--iv-percentile-threshold", type=float, default=90.0,
        help="IV percentile threshold for exit (default: 90)",
    )
    parser.add_argument(
        "--min-hold-days", type=int, default=0,
        help="Minimum hold days before IV exit triggers (default: 0)",
    )
    parser.add_argument(
        "--iv-lookback-days", type=int, default=252,
        help="Lookback days for IV percentile computation (default: 252)",
    )
    parser.add_argument(
        "--base-ranks-path",
        default=_GAP_DATA_PATH,
        help="Path to daily value-gap amounts parquet",
    )
    parser.add_argument(
        "--baseline-pnl-path",
        default=_BASELINE_PNL_PATH,
        help="Path to baseline trade PnL parquet",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # ── Step 1: Load gap data ──
    try:
        df_raw = _load_gap_data(args.data_root, args.base_ranks_path)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_gap_data"}
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            _get_yaml().safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        _gatekeeper_after_run(output_dir)
        return 1

    # ── Step 2: Load CB mapping & stock vol ──
    try:
        df_basic = _load_cb_basic(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_cb_basic"}
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            _get_yaml().safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        _gatekeeper_after_run(output_dir)
        return 1

    try:
        df_vol = _load_and_compute_stock_vol(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_stock_vol"}
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            _get_yaml().safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        _gatekeeper_after_run(output_dir)
        return 1

    # ── Step 3: Load baseline PnL ──
    try:
        df_baseline_pnl = _load_baseline_pnl(args.data_root, args.baseline_pnl_path)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_baseline_pnl"}
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            _get_yaml().safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        _gatekeeper_after_run(output_dir)
        return 1

    # ── Step 4: Merge vol → gap → percentile ──
    cb_to_stk: dict[str, str] = dict(
        zip(df_basic["ts_code"].astype(str), df_basic["stk_code"].astype(str))
    )
    df_merged = _merge_vol_into_gap(df_raw, df_vol, cb_to_stk)
    df_merged = _compute_vol_percentile(df_merged)

    # ── Step 5: Filter periods ──
    train_start = _get_pd().Timestamp(args.train_start)
    train_end = _get_pd().Timestamp(args.train_end)
    test_start = _get_pd().Timestamp(args.test_start)
    test_end = _get_pd().Timestamp(args.test_end)

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
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            _get_yaml().safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        _gatekeeper_after_run(output_dir)
        return 1

    # ── Step 6: Baseline metrics from actual trade PnL ──
    baseline_train = _baseline_metrics_from_pnl(
        df_baseline_pnl, train_start, train_end
    )
    baseline_test = _baseline_metrics_from_pnl(
        df_baseline_pnl, test_start, test_end
    )
    baseline_2020 = _baseline_metrics_from_pnl(
        df_baseline_pnl,
        _get_pd().Timestamp("2020-01-01"),
        _get_pd().Timestamp("2020-12-31"),
    )
    # Add Sharpe
    baseline_train["sharpe"] = _compute_sharpe(df_baseline_pnl, train_start, train_end)
    baseline_test["sharpe"] = _compute_sharpe(df_baseline_pnl, test_start, test_end)
    baseline_2020["sharpe"] = _compute_sharpe(
        df_baseline_pnl,
        _get_pd().Timestamp("2020-01-01"),
        _get_pd().Timestamp("2020-12-31"),
    )

    # ── Step 7: Grid search ──
    thresholds = sorted(set(_IV_PERCENTILE_THRESHOLDS + (args.iv_percentile_threshold,)))
    min_holds = sorted(set(_MIN_HOLD_DAYS_SWEEP + (args.min_hold_days,)))

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

            # Compute Sharpe from trade PnL
            def _sharpe_from_trades(trades: list[dict]) -> float:
                if not trades:
                    return 0.0
                tdf = _get_pd().DataFrame(trades)
                daily = tdf.groupby("exit_date")["pnl"].sum()
                m = float(daily.mean())
                s = float(daily.std(ddof=1))
                if s == 0 or _get_np().isnan(s):
                    return 0.0
                return round(m / s * _get_np().sqrt(252), 4)

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
                    "sharpe": _sharpe_from_trades(train_res.get("trades", [])),
                    "excess_return": excess_train,
                },
                "test": {
                    "total_pnl": test_res["total_pnl"],
                    "trade_count": test_res["trade_count"],
                    "win_rate": test_res["win_rate"],
                    "avg_win": test_res["avg_win"],
                    "avg_loss": test_res["avg_loss"],
                    "max_drawdown": test_res["max_drawdown"],
                    "sharpe": _sharpe_from_trades(test_res.get("trades", [])),
                    "excess_return": excess_test,
                },
                "validate_2020": {
                    "total_pnl": yr2020_res["total_pnl"],
                    "trade_count": yr2020_res["trade_count"],
                    "win_rate": yr2020_res["win_rate"],
                    "avg_win": yr2020_res["avg_win"],
                    "avg_loss": yr2020_res["avg_loss"],
                    "max_drawdown": yr2020_res["max_drawdown"],
                    "sharpe": _sharpe_from_trades(yr2020_res.get("trades", [])),
                    "excess_return": excess_2020,
                },
            }
            all_candidates.append(candidate)

            # Score: prefer 2020 excess return (core hypothesis)
            score = excess_2020
            if score > best_score:
                best_score = score
                best_candidate = candidate

    # ── Step 8: Write artifacts ──
    adoption_pass = _write_artifacts(
        output_dir=output_dir,
        best_candidate=best_candidate,
        all_candidates=all_candidates,
        baseline_train=baseline_train,
        baseline_test=baseline_test,
        baseline_2020=baseline_2020,
        thresholds=[float(t) for t in thresholds],
        min_holds=[int(m) for m in min_holds],
        train_period={"start": args.train_start, "end": args.train_end},
        test_period={"start": args.test_start, "end": args.test_end},
        df_train=df_train,
        df_test=df_test,
        df_2020=df_2020,
        cb_to_stk=cb_to_stk,
    )

    best_info = (
        f"thr={best_candidate['iv_percentile_threshold']},mhd={best_candidate['min_hold_days']}"
        if best_candidate
        else "none"
    )
    _gatekeeper_after_run(output_dir)

    print(
        f"[iv_percentile_exit_v2] adoption_pass={adoption_pass} "
        f"best=({best_info}) "
        f"candidates={len(all_candidates)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())