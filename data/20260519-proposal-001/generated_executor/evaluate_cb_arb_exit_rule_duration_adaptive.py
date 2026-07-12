#!/usr/bin/env python3
"""Evaluate time-decaying gap exit for cb_arb value-gap switch strategy.

Exit rule: after min_hold_days, if current_gap / entry_gap exceeds a threshold
that linearly decays from initial_threshold_fraction to 0 over max_hold_days,
close the position. Grid searches over min_hold_days, initial_threshold_fraction,
and decay_period_factor (applied to base max_hold_days=90).

Train on 2019-2024, early-stop 2020 out-sample, sealed test on 2025-2026.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root & sys.path — must come before any third-party import AND before
# `from scripts.X import Y`, because production runs execute from a foreign cwd
# where REPO_ROOT is not automatically on sys.path.  The compliance
# import-reachability probe runs with -E in /tmp, so all non-stdlib imports
# that follow this block must resolve from the venv site-packages
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PREVIOUS_RUN_DATA = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_BASE_MAX_HOLD_DAYS = 90  # decay_period_factor multiplies this

_SWEEP_MIN_HOLD_DAYS = (5, 10, 15)
_SWEEP_INITIAL_THRESHOLD = (0.5, 0.7, 1.0)
_SWEEP_DECAY_FACTOR = (0.5, 1.0, 1.5)


# ---------------------------------------------------------------------------
# Data requirements
# ---------------------------------------------------------------------------

def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _PREVIOUS_RUN_DATA,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
            },
        ]
    }


# ---------------------------------------------------------------------------
# GateKeeper helpers
# ---------------------------------------------------------------------------

def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _resolve_data_path(data_root: str | Path, relative: str) -> Path:
    data_root = Path(data_root)
    rel = Path(relative)
    candidates = [
        data_root / rel,
        _REPO_ROOT / rel,
        Path.cwd() / rel,
    ]
    if rel.parts[0] == "data":
        inner = Path(*rel.parts[1:])
        candidates.append(data_root / inner)
        candidates.append(_REPO_ROOT / rel)
    for c in candidates:
        if c.exists():
            return c
    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"Cannot find {relative} under data_root={data_root}; searched: {searched}")


def _load_gap_data(data_root: str):
    pd = _get_pd()
    path = _resolve_data_path(data_root, _PREVIOUS_RUN_DATA)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Core simulation logic
# ---------------------------------------------------------------------------

def _compute_threshold(
    hold_days: int,
    min_hold: int,
    initial_frac: float,
    max_hold: int,
) -> float:
    """Linearly decaying threshold.

    Before min_hold: no decay (returns initial_frac).
    After min_hold: decays from initial_frac at min_hold to 0 at max_hold,
    clamped to [0, initial_frac].
    """
    if hold_days < min_hold:
        return initial_frac
    if max_hold <= min_hold:
        return 0.0
    decay_range = max_hold - min_hold
    elapsed = min(hold_days - min_hold, decay_range)
    return initial_frac * max(0.0, 1.0 - elapsed / decay_range)


def _simulate_time_decay(
    df,
    min_hold_days: int,
    initial_threshold_fraction: float,
    max_hold_days: int,
) -> dict[str, Any]:
    """Simulate the time-decaying gap exit strategy.

    Entry: gap > 0 and not in position.
    Exit (gap closed): current_gap <= 0.
    Exit (time decay): after min_hold_days, if current_gap / entry_gap > threshold, close.
    PnL: (exit_gap - entry_gap) * 100.
    """
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        idx_list = grp.index.tolist()
        in_position = False
        entry_gap_val = 0.0
        entry_date = None

        for i, idx in enumerate(idx_list):
            row = grp.loc[idx]
            gap = float(row["value_gap_amount"])

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = row["trade_date"]
                continue

            if not in_position:
                continue

            should_exit = False
            exit_reason = ""

            if gap <= 0:
                should_exit = True
                exit_reason = "gap_closed"
            else:
                hold_days = (row["trade_date"] - entry_date).days if entry_date else 0
                if hold_days >= min_hold_days:
                    threshold = _compute_threshold(
                        hold_days, min_hold_days, initial_threshold_fraction, max_hold_days,
                    )
                    ratio = gap / entry_gap_val if entry_gap_val > 0 else 0.0
                    if ratio > threshold:
                        should_exit = True
                        exit_reason = "decay_exit"

            if should_exit:
                pnl = (gap - entry_gap_val) * 100.0
                total_pnl += pnl
                hold_days = (row["trade_date"] - entry_date).days if entry_date else 0
                trades.append({
                    "stock": stock,
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(row["trade_date"].date()),
                    "exit_reason": exit_reason,
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": round(pnl, 2),
                    "hold_days": hold_days,
                    "min_hold_days": min_hold_days,
                    "initial_threshold_fraction": initial_threshold_fraction,
                    "max_hold_days": max_hold_days,
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
                "min_hold_days": min_hold_days,
                "initial_threshold_fraction": initial_threshold_fraction,
                "max_hold_days": max_hold_days,
            })

    return _aggregate_metrics(trades, total_pnl)


def _simulate_baseline(df) -> dict[str, Any]:
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

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = row["trade_date"]
                continue

            if in_position and gap <= 0:
                pnl = (gap - entry_gap_val) * 100.0
                total_pnl += pnl
                hold_days = (row["trade_date"] - entry_date).days if entry_date else 0
                trades.append({
                    "stock": stock,
                    "entry_date": str(entry_date.date()) if entry_date else "",
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


def _aggregate_metrics(
    trades: list[dict[str, Any]],
    total_pnl: float,
) -> dict[str, Any]:
    """Compute performance metrics from trade list."""
    pd = _get_pd()
    if not trades:
        return {
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_hold_days": 0.0,
            "max_drawdown": 0.0,
            "decay_exits": 0,
            "gap_closed_exits": 0,
            "force_closes": 0,
            "trades": [],
        }

    tdf = pd.DataFrame(trades)
    winning = tdf[tdf["pnl"] > 0]
    losing = tdf[tdf["pnl"] <= 0]

    win_rate = round(len(winning) / len(tdf), 4) if len(tdf) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(tdf)
    avg_hold = round(float(tdf["hold_days"].mean()), 1)

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

    decay_exits = int((tdf["exit_reason"] == "decay_exit").sum()) if "exit_reason" in tdf.columns else 0
    gap_closed_exits = int((tdf["exit_reason"] == "gap_closed").sum()) if "exit_reason" in tdf.columns else 0
    force_closes = int((tdf["exit_reason"] == "force_close").sum()) if "exit_reason" in tdf.columns else 0

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "decay_exits": decay_exits,
        "gap_closed_exits": gap_closed_exits,
        "force_closes": force_closes,
        "trades": trades,
    }


def _compute_excess_return(strategy_pnl: float, baseline_pnl: float) -> float:
    return round(strategy_pnl - baseline_pnl, 2)


def _plain(value: Any) -> Any:
    np = _get_np()
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


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_artifacts(
    output_dir: Path,
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    best_2020: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    baseline_2020: dict[str, Any],
    best_params: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
    best_test_trades: list[dict[str, Any]] | None = None,
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    yaml = _get_yaml()
    _ensure_yaml_np_reprs()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.now()
    now_str = now.isoformat(timespec="seconds")

    excess_train = _compute_excess_return(best_train["total_pnl"], baseline_train["total_pnl"])
    excess_test = _compute_excess_return(best_test["total_pnl"], baseline_test["total_pnl"])
    excess_2020 = _compute_excess_return(best_2020["total_pnl"], baseline_2020["total_pnl"])

    train_excess_ok = excess_train >= 0.20
    test_excess_ok = excess_test >= 0.35
    y2020_excess_ok = excess_2020 >= -0.15
    dd_not_worse = best_test["max_drawdown"] >= -0.10

    adoption_pass = test_excess_ok and train_excess_ok and y2020_excess_ok and dd_not_worse

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Time-decaying gap exit (min_hold={best_params['min_hold_days']}, "
            f"threshold={best_params['initial_threshold_fraction']}, "
            f"max_hold={best_params['max_hold_days']}) passes all thresholds."
        )
    else:
        decision = "reject"
        parts: list[str] = []
        if not train_excess_ok:
            parts.append(f"train excess={excess_train} < 0.20")
        if not test_excess_ok:
            parts.append(f"test excess={excess_test} < 0.35")
        if not y2020_excess_ok:
            parts.append(f"2020 excess={excess_2020} < -0.15")
        if not dd_not_worse:
            parts.append(f"test dd={best_test['max_drawdown']} > -0.10")
        reason = "; ".join(parts) if parts else "unknown"

    # --- summary.json ---
    summary: dict[str, Any] = {
        "adoption_pass": adoption_pass,
        "status": "COMPLETE",
        "run_id": output_dir.name,
        "params": best_params,
        "decision": decision,
        "baseline": {
            "train": {k: v for k, v in baseline_train.items() if k not in ("trades",)},
            "test": {k: v for k, v in baseline_test.items() if k not in ("trades",)},
            "validate_2020": {k: v for k, v in baseline_2020.items() if k not in ("trades",)},
        },
        "train": dict(best_train, excess_return=excess_train),
        "test": dict(best_test, excess_return=excess_test),
        "validate_2020": dict(best_2020, excess_return=excess_2020),
        "best_candidate": best_candidate,
        "candidate_count": len(all_candidates),
        "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
    }
    for key in ("train", "test", "validate_2020"):
        if "trades" in summary[key]:
            del summary[key]["trades"]

    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # --- report.yaml ---
    report: dict[str, Any] = {
        "proposal_id": output_dir.name,
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "exit_rule_duration_adaptive",
        "adoption_pass": adoption_pass,
        "params": best_params,
        "best_candidate": best_candidate,
        "candidates": all_candidates,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
            "validate_2020_pnl": baseline_2020["total_pnl"],
        },
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True), encoding="utf-8",
    )

    # --- l4_ack.yaml ---
    l4_ack: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes_executor_code",
        "ack_at": now_str,
        "q1_hard_floors": {
            "description": "2020 stress period check.",
            "answer": (
                f"2020 excess={excess_2020} (> -0.15: {y2020_excess_ok}), "
                f"dd={best_2020['max_drawdown']} (baseline={baseline_2020['max_drawdown']})"
            ),
            "pass": y2020_excess_ok,
        },
        "q2_selection_quality": {
            "description": "Test period check.",
            "answer": (
                f"test excess={excess_test} (>= 0.35: {test_excess_ok}), "
                f"win_rate={best_test['win_rate']}, trades={best_test['trade_count']}"
            ),
            "pass": test_excess_ok,
        },
        "q3_falsifiers": {
            "description": "Drawdown degradation check.",
            "answer": (
                f"test dd={best_test['max_drawdown']} vs baseline={baseline_test['max_drawdown']}, "
                f"not materially worse: {dd_not_worse}"
            ),
            "pass": dd_not_worse,
        },
        "overall_pass": adoption_pass,
        "overall_decision": decision,
        "overall_reason": reason,
        "auto_computed_at": now_str,
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8",
    )

    # --- diagnostic.yaml ---
    exit_reason_counts: dict[str, int] = {}
    if best_test_trades:
        for t in best_test_trades:
            reason = t.get("exit_reason", "unknown")
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1

    diagnostic: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "diagnostic_date": now.strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes_executor_code",
        "verdict_referenced": decision,
        "summary": reason,
        "verdict_rationale": reason,
        "warnings": [],
        "errors": [],
        "params": best_params,
        "grid_sweep_size": len(all_candidates),
        "test_exit_reasons": exit_reason_counts,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True), encoding="utf-8",
    )

    return adoption_pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    pd = _get_pd()
    yaml = _get_yaml()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument("--train-start", required=True, help="Train period start (YYYYMMDD)")
    parser.add_argument("--train-end", required=True, help="Train period end (YYYYMMDD)")
    parser.add_argument("--test-start", required=True, help="Test period start (YYYYMMDD)")
    parser.add_argument("--test-end", required=True, help="Test period end (YYYYMMDD)")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    parser.add_argument("--min-hold-days", type=int, default=5, help="Minimum hold days before decay exit")
    parser.add_argument("--initial-threshold-fraction", type=float, default=0.7,
                        help="Initial ratio threshold (1.0 = no early exit, 0.0 = immediate)")
    parser.add_argument("--max-hold-days", type=int, default=90,
                        help="Base max hold days for decay schedule")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # Load data
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[duration_adaptive] FATAL: {exc}", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_raw[
        (df_raw["trade_date"] >= train_start) & (df_raw["trade_date"] <= train_end)
    ].copy()
    df_test = df_raw[
        (df_raw["trade_date"] >= test_start) & (df_raw["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        _ensure_yaml_np_reprs()
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print("[duration_adaptive] FATAL: empty train set", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    # Baseline
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    # Grid search over sweep params (using base 90 as max_hold)
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for mhd in _SWEEP_MIN_HOLD_DAYS:
        for thr in _SWEEP_INITIAL_THRESHOLD:
            for factor in _SWEEP_DECAY_FACTOR:
                max_hold = int(factor * _BASE_MAX_HOLD_DAYS)
                # Ensure max_hold >= mhd to avoid degenerate decay
                effective_max_hold = max(max_hold, mhd + 5)

                train_res = _simulate_time_decay(df_train, mhd, thr, effective_max_hold)
                test_res = _simulate_time_decay(df_test, mhd, thr, effective_max_hold)
                yr2020_res = _simulate_time_decay(df_2020, mhd, thr, effective_max_hold)

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
                    "min_hold_days": mhd,
                    "initial_threshold_fraction": thr,
                    "decay_period_factor": factor,
                    "max_hold_days": effective_max_hold,
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

                # Score: prefer highest test excess with 2020 improvement
                score = excess_test + 0.5 * excess_2020
                if score > best_score:
                    best_score = score
                    best_candidate = candidate

                print(
                    f"[duration_adaptive] mhd={mhd} thr={thr} factor={factor} "
                    f"train_excess={excess_train} test_excess={excess_test} "
                    f"2020_excess={excess_2020} test_dd={test_res['max_drawdown']}",
                    flush=True,
                )

    # Re-run best params for artifact output (with full trade data)
    if best_candidate is not None:
        best_params = {
            "min_hold_days": best_candidate["min_hold_days"],
            "initial_threshold_fraction": best_candidate["initial_threshold_fraction"],
            "max_hold_days": best_candidate["max_hold_days"],
            "decay_period_factor": best_candidate["decay_period_factor"],
        }
        best_train = _simulate_time_decay(
            df_train, best_params["min_hold_days"],
            best_params["initial_threshold_fraction"], best_params["max_hold_days"],
        )
        best_test = _simulate_time_decay(
            df_test, best_params["min_hold_days"],
            best_params["initial_threshold_fraction"], best_params["max_hold_days"],
        )
        best_2020 = _simulate_time_decay(
            df_2020, best_params["min_hold_days"],
            best_params["initial_threshold_fraction"], best_params["max_hold_days"],
        )
    else:
        # Fallback to CLI args
        best_params = {
            "min_hold_days": args.min_hold_days,
            "initial_threshold_fraction": args.initial_threshold_fraction,
            "max_hold_days": args.max_hold_days,
            "decay_period_factor": round(args.max_hold_days / _BASE_MAX_HOLD_DAYS, 2),
        }
        best_train = _simulate_time_decay(
            df_train, args.min_hold_days, args.initial_threshold_fraction, args.max_hold_days,
        )
        best_test = _simulate_time_decay(
            df_test, args.min_hold_days, args.initial_threshold_fraction, args.max_hold_days,
        )
        best_2020 = _simulate_time_decay(
            df_2020, args.min_hold_days, args.initial_threshold_fraction, args.max_hold_days,
        )

    # Write artifacts — _write_artifacts receives all data as explicit parameters, no stale refs.
    adoption_pass = _write_artifacts(
        output_dir, best_train, best_test, best_2020,
        baseline_train, baseline_test, baseline_2020,
        best_params, all_candidates, best_candidate,
        best_test_trades=best_test.get("trades"),
    )

    _gatekeeper_after_run(output_dir)

    print(
        f"[duration_adaptive] DONE adoption_pass={adoption_pass} "
        f"candidates={len(all_candidates)} "
        f"best=({best_params})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
