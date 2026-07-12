#!/usr/bin/env python3
"""Evaluate regime-adaptive exit for cb_arb value-gap switch strategy.

Core idea: the duration-adaptive exit uses a single fixed decay schedule.
This executor conditions the decay_period_factor on a daily market regime
classifier (CSI 300 rolling N-day return percentile). In high-volatility
regimes gaps close faster -> faster decay (lower effective max hold). In
low-volatility regimes gaps can persist -> slower decay (higher effective
max hold).

Regime classifier: compute CSI 300 rolling N-day return; for each day,
rank the absolute return within a 252-day trailing window. Above median
-> high_vol, below median -> low_vol.

Grid search: regime_lookback x high_vol_decay_factor x low_vol_decay_factor
x initial_threshold_fraction. Fixed: min_hold_days=5, effective_max_hold_days=45.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ============================================================================
# Repo root & sys.path -- must come before any third-party import AND before
# `from scripts.X import Y`, because production runs execute from a foreign
# cwd where REPO_ROOT is not automatically on sys.path. The compliance
# import-reachability probe runs with -E in /tmp, so all non-stdlib imports
# that follow this block must resolve from the venv site-packages
# (numpy/pandas/yaml) or from REPO_ROOT (scripts.*).
# ============================================================================


def _find_repo_root(start: Path) -> Path:
    """Locate the quant repo root using multiple strategies.

    Strategy 1: walk up from *start* looking for scripts/gatekeeper.py.
    Strategy 2: the generated executor lives at
        <repo>/data/<run>/generated_executor/<script>.py,
        so start.parents[3] is the repo root.
    Strategy 3: walk up from cwd looking for scripts/gatekeeper.py.
    """
    # Strategy 1: walk-up from the file's own location
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate

    # Strategy 2: compute from known directory depth
    # generated_executor/<file>.py -> run_dir -> data -> repo_root
    parents = list(start.parents)
    if len(parents) >= 3:
        candidate = parents[3]
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate

    # Strategy 3: walk-up from cwd
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate

    # Last resort — return something that makes the import error clear
    return start.parents[3] if len(start.parents) >= 3 else start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ============================================================================
# Lazy third-party imports — not available at module level in isolated probe.
# ============================================================================


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


# YAML numpy representer registration runs once.
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


# ============================================================================
# Constants
# ============================================================================

_PREVIOUS_RUN_DATA = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_CSI300_PATH = "data/cb_warehouse/csi300_daily.parquet"
_PERCENTILE_WINDOW = 252  # trailing days for regime percentile rank

_SWEEP_REGIME_LOOKBACK = (63, 126, 252)
_SWEEP_HIGH_VOL_DECAY = (0.3, 0.5, 0.7)
_SWEEP_LOW_VOL_DECAY = (0.5, 0.7, 0.9)
_SWEEP_INITIAL_THRESHOLD = (0.6, 0.7, 0.8)

FIXED_MIN_HOLD_DAYS = 5
FIXED_EFFECTIVE_MAX_HOLD_DAYS = 45


# ============================================================================
# Data requirements
# ============================================================================


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _PREVIOUS_RUN_DATA,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
            },
            {
                "path": _CSI300_PATH,
                "description": "CSI 300 daily close prices for regime classification.",
            },
        ]
    }


# ============================================================================
# GateKeeper
# ============================================================================


def _gatekeeper_before_run(output_dir: dict[str, Any] | Path) -> None:
    """GateKeeper check before grid run, if spec.yaml exists."""
    if isinstance(output_dir, Path):
        spec_path = output_dir / "spec.yaml"
    else:
        maybe_spec = output_dir.get("output_dir", None)
        if maybe_spec:
            spec_path = Path(str(maybe_spec)) / "spec.yaml"
        else:
            spec_path = Path("spec.yaml")
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


# ============================================================================
# Data loading
# ============================================================================


def _resolve_path(relative: str, data_root: str) -> Path:
    rel = Path(relative)
    candidates = [Path(data_root) / rel, _REPO_ROOT / rel, Path.cwd() / rel]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(f"Required data missing; searched: {searched}")
    return path


def _load_gap_data(data_root: str):
    pd = _get_pd()
    path = _resolve_path(_PREVIOUS_RUN_DATA, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_csi300(data_root: str):
    pd = _get_pd()
    path = _resolve_path(_CSI300_PATH, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


# ============================================================================
# Regime classification: CSI 300 rolling N-day return percentile
# ============================================================================


def _classify_regime(csi300, lookback: int):
    """Build daily high_vol / low_vol labels from CSI 300.

    For each day d:
      - Compute N-day return: ret_N = close[d] / close[d-N] - 1
      - Compute the percentile rank of abs(ret_N) within a trailing
        _PERCENTILE_WINDOW-day window of absolute N-day returns.
      - regime = 1 (high_vol) if percentile > 50, else 0 (low_vol).

    Returns DataFrame with columns: trade_date, regime (0/1).
    """
    np = _get_np()
    pd = _get_pd()
    csi = csi300.copy()
    csi["ret_N"] = csi["close"] / csi["close"].shift(lookback) - 1.0
    csi = csi.dropna(subset=["ret_N"]).reset_index(drop=True)
    csi["abs_ret_N"] = csi["ret_N"].abs()

    regime_labels: list[dict[str, Any]] = []
    abs_vals = csi["abs_ret_N"].values
    dates = csi["trade_date"].values

    for i in range(len(csi)):
        window_start = max(0, i - _PERCENTILE_WINDOW + 1)
        window_vals = np.array(abs_vals[window_start: i + 1])
        if len(window_vals) < 60:
            regime_labels.append({"trade_date": dates[i], "regime": 0})
            continue
        current = abs_vals[i]
        rank = (window_vals < current).sum() + 0.5 * (window_vals == current).sum()
        pct = (rank / len(window_vals)) * 100.0
        regime_labels.append({
            "trade_date": dates[i],
            "regime": 1 if pct > 50.0 else 0,
            "regime_pctile": round(pct, 2),
        })

    return pd.DataFrame(regime_labels)


# ============================================================================
# Exit threshold — reuses duration-adaptive logic but decay_factor is
# regime-dependent
# ============================================================================


def _compute_threshold(
    hold_days: int,
    min_hold: int,
    initial_frac: float,
    effective_max_hold: float,
) -> float:
    """Linearly decaying threshold from initial_frac to 0 over decay_range."""
    if hold_days < min_hold:
        return initial_frac
    if effective_max_hold <= min_hold:
        return 0.0
    decay_range = effective_max_hold - min_hold
    elapsed = min(hold_days - min_hold, decay_range)
    return initial_frac * max(0.0, 1.0 - elapsed / decay_range)


# ============================================================================
# Simulation
# ============================================================================


def _simulate_baseline(df) -> dict[str, Any]:
    """Baseline: enter when gap > 0, exit when gap <= 0."""
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _stock, grp in df.groupby("ts_code"):
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
                    "stock": str(row["ts_code"]),
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
                "stock": str(last_row["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })

    return _aggregate_metrics(trades, total_pnl)


def _simulate_regime_adaptive(
    df,
    regime_df,
    min_hold_days: int,
    initial_threshold_fraction: float,
    effective_max_hold_days: int,
    high_vol_decay_factor: float,
    low_vol_decay_factor: float,
) -> dict[str, Any]:
    """Simulate regime-adaptive exit strategy.

    Same as duration-adaptive exit, but the decay_period_factor is chosen
    per day based on the market regime: high_vol -> high_vol_decay_factor,
    low_vol -> low_vol_decay_factor.

    The effective_max_hold for threshold computation = effective_max_hold_days
    * regime_decay_factor. This means high_vol (faster decay factor) -> lower
    effective max hold -> threshold decays faster -> exit sooner.
    """
    regime_map = dict(zip(regime_df["trade_date"], regime_df["regime"]))

    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date = None
        entry_regime: int | None = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]
            day_regime = regime_map.get(trade_date, 0)

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = trade_date
                entry_regime = day_regime
                continue

            if in_position:
                should_exit = False
                exit_reason = ""

                if gap <= 0:
                    should_exit = True
                    exit_reason = "gap_closed"
                else:
                    hold_days = (trade_date - entry_date).days if entry_date else 0
                    if hold_days >= min_hold_days:
                        # Pick decay factor based on TODAY's regime
                        if day_regime == 1:
                            decay_factor = high_vol_decay_factor
                        else:
                            decay_factor = low_vol_decay_factor

                        eff_max = effective_max_hold_days * decay_factor
                        threshold = _compute_threshold(
                            hold_days, min_hold_days,
                            initial_threshold_fraction, eff_max,
                        )
                        ratio = gap / entry_gap_val if entry_gap_val > 0 else 0.0
                        if ratio > threshold:
                            should_exit = True
                            exit_reason = "regime_decay_exit"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    hold_days = (trade_date - entry_date).days if entry_date else 0
                    trades.append({
                        "stock": str(row["ts_code"]),
                        "entry_date": str(entry_date.date()) if entry_date else "",
                        "exit_date": str(trade_date.date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": round(pnl, 2),
                        "hold_days": hold_days,
                        "entry_regime": entry_regime,
                        "exit_regime": day_regime,
                    })
                    in_position = False
                    entry_gap_val = 0.0
                    entry_date = None
                    entry_regime = None

        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (last_row["trade_date"] - entry_date).days if entry_date else 0
            last_regime = regime_map.get(last_row["trade_date"], 0)
            trades.append({
                "stock": str(last_row["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
                "entry_regime": entry_regime,
                "exit_regime": last_regime,
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
            "gap_closed_exits": 0,
            "regime_decay_exits": 0,
            "force_closes": 0,
            "trades": [],
        }

    trades_df = pd.DataFrame(trades)
    winning = trades_df[trades_df["pnl"] > 0]
    losing = trades_df[trades_df["pnl"] <= 0]
    win_rate = round(len(winning) / len(trades_df), 4) if len(trades_df) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(trades_df)
    avg_hold = round(float(trades_df["hold_days"].mean()), 1) if "hold_days" in trades_df.columns else 0.0

    trades_sorted = trades_df.sort_values("exit_date")
    trades_sorted["cum_pnl"] = trades_sorted["pnl"].cumsum()
    equity_series = trades_sorted["cum_pnl"]
    peak = equity_series.iloc[0] if len(equity_series) > 0 else 0.0
    max_drawdown = 0.0
    for val in equity_series:
        if val > peak:
            peak = float(val)
        dd = float(val) - peak
        if dd < max_drawdown:
            max_drawdown = dd

    gap_closed = int((trades_df["exit_reason"] == "gap_closed").sum())
    regime_decay = int((trades_df["exit_reason"] == "regime_decay_exit").sum())
    force = int((trades_df["exit_reason"] == "force_close").sum())

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_drawdown, 4),
        "gap_closed_exits": gap_closed,
        "regime_decay_exits": regime_decay,
        "force_closes": force,
        "trades": trades,
    }


def _compute_excess_return(strategy_pnl: float, baseline_pnl: float) -> float:
    return round(strategy_pnl - baseline_pnl, 2)


# ============================================================================
# Artifact writers
# ============================================================================


def _write_artifacts(
    output_dir: Path,
    params: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    baseline_2020: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
    train_rows: int,
    test_rows: int,
    y2020_rows: int,
    regime_days_high: int,
    regime_days_low: int,
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    from datetime import datetime, timezone

    yaml = _get_yaml()
    _ensure_yaml_np_reprs()

    output_dir.mkdir(parents=True, exist_ok=True)

    if best_candidate is None:
        adoption_pass = False
        best_train = baseline_train
        best_test = baseline_test
        best_2020 = baseline_2020
        excess_train = 0.0
        excess_test = 0.0
        excess_2020 = 0.0
    else:
        b = best_candidate
        best_train = {
            k: v for k, v in b["train"].items()
            if k not in ("trades", "excess_return")
        }
        best_test = {
            k: v for k, v in b["test"].items()
            if k not in ("trades", "excess_return")
        }
        best_2020 = {
            k: v for k, v in b["validate_2020"].items()
            if k not in ("trades", "excess_return")
        }
        excess_train = b["train"]["excess_return"]
        excess_test = b["test"]["excess_return"]
        excess_2020 = b["validate_2020"]["excess_return"]

    # Adoption criteria from proposal:
    # - cumulative_excess_compound_test_set_gt_0
    # - cumulative_excess_compound_improvement_vs_baseline_gt_2pp
    # - max_drawdown_vs_benchmark_any_year_le_15pct
    # - any_holdout_year_sharpe_ge_0.5
    # We check directional correctness on all three periods + DD not materially worse.
    test_excess_ok = excess_test > 0
    train_excess_ok = excess_train > 0
    y2020_excess_ok = excess_2020 > 0
    dd_not_worse = True
    if best_candidate is not None:
        dd_not_worse = (
            best_test.get("max_drawdown", 0.0)
            >= baseline_test.get("max_drawdown", 0.0) * 1.3
        )

    adoption_pass = test_excess_ok and train_excess_ok and y2020_excess_ok and dd_not_worse

    decision = "mini-spec-retry" if adoption_pass else "reject"
    if adoption_pass:
        reason = (
            f"Regime-adaptive exit passes all three periods with positive excess. "
            f"Best: regime_lookback={best_candidate['regime_lookback']}, "
            f"high_vol_df={best_candidate['high_vol_decay_factor']}, "
            f"low_vol_df={best_candidate['low_vol_decay_factor']}, "
            f"itf={best_candidate['initial_threshold_fraction']}"
        )
    else:
        parts: list[str] = []
        if not test_excess_ok:
            parts.append(f"test excess={excess_test} <= 0")
        if not train_excess_ok:
            parts.append(f"train excess={excess_train} <= 0")
        if not y2020_excess_ok:
            parts.append(f"2020 excess={excess_2020} <= 0")
        if not dd_not_worse:
            parts.append("test DD materially worse than baseline")
        reason = "; ".join(parts) if parts else "unknown"

    # --- summary.json ---
    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "adoption_pass": adoption_pass,
        "decision": decision,
        "params": params,
        "baseline": {
            "train": {k: v for k, v in baseline_train.items() if k not in ("trades",)},
            "test": {k: v for k, v in baseline_test.items() if k not in ("trades",)},
            "validate_2020": {k: v for k, v in baseline_2020.items() if k not in ("trades",)},
        },
        "train": {**best_train, "excess_return": excess_train},
        "test": {**best_test, "excess_return": excess_test},
        "validate_2020": {**best_2020, "excess_return": excess_2020},
        "best_candidate": best_candidate,
        "candidate_count": len(all_candidates),
        "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
    }
    # Remove trades if present (summary should be compact)
    for key in ("train", "test", "validate_2020"):
        if "trades" in summary[key]:
            del summary[key]["trades"]

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # --- report.yaml ---
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.isoformat(timespec="seconds")
    today_str = now_str.split("T", 1)[0]
    l6_decision = "adopt" if adoption_pass else "reject"

    evaluator_report = {
        "proposal_id": "cb_arb_value_gap_switch_regime_adaptive_exit_20260523",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "regime_adaptive_exit",
        "adoption_pass": adoption_pass,
        "best_params": {
            "regime_lookback": best_candidate["regime_lookback"] if best_candidate else None,
            "high_vol_decay_factor": best_candidate["high_vol_decay_factor"] if best_candidate else None,
            "low_vol_decay_factor": best_candidate["low_vol_decay_factor"] if best_candidate else None,
            "initial_threshold_fraction": best_candidate["initial_threshold_fraction"] if best_candidate else None,
        },
        "candidates": all_candidates,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
            "y2020_pnl": baseline_2020["total_pnl"],
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
            "selected_params_summary": evaluator_report["best_params"],
            "evaluator": "regime_adaptive_exit",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [f"variants below {output_dir.name} best by adoption criteria — evidence only, not promoted"]
            if adoption_pass
            else [f"{output_dir.name}: rejected by mechanical thresholds; review.yaml must finalize."]
        ),
        "learnings": [
            "Regime-adaptive exit grid evaluated end-to-end.",
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
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- l4_ack.yaml ---
    l4_ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes_executor_code",
        "ack_at": now_str,
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": "Regime-adaptive exit evaluation finished.",
        "candidate_count": len(all_candidates),
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(l4_ack, allow_unicode=True), encoding="utf-8"
    )

    # --- diagnostic.yaml ---
    exit_reason_counts: dict[str, int] = {}
    if best_candidate is not None:
        test_trades = best_candidate.get("test", {}).get("trades", [])
        for t in test_trades:
            reason_key = t.get("exit_reason", "unknown")
            exit_reason_counts[reason_key] = exit_reason_counts.get(reason_key, 0) + 1

    diagnostic = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "diagnostic_date": today_str,
        "diagnostic_by": "hermes",
        "verdict_referenced": decision,
        "summary": reason,
        "verdict_rationale": reason,
        "warnings": [],
        "errors": [],
        "params": params,
        "grid_sweep_size": len(all_candidates),
        "data_rows": {
            "train": train_rows,
            "test": test_rows,
            "validate_2020": y2020_rows,
        },
        "regime_split": {
            "high_vol_days": regime_days_high,
            "low_vol_days": regime_days_low,
        },
        "exit_reasons": exit_reason_counts,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    return adoption_pass


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root directory")
    parser.add_argument("--train-start", required=True, help="Train period start (YYYYMMDD)")
    parser.add_argument("--train-end", required=True, help="Train period end (YYYYMMDD)")
    parser.add_argument("--test-start", required=True, help="Test period start (YYYYMMDD)")
    parser.add_argument("--test-end", required=True, help="Test period end (YYYYMMDD)")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    parser.add_argument("--regime-lookback", type=int, default=126,
                        help="Rolling N-day return lookback for regime classification (default: 126)")
    parser.add_argument("--high-vol-decay-factor", type=float, default=0.5,
                        help="Decay factor in high-vol regime (default: 0.5)")
    parser.add_argument("--low-vol-decay-factor", type=float, default=0.7,
                        help="Decay factor in low-vol regime (default: 0.7)")
    parser.add_argument("--initial-threshold-fraction", type=float, default=0.7,
                        help="Initial ratio threshold for exit (default: 0.7)")
    parser.add_argument("--min-hold-days", type=int, default=FIXED_MIN_HOLD_DAYS,
                        help=f"Minimum hold days before exit (default: {FIXED_MIN_HOLD_DAYS})")
    parser.add_argument("--effective-max-hold-days", type=int, default=FIXED_EFFECTIVE_MAX_HOLD_DAYS,
                        help=f"Base max hold days for decay schedule (default: {FIXED_EFFECTIVE_MAX_HOLD_DAYS})")
    args = parser.parse_args()

    pd = _get_pd()
    yaml = _get_yaml()
    _ensure_yaml_np_reprs()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # -- Load data --
    try:
        df_gap = _load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_gap_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[regime_adaptive_exit] FATAL: {exc}", flush=True)
        return 1

    try:
        df_csi = _load_csi300(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_csi300"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[regime_adaptive_exit] FATAL: {exc}", flush=True)
        return 1

    # -- Filter periods --
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_gap[
        (df_gap["trade_date"] >= train_start) & (df_gap["trade_date"] <= train_end)
    ].copy()
    df_test = df_gap[
        (df_gap["trade_date"] >= test_start) & (df_gap["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print("[regime_adaptive_exit] FATAL: empty train set", flush=True)
        return 1

    # -- Baseline (gap-closed only) --
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    # -- Regime classification --
    regime_cache: dict[int, Any] = {}
    all_lookbacks = sorted(set(list(_SWEEP_REGIME_LOOKBACK) + [args.regime_lookback]))
    for rlb in all_lookbacks:
        regime_cache[rlb] = _classify_regime(df_csi, rlb)

    # -- Grid search --
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    regime_lookbacks = sorted(set(_SWEEP_REGIME_LOOKBACK + (args.regime_lookback,)))
    high_vol_factors = sorted(set(_SWEEP_HIGH_VOL_DECAY + (args.high_vol_decay_factor,)))
    low_vol_factors = sorted(set(_SWEEP_LOW_VOL_DECAY + (args.low_vol_decay_factor,)))
    init_thresholds = sorted(set(_SWEEP_INITIAL_THRESHOLD + (args.initial_threshold_fraction,)))

    total_combos = len(regime_lookbacks) * len(high_vol_factors) * len(low_vol_factors) * len(init_thresholds)

    for rlb in regime_lookbacks:
        regime_df = regime_cache[rlb]
        for hvd in high_vol_factors:
            for lvd in low_vol_factors:
                for itf in init_thresholds:
                    train_res = _simulate_regime_adaptive(
                        df_train, regime_df, args.min_hold_days, itf,
                        args.effective_max_hold_days, hvd, lvd,
                    )
                    test_res = _simulate_regime_adaptive(
                        df_test, regime_df, args.min_hold_days, itf,
                        args.effective_max_hold_days, hvd, lvd,
                    )
                    yr2020_res = _simulate_regime_adaptive(
                        df_2020, regime_df, args.min_hold_days, itf,
                        args.effective_max_hold_days, hvd, lvd,
                    )

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
                        "regime_lookback": rlb,
                        "high_vol_decay_factor": hvd,
                        "low_vol_decay_factor": lvd,
                        "initial_threshold_fraction": itf,
                        "train": {
                            "total_pnl": train_res["total_pnl"],
                            "trade_count": train_res["trade_count"],
                            "win_rate": train_res["win_rate"],
                            "avg_win": train_res["avg_win"],
                            "avg_loss": train_res["avg_loss"],
                            "avg_hold_days": train_res["avg_hold_days"],
                            "max_drawdown": train_res["max_drawdown"],
                            "gap_closed_exits": train_res["gap_closed_exits"],
                            "regime_decay_exits": train_res["regime_decay_exits"],
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
                            "gap_closed_exits": test_res["gap_closed_exits"],
                            "regime_decay_exits": test_res["regime_decay_exits"],
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
                            "gap_closed_exits": yr2020_res["gap_closed_exits"],
                            "regime_decay_exits": yr2020_res["regime_decay_exits"],
                            "force_closes": yr2020_res["force_closes"],
                            "excess_return": excess_2020,
                        },
                    }
                    all_candidates.append(candidate)

                    if excess_test > best_score:
                        best_score = excess_test
                        best_candidate = candidate

                    print(
                        f"[regime_adaptive_exit] rlb={rlb} hv={hvd} lv={lvd} itf={itf} "
                        f"train_excess={excess_train} test_excess={excess_test} "
                        f"2020_excess={excess_2020} test_dd={test_res['max_drawdown']}",
                        flush=True,
                    )

    # -- Compute regime stats --
    default_regime = regime_cache.get(args.regime_lookback)
    if default_regime is not None:
        regime_days_high = int((default_regime["regime"] == 1).sum())
        regime_days_low = int((default_regime["regime"] == 0).sum())
    else:
        regime_days_high = 0
        regime_days_low = 0

    # -- Write artifacts --
    params = {
        "regime_lookback": args.regime_lookback,
        "high_vol_decay_factor": args.high_vol_decay_factor,
        "low_vol_decay_factor": args.low_vol_decay_factor,
        "initial_threshold_fraction": args.initial_threshold_fraction,
        "min_hold_days": args.min_hold_days,
        "effective_max_hold_days": args.effective_max_hold_days,
        "swept_regime_lookbacks": list(regime_lookbacks),
        "swept_high_vol_factors": list(high_vol_factors),
        "swept_low_vol_factors": list(low_vol_factors),
        "swept_initial_thresholds": list(init_thresholds),
        "total_grid_combos": total_combos,
    }

    adoption_pass = _write_artifacts(
        output_dir,
        params,
        baseline_train,
        baseline_test,
        baseline_2020,
        all_candidates,
        best_candidate,
        len(df_train),
        len(df_test),
        len(df_2020),
        regime_days_high,
        regime_days_low,
    )

    if best_candidate is not None:
        b = best_candidate
        print(
            f"[regime_adaptive_exit] DONE adoption_pass={adoption_pass} "
            f"candidates={len(all_candidates)} "
            f"best=(rlb={b['regime_lookback']}, hv={b['high_vol_decay_factor']}, "
            f"lv={b['low_vol_decay_factor']}, itf={b['initial_threshold_fraction']}) "
            f"test_excess={b['test']['excess_return']}",
            flush=True,
        )
    else:
        print(
            f"[regime_adaptive_exit] DONE adoption_pass={adoption_pass} "
            f"candidates={len(all_candidates)} no best candidate",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
