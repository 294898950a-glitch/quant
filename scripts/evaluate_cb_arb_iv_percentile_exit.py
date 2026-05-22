#!/usr/bin/env python3
"""Evaluate IV-regime exit for cb_arb value-gap switch strategy.

Exit rule: when a held CB's rolling stock volatility (proxy for implied
volatility) exceeds a high historical percentile, exit the position.
This overrides the standard gap-based exit to reduce losses during
market stress.

IV proxy: rolling 20-day annualised stock volatility computed from
forward-adjusted stock prices in stk_daily_qfq.parquet.

Grid search over iv_percentile_threshold (80, 85, 90, 95) and
min_hold_days (0, 1). Compares against baseline (gap <= 0 exit only)
on train, 2020 repair, and test periods.
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


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

_PREVIOUS_RUN_DATA = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_CB_BASIC_PATH = "data/cb_warehouse/cb_basic.parquet"
_STK_DAILY_PATH = "data/cb_warehouse/stk_daily_qfq.parquet"
_IV_PERCENTILE_THRESHOLDS = (80, 85, 90, 95)
_MIN_HOLD_DAYS_SWEEP = (0, 1)
_VOL_LOOKBACK = 20  # trading days for rolling stock vol
_PERCENTILE_LOOKBACK = 252  # trading days for IV percentile


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _PREVIOUS_RUN_DATA,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
                "required_columns": ["trade_date", "ts_code", "value_gap_amount"],
            },
            {
                "path": _CB_BASIC_PATH,
                "description": "CB-to-underlying-stock mapping used to join stock volatility.",
                "required_columns": ["ts_code", "stk_code"],
            },
            {
                "path": _STK_DAILY_PATH,
                "description": "Forward-adjusted daily stock prices for volatility calculation.",
                "required_columns": ["stk_code", "trade_date", "close"],
            },
        ],
        "generated_columns": {
            "stk_code": "Derived for value-gap rows by joining cb_basic.ts_code to cb_basic.stk_code.",
            "stock_vol": "Computed inside the executor from stk_daily_qfq close returns.",
            "vol_pctile": "Computed inside the executor from rolling stock_vol history.",
        },
    }


def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _resolve_path(relative: str, data_root: str) -> Path:
    rel = Path(relative)
    candidates = [Path(data_root) / rel, _REPO_ROOT / rel, Path.cwd() / rel]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(f"Required data missing; searched: {searched}")
    return path


def _load_gap_data(data_root: str) -> pd.DataFrame:
    path = _resolve_path(_PREVIOUS_RUN_DATA, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_cb_basic(data_root: str) -> pd.DataFrame:
    path = _resolve_path(_CB_BASIC_PATH, data_root)
    df = pd.read_parquet(path)
    missing = {"ts_code", "stk_code"} - set(df.columns)
    if missing:
        raise ValueError(f"cb_basic missing columns: {sorted(missing)}")
    return df[["ts_code", "stk_code"]].dropna().drop_duplicates()


def _load_and_compute_stock_vol(data_root: str) -> pd.DataFrame:
    """Compute rolling 20-day annualised stock vol for each stock."""
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

    return df[["stk_code", "trade_date", "close", "stock_vol"]]


def _merge_vol_into_gap(df_gap: pd.DataFrame, df_vol: pd.DataFrame) -> pd.DataFrame:
    """Merge stock volatility into the gap data by matching stk_code and trade_date.

    CB ts_code → stock code mapping: for CB ts_code like '128037.SZ',
    extract the stk_code (e.g. '000610.SZ') from cb_basic. But the gap
    data already comes from the strategy framework which uses stk_code
    for its underlying. Try matching 'stk_code' in df_gap if present,
    otherwise fall back to matching ts_code prefix.

    The daily_value_gap_amounts has ts_code (CB code) but the stock
    vol is keyed by stk_code. We build a mapping from ts_code to
    stk_code using the stk_daily_qfq data which has both.
    """
    df = df_gap.copy()

    # If df_gap already has stk_code, use it directly for merge
    if "stk_code" in df.columns:
        df_vol_renamed = df_vol.rename(columns={"stk_code": "stk_code"})
        merged = df.merge(
            df_vol_renamed[["stk_code", "trade_date", "stock_vol"]],
            on=["stk_code", "trade_date"],
            how="left",
        )
    else:
        # The gap data uses ts_code (CB code like 128037.SZ)
        # Build mapping: for each CB ts_code, find matching stock prices
        # by trying to match via a bridge. Since we don't have cb_basic loaded
        # here, we use a simple heuristic: the CB code prefix often maps to
        # stock code. But a more reliable approach: just use the stk_daily
        # with the stock code in the gap data if present.
        # Fallback: merge on trade_date only and compute vol percentile per-date
        merged = df.copy()
        merged["stock_vol"] = np.nan

    return merged


def _compute_vol_percentile(df: pd.DataFrame) -> pd.DataFrame:
    """For each CB, compute the rolling percentile rank of current vol
    within the trailing _PERCENTILE_LOOKBACK window.

    Returns df with added 'vol_pctile' column (0-100).
    """
    df = df.copy()
    df["vol_pctile"] = np.nan

    for ts_code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        vols = grp["stock_vol"].values
        pctiles = np.full(len(vols), np.nan)

        for i in range(len(vols)):
            window_start = max(0, i - _PERCENTILE_LOOKBACK + 1)
            window_vols = vols[window_start:i + 1]
            valid = window_vols[~np.isnan(window_vols)]
            if len(valid) < 60:  # need at least ~3 months of data
                continue
            current = vols[i]
            if np.isnan(current):
                continue
            rank = (valid < current).sum() + 0.5 * (valid == current).sum()
            pctiles[i] = (rank / len(valid)) * 100.0

        df.loc[grp.index, "vol_pctile"] = pctiles

    return df


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
                hold_days = (trade_date - entry_date).days if entry_date else 0
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
            hold_days = (last_row["trade_date"] - entry_date).days if entry_date else 0
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
    df: pd.DataFrame,
    iv_percentile_threshold: float,
    min_hold_days: int,
) -> dict[str, Any]:
    """Simulate IV-regime exit strategy.

    Entry: gap > 0 and flat → enter.
    Exit:
      - gap <= 0 → exit ("gap_closed"), always enforced
      - vol_pctile > iv_percentile_threshold (after min_hold_days) → exit ("iv_spike")
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
                elif min_hold_days == 0 or (trade_date - entry_date).days >= min_hold_days:
                    vol_pct = row.get("vol_pctile")
                    if vol_pct is not None and not (isinstance(vol_pct, float) and np.isnan(vol_pct)):
                        if float(vol_pct) > iv_percentile_threshold:
                            should_exit = True
                            exit_reason = "iv_spike"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    hold_days = (trade_date - entry_date).days if entry_date else 0
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
            hold_days = (last_row["trade_date"] - entry_date).days if entry_date else 0
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
    trades: list[dict[str, Any]],
    total_pnl: float,
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
    win_rate = round(len(winning) / len(trades_df), 4) if len(trades_df) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
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


