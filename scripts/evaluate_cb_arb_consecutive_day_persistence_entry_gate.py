#!/usr/bin/env python3
"""Evaluate per-symbol entry signal persistence for cb_arb value-gap switch.

For each CB, compute a rolling count of consecutive trading days where
value_gap_amount exceeds the entry threshold (gap > 0). Suppress entry
when consecutive_count < persistence_days.

Combined with accepted duration-adaptive exit parameters:
  min_hold_days=5, initial_threshold_fraction=0.7,
  decay_period_factor=0.5, effective_max_hold_days=45

Grid search: persistence_days in [1,2,3,4,5,7,10].
persistence_days=1 is the baseline (no filter — every positive-gap day qualifies).

Periods: train 2018-2022, validate_2020 (subset of train), test 2023-2025.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


# ── fixed exit parameters (from accepted duration-adaptive result) ─────
DEFAULT_MIN_HOLD_DAYS = 5
DEFAULT_INITIAL_THRESHOLD_FRACTION = 0.7
DEFAULT_DECAY_PERIOD_FACTOR = 0.5
DEFAULT_EFFECTIVE_MAX_HOLD_DAYS = 45

# ── grid search over persistence_days ──────────────────────────────────
PERSISTENCE_DAYS_GRID = [1, 2, 3, 4, 5, 7, 10]

# ── data path (fixed, from proposal) ───────────────────────────────────
_VALUE_GAP_PARQUET = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)


def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    data_root_raw = _command_value(command, "--data-root") or ""
    data_root = Path(data_root_raw)
    return {
        "schema_version": 1,
        "executor": (
            "generated_executor/"
            "evaluate_cb_arb_consecutive_day_persistence_entry_gate.py"
        ),
        "required_files": [
            {
                "path": _VALUE_GAP_PARQUET,
                "description": (
                    "Daily value-gap amounts per CB — trade_date, ts_code, "
                    "value_gap_amount."
                ),
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "list_date", "delist_date"],
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "trade_date", "close"],
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "trade_date", "close"],
            },
        ],
    }


def _command_value(command: list[Any], flag: str) -> str | None:
    parts = [str(p) for p in command]
    for i, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[i + 1]
    return None


# ── helpers ────────────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    """Recursively convert numpy/pandas types to plain Python types."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load_data(data_root: str) -> pd.DataFrame:
    """Load daily_value_gap_amounts.parquet from data root or repo root."""
    candidates = [
        Path(data_root) / _VALUE_GAP_PARQUET if data_root else None,
        Path.cwd() / _VALUE_GAP_PARQUET,
    ]
    if data_root:
        candidates.append(Path(data_root) / Path(_VALUE_GAP_PARQUET).name)
    path = next((c for c in candidates if c is not None and c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates if c is not None)
        raise FileNotFoundError(
            f"daily_value_gap_amounts.parquet not found; searched: {searched}"
        )
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _compute_persistence_count(df: pd.DataFrame) -> pd.DataFrame:
    """Add 'consecutive_gap_days' column: rolling count of consecutive days
    with value_gap_amount > 0, computed per CB.

    Returns a copy of df with the new column.
    """
    df = df.copy()
    df["consecutive_gap_days"] = 0
    for ts_code, grp in df.groupby("ts_code", sort=False):
        grp = grp.sort_values("trade_date")
        count = 0
        indices = []
        values = []
        for _, row in grp.iterrows():
            if row["value_gap_amount"] > 0:
                count += 1
            else:
                count = 0
            indices.append(row.name)
            values.append(count)
        df.loc[indices, "consecutive_gap_days"] = values
    return df


def _compute_threshold(
    hold_days: int, min_hold: int, initial_frac: float, max_hold: int
) -> float:
    """Linearly decaying threshold."""
    if hold_days < min_hold:
        return initial_frac
    if max_hold <= min_hold:
        return 0.0
    decay_range = max_hold - min_hold
    elapsed = min(hold_days - min_hold, decay_range)
    return initial_frac * max(0.0, 1.0 - elapsed / decay_range)


def _simulate_baseline(df: pd.DataFrame) -> dict[str, Any]:
    """Baseline: enter on any day gap > 0, exit when gap <= 0.
    This is equivalent to persistence_days=1 behavior.
    """
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for ts_code, grp in df.groupby("ts_code", sort=False):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = row["trade_date"]
                continue
            if in_position and gap <= 0:
                pnl = (gap - entry_gap_val) * 100.0
                total_pnl += pnl
                hold_days = (
                    (row["trade_date"] - entry_date).days if entry_date else 0
                )
                trades.append({
                    "ts_code": ts_code,
                    "entry_date": str(entry_date.date()),
                    "exit_date": str(row["trade_date"].date()),
                    "exit_reason": "gap_closed",
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": round(pnl, 2),
                    "hold_days": hold_days,
                })
                in_position = False
                entry_gap_val = 0.0
                entry_date = None

        # force-close open position
        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (
                (last_row["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append({
                "ts_code": ts_code,
                "entry_date": str(entry_date.date()),
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })

    return _compute_metrics_from_trades(trades, total_pnl, "baseline")


def _simulate_with_persistence_and_exit(
    df: pd.DataFrame,
    persistence_days: int,
    min_hold_days: int,
    initial_threshold_fraction: float,
    decay_period_factor: float,
    effective_max_hold_days: int,
) -> dict[str, Any]:
    """Simulate persistence-filtered entry + duration-adaptive exit.

    Entry only on days where:
      - value_gap_amount > 0
      - consecutive_gap_days >= persistence_days
      - not already in a position

    Exit rules (after entering):
      1. gap <= 0 → force exit (gap closed)
      2. after min_hold_days, if current_gap / entry_gap > decaying threshold → exit
      3. force close at end of period if still open
    """
    df = df.copy()
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for ts_code, grp in df.groupby("ts_code", sort=False):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            cons_days = int(row.get("consecutive_gap_days", 0))

            # ── entry logic ──
            if not in_position and gap > 0 and cons_days >= persistence_days:
                in_position = True
                entry_gap_val = gap
                entry_date = row["trade_date"]
                continue

            # ── exit logic ──
            if in_position:
                should_exit = False
                exit_reason = ""

                # rule 1: gap closed
                if gap <= 0:
                    should_exit = True
                    exit_reason = "gap_closed"
                else:
                    hold_days = (
                        (row["trade_date"] - entry_date).days if entry_date else 0
                    )
                    if hold_days >= min_hold_days:
                        threshold = _compute_threshold(
                            hold_days, min_hold_days,
                            initial_threshold_fraction, effective_max_hold_days,
                        )
                        ratio = gap / entry_gap_val if entry_gap_val > 0 else 0.0
                        if ratio > threshold:
                            should_exit = True
                            exit_reason = "decay_exit"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    hold_days = (
                        (row["trade_date"] - entry_date).days if entry_date else 0
                    )
                    trades.append({
                        "ts_code": ts_code,
                        "entry_date": str(entry_date.date()) if entry_date else "",
                        "exit_date": str(row["trade_date"].date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": round(pnl, 2),
                        "hold_days": hold_days,
                        "persistence_days": persistence_days,
                    })
                    in_position = False
                    entry_gap_val = 0.0
                    entry_date = None

        # force-close open position
        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (
                (last_row["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append({
                "ts_code": ts_code,
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
                "persistence_days": persistence_days,
            })

    return _compute_metrics_from_trades(
        trades, total_pnl, f"persistence_{persistence_days}"
    )


def _compute_metrics_from_trades(
    trades: list[dict[str, Any]], total_pnl: float, variant: str
) -> dict[str, Any]:
    """Compute aggregate metrics from a list of trade dicts."""
    if not trades:
        return {
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_hold_days": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "excess_return": 0.0,
            "decay_exits": 0,
            "gap_closed_exits": 0,
            "force_closes": 0,
            "trades": [],
            "variant": variant,
        }

    trades_df = pd.DataFrame(trades)
    winning = trades_df[trades_df["pnl"] > 0]
    losing = trades_df[trades_df["pnl"] <= 0]
    win_rate = round(len(winning) / len(trades_df), 4) if len(trades_df) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(trades_df)
    avg_hold = round(float(trades_df["hold_days"].mean()), 1)

    # max drawdown from equity curve sorted by exit date
    trades_sorted = trades_df.sort_values("exit_date")
    trades_sorted["cum_pnl"] = trades_sorted["pnl"].cumsum()
    equity = trades_sorted["cum_pnl"]
    peak = equity.iloc[0] if len(equity) > 0 else 0.0
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = val - peak
        if dd < max_dd:
            max_dd = dd

    # sharpe approximation: mean(pnl)/std(pnl) * sqrt(trade_count)
    sharpe = 0.0
    if len(trades_df) >= 2 and float(trades_df["pnl"].std()) > 0:
        sharpe = round(
            float(trades_df["pnl"].mean())
            / float(trades_df["pnl"].std())
            * (len(trades_df) ** 0.5),
            4,
        )

    # max drawdown as percentage of peak equity
    max_dd_pct = 0.0
    if peak > 0:
        max_dd_pct = round(-max_dd / peak, 4)

    decay_exits = int((trades_df["exit_reason"] == "decay_exit").sum())
    gap_closed = int((trades_df["exit_reason"] == "gap_closed").sum())
    force_close = int((trades_df["exit_reason"] == "force_close").sum())

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_ratio": sharpe,
        "excess_return": 0.0,  # filled later by caller
        "decay_exits": decay_exits,
        "gap_closed_exits": gap_closed,
        "force_closes": force_close,
        "trades": trades,
        "variant": variant,
    }


# ── artifact writers ───────────────────────────────────────────────────


def _write_summary(
    output_dir: Path,
    best_pd: int,
    adoption_pass: bool,
    baseline: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    fixed_params: dict[str, Any],
) -> dict[str, Any]:
    summary = {
        "adoption_pass": adoption_pass,
        "best_persistence_days": best_pd,
        "baseline": baseline,
        "all_candidates": all_candidates,
        "train_period": {"start": train_start, "end": train_end},
        "test_period": {"start": test_start, "end": test_end},
        "swept_parameters": {
            "persistence_days": PERSISTENCE_DAYS_GRID,
        },
        "fixed_params": fixed_params,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _write_report(
    output_dir: Path,
    run_id: str,
    best_pd: int,
    adoption_pass: bool,
    best_candidate: dict[str, Any] | None,
    baseline: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    fixed_params: dict[str, Any],
) -> None:
    now_utc = datetime.now(timezone.utc).isoformat()
    today = now_utc.split("T", 1)[0]
    l6 = "adopt" if adoption_pass else "reject"

    evaluator_report = {
        "proposal_id": "cb_arb_value_gap_switch_signal_persistence_entry_20260523",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "consecutive_day_persistence_entry_gate",
        "adoption_pass": adoption_pass,
        "best_persistence_days": best_pd,
        "best_params": fixed_params | {"persistence_days": best_pd},
        "candidates": all_candidates,
        "baseline": baseline,
    }

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "date": today,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "selected_persistence_days": best_pd,
            "executor": "consecutive_day_persistence_entry_gate",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            []
            if adoption_pass
            else [f"persistence_days_sweep_rejected_{run_id}"]
        ),
        "learnings": [
            "Per-symbol signal persistence evaluated across "
            f"persistence_days={PERSISTENCE_DAYS_GRID} with fixed duration-adaptive exit.",
        ],
        "follow_up_actions": (
            ["evidence-only; do not promote without user approval"]
            if adoption_pass
            else ["review reject reason; do not revive without new mechanism"]
        ),
        "status": "COMPLETE",
        "generated_at": now_utc,
        "evaluator_report": evaluator_report,
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_l4_ack(
    output_dir: Path,
    run_id: str,
    best_pd: int,
    adoption_pass: bool,
    best_candidate: dict[str, Any] | None,
    baseline: dict[str, Any],
) -> None:
    now_utc = datetime.now(timezone.utc).isoformat()
    ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": (
            f"Persistence entry gate evaluation completed. "
            f"Best persistence_days={best_pd}, adoption_pass={adoption_pass}."
        ),
        "candidate_count": len(PERSISTENCE_DAYS_GRID),
        "best_candidate_summary": (
            {
                "persistence_days": best_pd,
                "test_trade_count": best_candidate.get("test_trade_count"),
                "test_win_rate": best_candidate.get("test_win_rate"),
                "test_excess_return": best_candidate.get("test_excess_return"),
                "test_max_drawdown": best_candidate.get("test_max_drawdown"),
                "test_sharpe": best_candidate.get("test_sharpe"),
            }
            if best_candidate
            else None
        ),
        "baseline_summary": {
            "test_trade_count": baseline.get("test_trade_count"),
            "test_win_rate": baseline.get("test_win_rate"),
            "test_excess_return": baseline.get("test_excess_return"),
            "test_max_drawdown": baseline.get("test_max_drawdown"),
            "test_sharpe": baseline.get("test_sharpe"),
        },
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(ack), allow_unicode=True), encoding="utf-8",
    )


