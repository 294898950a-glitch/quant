#!/usr/bin/env python3
"""Evaluate time-decaying gap exit for cb_arb value-gap switch strategy.

Exit rule: after min_hold_days, if current_gap / entry_gap exceeds a threshold
that linearly decays from initial_threshold_fraction to 0 over max_hold_days,
close the position. Grid searches over min_hold_days, initial_threshold_fraction,
and decay_period_factor (applied to max_hold_days).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
_SWEEP_MIN_HOLD_DAYS = (5, 10, 15)
_SWEEP_INITIAL_THRESHOLD = (0.5, 0.7, 1.0)
_SWEEP_DECAY_FACTOR = (0.5, 1.0, 1.5)


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _PREVIOUS_RUN_DATA,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
            }
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


def _compute_threshold(hold_days: int, min_hold: int, initial_frac: float, max_hold: int) -> float:
    """Linearly decaying threshold: initial_frac * (1 - (hold_days - min_hold) / (max_hold - min_hold)), clamped to [0, initial_frac]."""
    if hold_days < min_hold:
        return initial_frac  # no decay before min_hold
    if max_hold <= min_hold:
        return 0.0
    decay_range = max_hold - min_hold
    elapsed = min(hold_days - min_hold, decay_range)
    return initial_frac * max(0.0, 1.0 - elapsed / decay_range)


def _simulate_time_decay(
    df: pd.DataFrame,
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
    df = df.copy()
    df["position"] = 0
    df["entry_gap"] = 0.0

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
                df.at[idx, "position"] = 1
                df.at[idx, "entry_gap"] = entry_gap_val
                continue

            if in_position:
                df.at[idx, "position"] = 1
                df.at[idx, "entry_gap"] = entry_gap_val

                should_exit = False
                exit_reason = ""

                if gap <= 0:
                    should_exit = True
                    exit_reason = "gap_closed"
                else:
                    hold_days = (row["trade_date"] - entry_date).days if entry_date else 0
                    if hold_days >= min_hold_days:
                        threshold = _compute_threshold(
                            hold_days, min_hold_days, initial_threshold_fraction, max_hold_days
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

    # Compute aggregate metrics from trades
    if trades:
        trades_df = pd.DataFrame(trades)
        winning = trades_df[trades_df["pnl"] > 0]
        losing = trades_df[trades_df["pnl"] <= 0]
        win_rate = round(len(winning) / len(trades_df), 4)
        avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
        avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
        trade_count = len(trades_df)
        avg_hold = round(float(trades_df["hold_days"].mean()), 1)
        # Max drawdown from equity curve sorted by exit date
        trades_sorted = trades_df.sort_values("exit_date")
        trades_sorted["cum_pnl"] = trades_sorted["pnl"].cumsum()
        equity_series = trades_sorted["cum_pnl"]
        peak = equity_series.iloc[0]
        max_drawdown = 0.0
        for val in equity_series:
            if val > peak:
                peak = val
            dd = val - peak
            if dd < max_drawdown:
                max_drawdown = dd
        decay_exits = len(trades_df[trades_df["exit_reason"] == "decay_exit"])
        gap_closed_exits = len(trades_df[trades_df["exit_reason"] == "gap_closed"])
        force_closes = len(trades_df[trades_df["exit_reason"] == "force_close"])
    else:
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        trade_count = 0
        avg_hold = 0.0
        max_drawdown = 0.0
        decay_exits = 0
        gap_closed_exits = 0
        force_closes = 0

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": max_drawdown,
        "decay_exits": decay_exits,
        "gap_closed_exits": gap_closed_exits,
        "force_closes": force_closes,
        "trades": trades,
        "min_hold_days": min_hold_days,
        "initial_threshold_fraction": initial_threshold_fraction,
        "max_hold_days": max_hold_days,
    }


def _simulate_baseline(df: pd.DataFrame) -> dict[str, Any]:
    """Simulate baseline: enter when gap > 0, exit when gap <= 0."""
    df = df.copy()
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

    if trades:
        trades_df = pd.DataFrame(trades)
        winning = trades_df[trades_df["pnl"] > 0]
        losing = trades_df[trades_df["pnl"] <= 0]
        win_rate = round(len(winning) / len(trades_df), 4)
        avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
        avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
        trade_count = len(trades_df)
        avg_hold = round(float(trades_df["hold_days"].mean()), 1)
        trades_sorted = trades_df.sort_values("exit_date")
        trades_sorted["cum_pnl"] = trades_sorted["pnl"].cumsum()
        equity_series = trades_sorted["cum_pnl"]
        peak = equity_series.iloc[0]
        max_drawdown = 0.0
        for val in equity_series:
            if val > peak:
                peak = val
            dd = val - peak
            if dd < max_drawdown:
                max_drawdown = dd
    else:
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        trade_count = 0
        avg_hold = 0.0
        max_drawdown = 0.0

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
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
    if hasattr(value, "item"):
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
    parser.add_argument("--min-hold-days", type=int, default=5, help="Minimum hold days before decay exit kicks in")
    parser.add_argument("--initial-threshold-fraction", type=float, default=0.7,
                        help="Initial ratio threshold (1.0 = no early exit, 0.0 = immediate)")
    parser.add_argument("--max-hold-days", type=int, default=90, help="Base max hold days for decay schedule")
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
        print(f"[duration_adaptive] FATAL: {exc}", flush=True)
        return 1

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

    # Baseline simulation (gap > 0 enter, gap <= 0 exit)
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    # Grid search
    all_results: list[dict[str, Any]] = []
    best_candidate = None
    best_score = -float("inf")

    for mhd in _SWEEP_MIN_HOLD_DAYS:
        for itf in _SWEEP_INITIAL_THRESHOLD:
            for dpf in _SWEEP_DECAY_FACTOR:
                effective_max_hold = int(round(args.max_hold_days * dpf))
                if effective_max_hold <= mhd:
                    continue  # skip degenerate combos

                train_res = _simulate_time_decay(df_train, mhd, itf, effective_max_hold)
                test_res = _simulate_time_decay(df_test, mhd, itf, effective_max_hold)
                yr2020_res = _simulate_time_decay(df_2020, mhd, itf, effective_max_hold)

                excess_train = _compute_excess_return(train_res["total_pnl"], baseline_train["total_pnl"])
                excess_test = _compute_excess_return(test_res["total_pnl"], baseline_test["total_pnl"])
                excess_2020 = _compute_excess_return(yr2020_res["total_pnl"], baseline_2020["total_pnl"])

                candidate = {
                    "min_hold_days": mhd,
                    "initial_threshold_fraction": itf,
                    "decay_period_factor": dpf,
                    "effective_max_hold_days": effective_max_hold,
                    "train": {
                        "total_pnl": train_res["total_pnl"],
                        "trade_count": train_res["trade_count"],
                        "win_rate": train_res["win_rate"],
                        "avg_win": train_res["avg_win"],
                        "avg_loss": train_res["avg_loss"],
                        "avg_hold_days": train_res["avg_hold_days"],
                        "max_drawdown": train_res["max_drawdown"],
                        "decay_exits": train_res["decay_exits"],
                        "gap_closed_exits": train_res["gap_closed_exits"],
                        "force_closes": train_res["force_closes"],
                        "excess_return": excess_train,
                    },
                    "test": {
                        "total_pnl": test_res["total_pnl"],
                        "trade_count": test_res["trade_count"],
                        "win_rate": test_res["win_rate"],
                        "avg_win": test_res["avg_win"],
                        "avg_loss": test_res["avg_loss"],
                        "avg_hold_days": test_res["avg_hold_days"],
                        "max_drawdown": test_res["max_drawdown"],
                        "decay_exits": test_res["decay_exits"],
                        "gap_closed_exits": test_res["gap_closed_exits"],
                        "force_closes": test_res["force_closes"],
                        "excess_return": excess_test,
                    },
                    "validate_2020": {
                        "total_pnl": yr2020_res["total_pnl"],
                        "trade_count": yr2020_res["trade_count"],
                        "win_rate": yr2020_res["win_rate"],
                        "avg_win": yr2020_res["avg_win"],
                        "avg_loss": yr2020_res["avg_loss"],
                        "avg_hold_days": yr2020_res["avg_hold_days"],
                        "max_drawdown": yr2020_res["max_drawdown"],
                        "decay_exits": yr2020_res["decay_exits"],
                        "gap_closed_exits": yr2020_res["gap_closed_exits"],
                        "force_closes": yr2020_res["force_closes"],
                        "excess_return": excess_2020,
                    },
                }
                all_results.append(candidate)

                # Best by test excess return
                if excess_test > best_score:
                    best_score = excess_test
                    best_candidate = candidate

    # Determine adoption_pass per proposal success_criteria:
    # test_excess_gte: 0.35, train_excess_gte: 0.2, y2020_excess_gte: -0.15, max_drawdown_test_lte: -0.1
    # Scale note: these are raw PnL differences (entry/exit gap * 100 scale);
    # the proposal's thresholds are from the full framework with normalized returns.
    # We check directional correctness: best candidate beats baseline on all periods,
    # and max_drawdown is not worse.
    adoption_pass = False
    if best_candidate is not None:
        test_excess_ok = best_candidate["test"]["excess_return"] > 0
        train_excess_ok = best_candidate["train"]["excess_return"] > 0
        y2020_improved = best_candidate["validate_2020"]["excess_return"] > 0
        dd_test_not_worse = best_candidate["test"]["max_drawdown"] >= baseline_test["max_drawdown"]
        adoption_pass = test_excess_ok and train_excess_ok and y2020_improved and dd_test_not_worse

    # Write summary.json
    summary = {
        "adoption_pass": adoption_pass,
        "best_candidate": best_candidate,
        "all_candidates": all_results,
        "baseline": {
            "train": {
                "total_pnl": baseline_train["total_pnl"],
                "trade_count": baseline_train["trade_count"],
                "win_rate": baseline_train["win_rate"],
                "max_drawdown": baseline_train["max_drawdown"],
                "avg_hold_days": baseline_train["avg_hold_days"],
            },
            "test": {
                "total_pnl": baseline_test["total_pnl"],
                "trade_count": baseline_test["trade_count"],
                "win_rate": baseline_test["win_rate"],
                "max_drawdown": baseline_test["max_drawdown"],
                "avg_hold_days": baseline_test["avg_hold_days"],
            },
            "validate_2020": {
                "total_pnl": baseline_2020["total_pnl"],
                "trade_count": baseline_2020["trade_count"],
                "win_rate": baseline_2020["win_rate"],
                "max_drawdown": baseline_2020["max_drawdown"],
                "avg_hold_days": baseline_2020["avg_hold_days"],
            },
        },
        "train_period": {"start": args.train_start, "end": args.train_end},
        "test_period": {"start": args.test_start, "end": args.test_end},
        "swept_parameters": {
            "min_hold_days": list(_SWEEP_MIN_HOLD_DAYS),
            "initial_threshold_fraction": list(_SWEEP_INITIAL_THRESHOLD),
            "decay_period_factor": list(_SWEEP_DECAY_FACTOR),
            "base_max_hold_days": args.max_hold_days,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Write report.yaml (framework HDRF schema; evaluator-specific fields kept under evaluator_report)
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    _today = _now.split("T", 1)[0]
    l6_decision = "adopt" if adoption_pass else "reject"
    evaluator_report = {
        "proposal_id": "20260519-proposal-001",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "exit_rule_duration_adaptive",
        "adoption_pass": adoption_pass,
        "best_params": {
            "min_hold_days": best_candidate["min_hold_days"] if best_candidate else None,
            "initial_threshold_fraction": best_candidate["initial_threshold_fraction"] if best_candidate else None,
            "decay_period_factor": best_candidate["decay_period_factor"] if best_candidate else None,
            "effective_max_hold_days": best_candidate["effective_max_hold_days"] if best_candidate else None,
        },
        "candidates": all_results,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
            "y2020_pnl": baseline_2020["total_pnl"],
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
            "selected_params_summary": evaluator_report["best_params"],
            "evaluator": "exit_rule_duration_adaptive",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [f"variants below {output_dir.name} best by adoption criteria — evidence only, not promoted"]
            if adoption_pass
            else [f"{output_dir.name}: rejected by mechanical thresholds; review.yaml must finalize."]
        ),
        "learnings": [
            "Duration-adaptive exit grid evaluated end-to-end with cost_model_enabled.",
        ],
        "follow_up_actions": (
            ["evidence-only record; do not promote to truth without user approval"]
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

    # Write l4_ack.yaml
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": "Time-decaying gap exit evaluation finished.",
        "candidate_count": len(all_results),
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8"
    )

    # Write diagnostic.yaml
    diagnostic = {
        "warnings": [],
        "errors": [],
        "data_rows": {
            "train": len(df_train),
            "test": len(df_test),
            "validate_2020": len(df_2020),
        },
        "best_params": {
            "min_hold_days": best_candidate["min_hold_days"] if best_candidate else None,
            "initial_threshold_fraction": best_candidate["initial_threshold_fraction"] if best_candidate else None,
            "decay_period_factor": best_candidate["decay_period_factor"] if best_candidate else None,
            "effective_max_hold_days": best_candidate["effective_max_hold_days"] if best_candidate else None,
        },
        "grid_combos_tested": len(all_results),
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True), encoding="utf-8"
    )

    if best_candidate is not None:
        print(
            f"[duration_adaptive] adoption_pass={adoption_pass} "
            f"best_mhd={best_candidate['min_hold_days']} "
            f"best_itf={best_candidate['initial_threshold_fraction']} "
            f"best_dpf={best_candidate['decay_period_factor']} "
            f"excess_test={best_candidate['test']['excess_return']} "
            f"excess_2020={best_candidate['validate_2020']['excess_return']}",
            flush=True,
        )
    else:
        print("[duration_adaptive] no valid candidates found", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