def _compute_excess_return(strategy_pnl: float, baseline_pnl: float) -> float:
    return round(strategy_pnl - baseline_pnl, 2)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument("--train-start", required=True, help="Train period start (YYYYMMDD)")
    parser.add_argument("--train-end", required=True, help="Train period end (YYYYMMDD)")
    parser.add_argument("--test-start", required=True, help="Test period start (YYYYMMDD)")
    parser.add_argument("--test-end", required=True, help="Test period end (YYYYMMDD)")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    parser.add_argument("--iv-percentile-threshold", type=float, default=90.0,
                        help="IV percentile threshold for exit (default: 90)")
    parser.add_argument("--min-hold-days", type=int, default=0,
                        help="Minimum hold days before IV exit can trigger (default: 0)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # ── Load gap data ──
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_gap_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[iv_percentile_exit] FATAL: {exc}", flush=True)
        return 1

    # ── Load CB-to-stock map and stock vol data ──
    try:
        df_basic = _load_cb_basic(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_cb_basic"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[iv_percentile_exit] FATAL: {exc}", flush=True)
        return 1

    try:
        df_vol = _load_and_compute_stock_vol(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_stock_vol"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[iv_percentile_exit] FATAL: {exc}", flush=True)
        return 1

    # ── Merge vol into gap data and compute percentile ──
    cb_to_stk = dict(zip(df_basic["ts_code"].astype(str), df_basic["stk_code"].astype(str)))
    df_enriched = df_raw.copy()
    df_enriched["stk_code"] = df_enriched["ts_code"].astype(str).map(cb_to_stk)

    # Merge stock vol
    df_vol_lean = df_vol[["stk_code", "trade_date", "stock_vol"]].copy()
    df_merged = df_enriched.merge(
        df_vol_lean,
        on=["stk_code", "trade_date"],
        how="left",
    )

    # Compute vol percentile
    df_merged = _compute_vol_percentile(df_merged)

    # ── Filter periods ──
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_merged[
        (df_merged["trade_date"] >= train_start) & (df_merged["trade_date"] <= train_end)
    ].copy()
    df_test = df_merged[
        (df_merged["trade_date"] >= test_start) & (df_merged["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        return 1

    # ── Baseline ──
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    # ── Grid search ──
    # Use specified threshold plus sweep values
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

            # Score: prefer 2020 excess return (the core hypothesis)
            score = excess_2020
            if score > best_score:
                best_score = score
                best_candidate = candidate

    # ── Adoption criteria ──
    # From proposal:
    # - 2020 max drawdown reduced vs baseline
    # - 2020 excess return > 0 (less negative)
    # - Test period excess return not significantly worse
    adoption_pass = False
    if best_candidate is not None:
        dd_2020_improved = (
            best_candidate["validate_2020"]["max_drawdown"]
            > baseline_2020["max_drawdown"]
        )
        excess_2020_positive = best_candidate["validate_2020"]["excess_return"] > 0
        test_not_worse = best_candidate["test"]["excess_return"] > -50  # reasonable tol

        adoption_pass = dd_2020_improved and excess_2020_positive and test_not_worse

    # ── summary.json ──
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
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # ── report.yaml (framework HDRF schema) ──
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    _today = _now.split("T", 1)[0]
    l6_decision = "adopt" if adoption_pass else "reject"
    evaluator_report = {
        "proposal_id": "cb_arb_value_gap_switch_iv-regime-exit_2026-05-21",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "iv_percentile_exit",
        "adoption_pass": adoption_pass,
        "best_iv_percentile_threshold": (
            best_candidate["iv_percentile_threshold"] if best_candidate else None
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
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": _today,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "best_iv_percentile_threshold": evaluator_report["best_iv_percentile_threshold"],
            "best_min_hold_days": evaluator_report["best_min_hold_days"],
            "evaluator": "iv_percentile_exit",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [f"variants below {output_dir.name} best by adoption criteria — evidence only, not promoted"]
            if adoption_pass
            else [f"{output_dir.name}: rejected by mechanical thresholds; review.yaml must finalize."]
        ),
        "learnings": [
            "IV percentile exit grid evaluated end-to-end.",
        ],
        "follow_up_actions": (
            ["evidence-only record; do not promote without user approval"]
            if adoption_pass
            else ["review reject reason; do not revive without new mechanism"]
        ),
        "status": "COMPLETE",
        "generated_at": _now,
        "evaluator_report": evaluator_report,
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # ── l4_ack.yaml ──
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": "IV percentile exit evaluation finished.",
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8"
    )

    # ── diagnostic.yaml ──
    # Count exit reasons for best candidate
    exit_reason_counts: dict[str, int] = {}
    if best_candidate:
        # Re-simulate with best params to get trade-level data
        best_thr = best_candidate["iv_percentile_threshold"]
        best_mhd = best_candidate["min_hold_days"]
        best_test_res = _simulate_iv_exit(df_test, best_thr, best_mhd)
        for t in best_test_res.get("trades", []):
            reason = t.get("exit_reason", "unknown")
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1

    # Count NaN/missing vol data
    vol_missing_train = int(df_train["stock_vol"].isna().sum())
    vol_missing_test = int(df_test["stock_vol"].isna().sum())
    warnings: list[str] = []
    if vol_missing_train > 0:
        warnings.append(
            f"train: {vol_missing_train} rows missing stock_vol "
            f"({vol_missing_train/len(df_train)*100:.1f}%)"
        )
    if vol_missing_test > 0:
        warnings.append(
            f"test: {vol_missing_test} rows missing stock_vol "
            f"({vol_missing_test/len(df_test)*100:.1f}%)"
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
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True), encoding="utf-8"
    )

    best_info = f"thr={best_candidate['iv_percentile_threshold']},mhd={best_candidate['min_hold_days']}" if best_candidate else "none"
    print(
        f"[iv_percentile_exit] adoption_pass={adoption_pass} "
        f"best=({best_info}) "
        f"candidates={len(all_candidates)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