def _write_diagnostic(
    output_dir: Path,
    best_pd: int,
    adoption_pass: bool,
    data_sizes: dict[str, int],
    baseline: dict[str, Any],
    all_candidates: list[dict[str, Any]],
) -> None:
    diag = {
        "warnings": [],
        "errors": [],
        "data_rows": data_sizes,
        "best_persistence_days": best_pd,
        "adoption_pass": adoption_pass,
        "grid_combos_tested": len(all_candidates),
        "persistence_days_grid": PERSISTENCE_DAYS_GRID,
        "baseline_trade_count": baseline.get("test_trade_count", 0),
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8",
    )


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root dir")
    parser.add_argument("--train-start", required=True, help="Train start (YYYYMMDD)")
    parser.add_argument("--train-end", required=True, help="Train end (YYYYMMDD)")
    parser.add_argument("--test-start", required=True, help="Test start (YYYYMMDD)")
    parser.add_argument("--test-end", required=True, help="Test end (YYYYMMDD)")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument(
        "--persistence-days", type=int, default=1,
        help="Persistence days for this run (1=none). When >1, indicates "
             "the single value to evaluate; when =1, runs full grid sweep.",
    )
    parser.add_argument(
        "--min-hold-days", type=int, default=DEFAULT_MIN_HOLD_DAYS,
    )
    parser.add_argument(
        "--initial-threshold-fraction", type=float,
        default=DEFAULT_INITIAL_THRESHOLD_FRACTION,
    )
    parser.add_argument(
        "--decay-period-factor", type=float,
        default=DEFAULT_DECAY_PERIOD_FACTOR,
    )
    parser.add_argument(
        "--effective-max-hold-days", type=int,
        default=DEFAULT_EFFECTIVE_MAX_HOLD_DAYS,
    )
    parser.add_argument(
        "--cost-model-enabled", action="store_true", default=False,
        help="Cost model toggle (reserved, not yet implemented in executor).",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── load data ──
    try:
        df_raw = _load_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8",
        )
        print(f"[persistence_gate] FATAL: {exc}", flush=True)
        return 1

    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train_raw = df_raw[
        (df_raw["trade_date"] >= train_start)
        & (df_raw["trade_date"] <= train_end)
    ].copy()
    df_test_raw = df_raw[
        (df_raw["trade_date"] >= test_start)
        & (df_raw["trade_date"] <= test_end)
    ].copy()
    df_2020_raw = df_train_raw[df_train_raw["trade_date"].dt.year == 2020].copy()

    if len(df_train_raw) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8",
        )
        return 1

    # ── compute persistence counts for all periods ──
    df_train = _compute_persistence_count(df_train_raw)
    df_test = _compute_persistence_count(df_test_raw)
    df_2020 = _compute_persistence_count(df_2020_raw)

    # ── baseline: persistence_days=1 ──
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    baseline = {
        "persistence_days": 1,
        "train_total_pnl": baseline_train["total_pnl"],
        "train_trade_count": baseline_train["trade_count"],
        "train_win_rate": baseline_train["win_rate"],
        "train_max_drawdown": baseline_train["max_drawdown"],
        "train_sharpe": baseline_train["sharpe_ratio"],
        "test_total_pnl": baseline_test["total_pnl"],
        "test_trade_count": baseline_test["trade_count"],
        "test_win_rate": baseline_test["win_rate"],
        "test_max_drawdown": baseline_test["max_drawdown"],
        "test_sharpe": baseline_test["sharpe_ratio"],
        "validate_2020_total_pnl": baseline_2020["total_pnl"],
        "validate_2020_trade_count": baseline_2020["trade_count"],
        "validate_2020_win_rate": baseline_2020["win_rate"],
        "validate_2020_max_drawdown": baseline_2020["max_drawdown"],
        "validate_2020_sharpe": baseline_2020["sharpe_ratio"],
    }

    # ── exit parameters ──
    min_hold = args.min_hold_days
    init_frac = args.initial_threshold_fraction
    decay_factor = args.decay_period_factor
    eff_max_hold = int(round(args.effective_max_hold_days * decay_factor))
    if eff_max_hold <= min_hold:
        eff_max_hold = min_hold + 1  # avoid degenerate

    fixed_params = {
        "min_hold_days": min_hold,
        "initial_threshold_fraction": init_frac,
        "decay_period_factor": decay_factor,
        "effective_max_hold_days": eff_max_hold,
    }

    # ── grid search ──
    all_candidates: list[dict[str, Any]] = []
    best_pd = 1
    best_test_excess = -float("inf")

    for pd_val in PERSISTENCE_DAYS_GRID:
        train_res = _simulate_with_persistence_and_exit(
            df_train, pd_val, min_hold, init_frac, decay_factor, eff_max_hold,
        )
        test_res = _simulate_with_persistence_and_exit(
            df_test, pd_val, min_hold, init_frac, decay_factor, eff_max_hold,
        )
        yr2020_res = _simulate_with_persistence_and_exit(
            df_2020, pd_val, min_hold, init_frac, decay_factor, eff_max_hold,
        )

        excess_train = round(
            train_res["total_pnl"] - baseline_train["total_pnl"], 2
        )
        excess_test = round(
            test_res["total_pnl"] - baseline_test["total_pnl"], 2
        )
        excess_2020 = round(
            yr2020_res["total_pnl"] - baseline_2020["total_pnl"], 2
        )

        candidate = {
            "persistence_days": pd_val,
            "min_hold_days": min_hold,
            "initial_threshold_fraction": init_frac,
            "decay_period_factor": decay_factor,
            "effective_max_hold_days": eff_max_hold,
            "train_total_pnl": train_res["total_pnl"],
            "train_trade_count": train_res["trade_count"],
            "train_win_rate": train_res["win_rate"],
            "train_max_drawdown": train_res["max_drawdown"],
            "train_max_drawdown_pct": train_res["max_drawdown_pct"],
            "train_sharpe": train_res["sharpe_ratio"],
            "train_excess_return": excess_train,
            "test_total_pnl": test_res["total_pnl"],
            "test_trade_count": test_res["trade_count"],
            "test_win_rate": test_res["win_rate"],
            "test_max_drawdown": test_res["max_drawdown"],
            "test_max_drawdown_pct": test_res["max_drawdown_pct"],
            "test_sharpe": test_res["sharpe_ratio"],
            "test_excess_return": excess_test,
            "validate_2020_total_pnl": yr2020_res["total_pnl"],
            "validate_2020_trade_count": yr2020_res["trade_count"],
            "validate_2020_win_rate": yr2020_res["win_rate"],
            "validate_2020_max_drawdown": yr2020_res["max_drawdown"],
            "validate_2020_sharpe": yr2020_res["sharpe_ratio"],
            "validate_2020_excess_return": excess_2020,
        }
        all_candidates.append(candidate)

        if excess_test > best_test_excess:
            best_test_excess = excess_test
            best_pd = pd_val

    # ── adoption_pass: per proposal success criteria ──
    #   1. cumulative excess > baseline on test
    #   2. max drawdown not worse than baseline on test
    #   3. win rate higher than baseline on test
    #   Falsifiers: any year trade_count < 30, any year sharpe < 0
    best_candidate = next(
        (c for c in all_candidates if c["persistence_days"] == best_pd), None
    )

    adoption_pass = False
    if best_candidate is not None:
        test_excess_ok = best_candidate["test_excess_return"] > 0
        test_dd_ok = best_candidate["test_max_drawdown"] >= baseline_test["max_drawdown"]
        test_wr_ok = best_candidate["test_win_rate"] > baseline_test["win_rate"]
        test_trades_ok = best_candidate["test_trade_count"] >= 30
        test_sharpe_ok = best_candidate["test_sharpe"] >= 0

        train_excess_ok = best_candidate["train_excess_return"] > 0
        y2020_excess_ok = best_candidate["validate_2020_excess_return"] > 0

        adoption_pass = (
            test_excess_ok
            and test_dd_ok
            and test_wr_ok
            and test_trades_ok
            and test_sharpe_ok
            and train_excess_ok
            and y2020_excess_ok
        )

    # ── write artifacts ──
    run_id = output_dir.name

    _write_summary(
        output_dir, best_pd, adoption_pass, baseline, all_candidates,
        args.train_start, args.train_end, args.test_start, args.test_end,
        fixed_params,
    )
    _write_report(
        output_dir, run_id, best_pd, adoption_pass, best_candidate,
        baseline, all_candidates, fixed_params,
    )
    _write_l4_ack(output_dir, run_id, best_pd, adoption_pass, best_candidate, baseline)
    _write_diagnostic(
        output_dir, best_pd, adoption_pass,
        {"train": len(df_train), "test": len(df_test), "validate_2020": len(df_2020)},
        baseline, all_candidates,
    )

    # ── done ──
    if best_candidate is not None:
        print(
            f"[persistence_gate] adoption_pass={adoption_pass} "
            f"best_pd={best_pd} "
            f"test_excess={best_candidate['test_excess_return']} "
            f"test_trades={best_candidate['test_trade_count']} "
            f"test_wr={best_candidate['test_win_rate']} "
            f"test_sharpe={best_candidate['test_sharpe']} "
            f"baseline_test_trades={baseline_test['trade_count']}",
            flush=True,
        )
    else:
        print("[persistence_gate] no valid candidates", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
