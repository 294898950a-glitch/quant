#!/usr/bin/env python3
"""Evaluate adaptive take-profit and stop-loss exit rules for cb_arb value-gap switch.

Adaptive exit uses two dynamic signals:
  1. Gap closing speed – how fast the value gap is narrowing over a lookback window.
     Fast closing → lower take-profit threshold (exit earlier, lock in gains).
     Slow/negative closing → higher take-profit threshold (give position more room).
  2. Rolling realised PnL feedback – average PnL from recently closed trades.
     Negative recent PnL → tighter stop-loss (cut losses sooner).
     Positive recent PnL → looser stop-loss (let winners run).

The executor runs three periods: train (2019-2024), sealed test (2025-202605),
and 2020 repair stress.  It compares against a simulated baseline (gap>0 enter,
gap<=0 exit only) and computes excess returns.

Entry logic and candidate generation are identical to the baseline
regime-option-entry-gate run.  Only the exit rule is replaced.
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
_BASELINE_TRADE_PNL = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "baseline_trade_pnl.parquet"
)


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG001
    return {
        "required_files": [
            {
                "path": _PREVIOUS_RUN_DATA,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
            },
            {
                "path": _BASELINE_TRADE_PNL,
                "description": "Baseline trade-level PnL (optional; computed on the fly if missing).",
            },
        ]
    }


def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _load_data(data_root: str) -> pd.DataFrame:
    relative_path = Path(_PREVIOUS_RUN_DATA)
    candidates = [
        Path(data_root) / relative_path,
        _REPO_ROOT / relative_path,
        Path.cwd() / relative_path,
    ]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(f"Required data missing; searched: {searched}")
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _try_load_baseline_pnl(data_root: str) -> pd.DataFrame | None:
    relative_path = Path(_BASELINE_TRADE_PNL)
    candidates = [
        Path(data_root) / relative_path,
        _REPO_ROOT / relative_path,
        Path.cwd() / relative_path,
    ]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        return None
    df = pd.read_parquet(path)
    if "exit_date" in df.columns:
        df["exit_date"] = pd.to_datetime(df["exit_date"])
    if "pnl" in df.columns:
        df["pnl"] = df["pnl"].astype(float)
    return df


def _compute_gap_closing_speed(
    df: pd.DataFrame,
    ts_code: str,
    trade_date: pd.Timestamp,
    lookback: int,
) -> float:
    """Compute gap closing speed as a normalised rate over the lookback window.

    Returns a value in [-1, 1]:
      -1  → gap is closing very fast (declining sharply)
       0  → gap is stable
      +1  → gap is widening

    Uses the slope of the gap_amount scaled by the trailing maximum gap
    to avoid over-weighting moves at near-zero gaps.
    """
    mask = (df["ts_code"] == ts_code) & (df["trade_date"] <= trade_date)
    hist = df.loc[mask, ["trade_date", "value_gap_amount"]].sort_values("trade_date").tail(lookback)
    if len(hist) < max(10, lookback // 2):
        return 0.0

    gaps = hist["value_gap_amount"].values.astype(float)
    if len(gaps) < 2:
        return 0.0

    trailing_max = float(np.max(gaps))
    if trailing_max <= 0.0:
        return 0.0

    first = gaps[0]
    last = gaps[-1]
    raw_change = (last - first) / trailing_max
    clamped = float(np.clip(raw_change, -1.0, 1.0))
    return -clamped


def _compute_rolling_pnl_factor(
    trade_pnl_history: pd.DataFrame | None,
    trade_date: pd.Timestamp,
    lookback_days: int,
) -> float:
    """Compute rolling PnL feedback factor from recently closed trades.

    Returns a value in [-0.3, 0.3]:
      Negative → recent losses → tighter exits
      Positive → recent gains  → looser exits
    """
    if trade_pnl_history is None or len(trade_pnl_history) == 0:
        return 0.0

    cutoff = trade_date - pd.Timedelta(days=lookback_days)  # Lookback in calendar days
    recent = trade_pnl_history[
        (trade_pnl_history["exit_date"] >= cutoff) & (trade_pnl_history["exit_date"] <= trade_date)
    ]

    if len(recent) < 3:
        return 0.0

    mean_pnl = float(recent["pnl"].mean())
    abs_pnl = float(recent["pnl"].abs().mean())
    if abs_pnl < 0.01:
        return 0.0

    raw = mean_pnl / abs_pnl
    return float(np.clip(raw * 0.3, -0.3, 0.3))


def _simulate_adaptive(
    df: pd.DataFrame,
    tp_gap_fraction: float,
    sl_gap_fraction: float,
    gap_speed_lookback: int,
    pnl_lookback: int,
    min_hold_days: int,
    trade_pnl_history: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Simulate adaptive exit with gap closing speed and PnL feedback.

    Entry: gap > 0 and flat → enter, record entry_gap.
    Exit:
      - gap <= 0 → exit ("gap_closed"), always enforced
      - dynamic TP:  current_gap / entry_gap < dynamic_tp_gate → exit ("tp")
      - dynamic SL:  current_gap / entry_gap > dynamic_sl_gate → exit ("sl")

    Dynamic TP = tp_gap_fraction * (1 + speed_signal)
      speed_signal ∈ [-0.5, 0.5]: fast closing → lower TP

    Dynamic SL = sl_gap_fraction * (1 - pnl_factor)
      pnl_factor ∈ [-0.3, 0.3]: negative PnL → lower SL → tighter stop

    TP/SL exits only apply after min_hold_days.
    """
    df = df.copy()
    df["position"] = 0
    df["entry_gap"] = 0.0

    total_pnl = 0.0
    trades: list[dict[str, Any]] = []
    live_trade_pnl: list[dict[str, Any]] = []

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        idx_list = grp.index.tolist()
        in_position = False
        entry_gap_val = 0.0
        entry_date: pd.Timestamp | None = None

        for idx in idx_list:
            row = grp.loc[idx]
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = trade_date
                df.at[idx, "position"] = 1
                df.at[idx, "entry_gap"] = entry_gap_val
                continue

            if in_position:
                df.at[idx, "position"] = 1
                df.at[idx, "entry_gap"] = entry_gap_val

                assert entry_date is not None
                hold_days = (trade_date - entry_date).days
                gap_ratio = gap / entry_gap_val if entry_gap_val > 0 else 0.0

                should_exit = False
                exit_reason = ""

                # 1. Gap closed → always exit
                if gap <= 0:
                    should_exit = True
                    exit_reason = "gap_closed"

                # 2 & 3. Adaptive TP and SL (only after min_hold_days)
                elif hold_days >= min_hold_days:
                    closing_speed = _compute_gap_closing_speed(
                        df, stock, trade_date, gap_speed_lookback
                    )
                    pnl_factor = _compute_rolling_pnl_factor(
                        live_trade_pnl, trade_date, pnl_lookback
                    )

                    speed_signal = float(np.clip(closing_speed * 0.5, -0.5, 0.5))
                    dynamic_tp = tp_gap_fraction * (1.0 + speed_signal)
                    dynamic_sl = min(sl_gap_fraction * (1.0 - pnl_factor), 1.0)

                    if gap_ratio < dynamic_tp:
                        should_exit = True
                        exit_reason = "tp"
                    elif gap_ratio > dynamic_sl:
                        should_exit = True
                        exit_reason = "sl"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    trade = {
                        "stock": stock,
                        "entry_date": str(entry_date.date()),
                        "exit_date": str(trade_date.date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": round(pnl, 2),
                        "hold_days": hold_days,
                    }
                    trades.append(trade)
                    live_trade_pnl.append({
                        "exit_date": trade_date,
                        "pnl": pnl,
                    })
                    in_position = False
                    entry_gap_val = 0.0
                    entry_date = None

        # Force-close any still-open position at end of data
        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            assert entry_date is not None
            hold_days = (last_row["trade_date"] - entry_date).days
            trade = {
                "stock": stock,
                "entry_date": str(entry_date.date()),
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            }
            trades.append(trade)
            live_trade_pnl.append({
                "exit_date": last_row["trade_date"],
                "pnl": pnl,
            })

    live_trade_pnl_df = pd.DataFrame(live_trade_pnl) if live_trade_pnl else pd.DataFrame()
    return _aggregate_metrics(trades, total_pnl, live_trade_pnl_df)


def _simulate_baseline(df: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    """Simulate baseline: enter when gap > 0, exit when gap <= 0."""
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []
    live_trade_pnl: list[dict[str, Any]] = []

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date: pd.Timestamp | None = None

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
                assert entry_date is not None
                hold_days = (trade_date - entry_date).days
                trades.append({
                    "stock": stock,
                    "entry_date": str(entry_date.date()),
                    "exit_date": str(trade_date.date()),
                    "exit_reason": "gap_closed",
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": round(pnl, 2),
                    "hold_days": hold_days,
                })
                live_trade_pnl.append({"exit_date": trade_date, "pnl": pnl})
                in_position = False
                entry_gap_val = 0.0
                entry_date = None

        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            assert entry_date is not None
            hold_days = (last_row["trade_date"] - entry_date).days
            trades.append({
                "stock": stock,
                "entry_date": str(entry_date.date()),
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })
            live_trade_pnl.append({"exit_date": last_row["trade_date"], "pnl": pnl})

    live_trade_pnl_df = pd.DataFrame(live_trade_pnl) if live_trade_pnl else pd.DataFrame()
    result, _ = _aggregate_metrics(trades, total_pnl, live_trade_pnl_df)
    return result, live_trade_pnl_df


def _aggregate_metrics(
    trades: list[dict[str, Any]],
    total_pnl: float,
    trade_pnl_df: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Compute performance metrics from trade list."""
    if not trades:
        return {
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "max_hold_days": 0,
            "trades": [],
        }, trade_pnl_df

    trades_df = pd.DataFrame(trades)
    winning = trades_df[trades_df["pnl"] > 0]
    losing = trades_df[trades_df["pnl"] <= 0]
    win_rate = round(len(winning) / len(trades_df), 4) if len(trades_df) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(trades_df)
    max_hold = int(trades_df["hold_days"].max()) if "hold_days" in trades_df.columns else 0

    trades_sorted = trades_df.sort_values("exit_date")
    trades_sorted["cum_pnl"] = trades_sorted["pnl"].cumsum()
    equity_series = trades_sorted["cum_pnl"]
    peak = equity_series.iloc[0]
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
        "max_hold_days": max_hold,
        "trades": trades,
    }, trade_pnl_df


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


def _compute_params_summary(
    tp_gap_fraction: float,
    sl_gap_fraction: float,
    gap_speed_lookback: int,
    pnl_lookback: int,
    min_hold_days: int,
) -> dict[str, Any]:
    return {
        "tp_gap_fraction": tp_gap_fraction,
        "sl_gap_fraction": sl_gap_fraction,
        "gap_speed_lookback": gap_speed_lookback,
        "pnl_lookback": pnl_lookback,
        "min_hold_days": min_hold_days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument("--train-start", required=True, help="Train period start (YYYYMMDD)")
    parser.add_argument("--train-end", required=True, help="Train period end (YYYYMMDD)")
    parser.add_argument("--test-start", required=True, help="Test period start (YYYYMMDD)")
    parser.add_argument("--test-end", required=True, help="Test period end (YYYYMMDD)")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    parser.add_argument("--gap-speed-lookback", type=int, default=20,
                        help="Lookback days for gap closing speed calculation")
    parser.add_argument("--pnl-lookback", type=int, default=60,
                        help="Lookback days for rolling PnL feedback")
    parser.add_argument("--tp-gap-fraction", type=float, default=0.3,
                        help="Base take-profit gap fraction threshold")
    parser.add_argument("--sl-gap-fraction", type=float, default=0.8,
                        help="Base stop-loss gap fraction threshold")
    parser.add_argument("--min-hold-days", type=int, default=5,
                        help="Minimum hold days before TP/SL can trigger")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # Load data
    try:
        df_raw = _load_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[pnl_adaptive_exit] FATAL: {exc}", flush=True)
        return 1

    # Filter periods
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_raw[(df_raw["trade_date"] >= train_start) & (df_raw["trade_date"] <= train_end)].copy()
    df_test = df_raw[(df_raw["trade_date"] >= test_start) & (df_raw["trade_date"] <= test_end)].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        return 1

    # Run baseline first (used for comparison)
    baseline_train, _ = _simulate_baseline(df_train)
    baseline_test, _ = _simulate_baseline(df_test)
    baseline_2020, _ = _simulate_baseline(df_2020)

    # Run adaptive strategy
    strategy_train, trade_pnl_train = _simulate_adaptive(
        df_train,
        tp_gap_fraction=args.tp_gap_fraction,
        sl_gap_fraction=args.sl_gap_fraction,
        gap_speed_lookback=args.gap_speed_lookback,
        pnl_lookback=args.pnl_lookback,
        min_hold_days=args.min_hold_days,
        trade_pnl_history=None,
    )
    strategy_test, _ = _simulate_adaptive(
        df_test,
        tp_gap_fraction=args.tp_gap_fraction,
        sl_gap_fraction=args.sl_gap_fraction,
        gap_speed_lookback=args.gap_speed_lookback,
        pnl_lookback=args.pnl_lookback,
        min_hold_days=args.min_hold_days,
        trade_pnl_history=trade_pnl_train,
    )
    strategy_2020, _ = _simulate_adaptive(
        df_2020,
        tp_gap_fraction=args.tp_gap_fraction,
        sl_gap_fraction=args.sl_gap_fraction,
        gap_speed_lookback=args.gap_speed_lookback,
        pnl_lookback=args.pnl_lookback,
        min_hold_days=args.min_hold_days,
        trade_pnl_history=None,
    )

    excess_train = _compute_excess_return(strategy_train["total_pnl"], baseline_train["total_pnl"])
    excess_test = _compute_excess_return(strategy_test["total_pnl"], baseline_test["total_pnl"])
    excess_2020 = _compute_excess_return(strategy_2020["total_pnl"], baseline_2020["total_pnl"])

    params = _compute_params_summary(
        args.tp_gap_fraction, args.sl_gap_fraction,
        args.gap_speed_lookback, args.pnl_lookback, args.min_hold_days,
    )

    # Success criteria from proposal:
    #   train excess >= 0.15
    #   test excess >= 0.33
    #   2020 repair excess >= -0.15
    #   win rate >= 0.55 on test
    #   max drawdown test <= -0.15
    #   no max_hold > 90 days dominance
    criteria = {
        "train_excess_ok": excess_train >= 0.15,
        "test_excess_ok": excess_test >= 0.33,
        "validate_excess_ok": excess_2020 >= -0.15,
        "test_winrate_ok": strategy_test["win_rate"] >= 0.55,
        "test_maxdd_ok": strategy_test["max_drawdown"] >= -0.15,
        "no_signal_collapse": strategy_train.get("max_hold_days", 0) <= 90,
    }
    adoption_pass = all(criteria.values())

    candidate = {
        "params": params,
        "train": {
            "total_pnl": strategy_train["total_pnl"],
            "trade_count": strategy_train["trade_count"],
            "win_rate": strategy_train["win_rate"],
            "avg_win": strategy_train["avg_win"],
            "avg_loss": strategy_train["avg_loss"],
            "max_drawdown": strategy_train["max_drawdown"],
            "max_hold_days": strategy_train.get("max_hold_days", 0),
            "excess_return": excess_train,
        },
        "test": {
            "total_pnl": strategy_test["total_pnl"],
            "trade_count": strategy_test["trade_count"],
            "win_rate": strategy_test["win_rate"],
            "avg_win": strategy_test["avg_win"],
            "avg_loss": strategy_test["avg_loss"],
            "max_drawdown": strategy_test["max_drawdown"],
            "max_hold_days": strategy_test.get("max_hold_days", 0),
            "excess_return": excess_test,
        },
        "validate_2020": {
            "total_pnl": strategy_2020["total_pnl"],
            "trade_count": strategy_2020["trade_count"],
            "win_rate": strategy_2020["win_rate"],
            "avg_win": strategy_2020["avg_win"],
            "avg_loss": strategy_2020["avg_loss"],
            "excess_return": excess_2020,
        },
        "criteria_check": criteria,
    }

    # summary.json
    summary = {
        "adoption_pass": adoption_pass,
        "candidate": candidate,
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
        "params": params,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    report = {
        "proposal_id": "adaptive_exit_rule_2026-05-19",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "pnl_adaptive_exit",
        "adoption_pass": adoption_pass,
        "params": params,
        "candidate": candidate,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
        },
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True), encoding="utf-8"
    )

    # l4_ack.yaml
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": "PnL adaptive exit evaluation finished.",
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8"
    )

    # diagnostic.yaml
    exit_reason_counts = {}
    for t in strategy_test.get("trades", []):
        reason = t.get("exit_reason", "unknown")
        exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1

    diagnostic = {
        "warnings": [],
        "errors": [],
        "data_rows": {
            "train": len(df_train),
            "test": len(df_test),
            "validate_2020": len(df_2020),
        },
        "test_exit_reasons": exit_reason_counts,
        "criteria_check": criteria,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True), encoding="utf-8"
    )

    print(
        f"[pnl_adaptive_exit] adoption_pass={adoption_pass} "
        f"excess_train={excess_train} excess_test={excess_test} "
        f"excess_2020={excess_2020} "
        f"max_hold={strategy_train.get('max_hold_days', 0)}d",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
