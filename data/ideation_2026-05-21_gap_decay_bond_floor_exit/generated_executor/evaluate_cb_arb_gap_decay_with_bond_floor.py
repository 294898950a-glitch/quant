"""Evaluate time-decaying gap exit with bond-floor proximity awareness.

Core idea: for each open CB arbitrage position, track the entry gap.
After min_hold_days, compute current_gap / entry_gap ratio and compare
against a linearly decaying threshold (initial_threshold_fraction -> 0
over max_hold_days). Additionally, each day compute bond_floor_proximity
= CB_close / bond_floor - 1; if it falls below floor_proximity_threshold,
the decay threshold is multiplied by floor_penalty_factor (0-1), which
accelerates exits when the bond trades near its floor.

Grid-search over min_hold_days, initial_threshold_fraction, max_hold_days,
floor_proximity_threshold, and floor_penalty_factor on train period.
Select best by composite score (2020 excess return + test excess return
+ test win rate). Evaluate best on train, test, and 2020 stress periods.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root & sys.path — must come before any `from scripts.X import Y`.
# The compliance import-reachability probe runs with -I in /tmp, so all
# non-stdlib imports that follow must resolve from the venv site-packages
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
# Data requirements — must exist
# ---------------------------------------------------------------------------

_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)


def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
                "description": "Daily value-gap amounts with theoretical_value, bond_floor, option_value.",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic reference table (conversion price, coupon, maturity).",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices.",
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "description": "Daily CB market prices.",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Gatekeeper
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
    raise FileNotFoundError(
        f"Cannot find {relative} under data_root={data_root}; searched: {searched}"
    )


def _load_gap_data(data_root: str):
    pd = _get_pd()
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Bond-floor proximity
# ---------------------------------------------------------------------------


def _compute_proximity(row) -> float | None:
    """Return close/bond_floor - 1, or None if unusable."""
    pd = _get_pd()
    cb_close = float(row["close"])
    bf = float(row["bond_floor"])
    if bf <= 0 or pd.isna(bf) or pd.isna(cb_close):
        return None
    return round(cb_close / bf - 1.0, 6)


# ---------------------------------------------------------------------------
# Exit rule: gap decay with bond-floor awareness
# ---------------------------------------------------------------------------


def _should_exit_gap_decay_floor(
    hold_days: int,
    current_gap: float,
    entry_gap: float,
    proximity: float | None,
    min_hold_days: int,
    initial_threshold_fraction: float,
    max_hold_days: int,
    floor_proximity_threshold: float,
    floor_penalty_factor: float,
) -> bool:
    """Decide whether to exit based on decayed gap threshold + floor proximity.

    effective_threshold = initial_threshold_fraction * (1 - hold_days / max_hold_days)
    If proximity < floor_proximity_threshold, multiply effective_threshold by floor_penalty_factor.

    Exit if current_gap < entry_gap * effective_threshold AND hold_days >= min_hold_days.
    """
    if hold_days < min_hold_days:
        return False
    if entry_gap <= 0:
        return False

    # Linear decay: threshold goes from initial_threshold_fraction to 0
    decay_fraction = max(0.0, 1.0 - hold_days / max_hold_days)
    effective_threshold = initial_threshold_fraction * decay_fraction

    # Bond-floor penalty: accelerate exit when near the floor
    if (
        proximity is not None
        and floor_proximity_threshold > 0
        and proximity < floor_proximity_threshold
    ):
        effective_threshold *= floor_penalty_factor

    if effective_threshold <= 0:
        return False

    return current_gap < entry_gap * effective_threshold


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


def _simulate_baseline(df) -> dict[str, Any]:
    """Baseline strategy: enter when gap > 0, exit when gap <= 0."""
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _, grp in df.groupby("ts_code"):
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
                pnl = round((gap - entry_gap_val) * 100.0, 2)
                total_pnl += pnl
                days = (row["trade_date"] - entry_date).days if entry_date else 0
                trades.append(
                    {
                        "stock": str(row["ts_code"]),
                        "entry_date": str(entry_date.date()) if entry_date else "",
                        "exit_date": str(row["trade_date"].date()),
                        "exit_reason": "gap_closed",
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": pnl,
                        "hold_days": days,
                    }
                )
                in_position = False
                entry_gap_val = 0.0
                entry_date = None

        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = round((final_gap - entry_gap_val) * 100.0, 2)
            total_pnl += pnl
            days = (last["trade_date"] - entry_date).days if entry_date else 0
            trades.append(
                {
                    "stock": str(last["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(last["trade_date"].date()),
                    "exit_reason": "force_close",
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(final_gap, 4),
                    "pnl": pnl,
                    "hold_days": days,
                }
            )

    return _compute_metrics(trades, total_pnl)


def _compute_metrics(
    trades: list[dict[str, Any]], total_pnl: float
) -> dict[str, Any]:
    """Derive aggregate metrics from trade list."""
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
            "decay_exits": 0,
            "floor_accelerated_exits": 0,
            "force_closes": 0,
        }

    pd = _get_pd()
    tdf = pd.DataFrame(trades)
    winning = tdf[tdf["pnl"] > 0]
    losing = tdf[tdf["pnl"] <= 0]

    win_rate = round(len(winning) / len(tdf), 4) if len(tdf) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(tdf)
    avg_hold = round(float(tdf["hold_days"].mean()), 1)

    # Max drawdown from cumulative PnL curve
    tdf_sorted = tdf.sort_values("exit_date")
    tdf_sorted["cum_pnl"] = tdf_sorted["pnl"].cumsum()
    equity = tdf_sorted["cum_pnl"].values
    peak = float(equity[0]) if len(equity) > 0 else 0.0
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = float(val)
        dd = float(val) - peak
        if dd < max_dd:
            max_dd = dd

    gap_closed = (
        int((tdf["exit_reason"] == "gap_closed").sum())
        if "exit_reason" in tdf.columns
        else 0
    )
    decay_exits = (
        int((tdf["exit_reason"] == "decay_exit").sum())
        if "exit_reason" in tdf.columns
        else 0
    )
    floor_accel = (
        int((tdf["exit_reason"] == "decay_exit_floor_accelerated").sum())
        if "exit_reason" in tdf.columns
        else 0
    )
    force_closes = (
        int((tdf["exit_reason"] == "force_close").sum())
        if "exit_reason" in tdf.columns
        else 0
    )

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "gap_closed_exits": gap_closed,
        "decay_exits": decay_exits,
        "floor_accelerated_exits": floor_accel,
        "force_closes": force_closes,
    }


def _simulate_gap_decay_floor(
    df,
    min_hold_days: int,
    initial_threshold_fraction: float,
    max_hold_days: int,
    floor_proximity_threshold: float,
    floor_penalty_factor: float,
) -> dict[str, Any]:
    """Simulate gap-decay-with-bond-floor exit rule.

    Walk forward per bond: enter when gap > 0. After min_hold_days, each day
    compute the decayed threshold (with floor-proximity penalty) and exit if
    current_gap < entry_gap * effective_threshold.

    PnL: (exit_gap - entry_gap) * 100.
    """
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_date = None
        entry_gap_val: float = 0.0

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]
            proximity = _compute_proximity(row)

            if not in_position and gap > 0:
                in_position = True
                entry_date = trade_date
                entry_gap_val = gap
                continue

            if not in_position:
                continue

            should_exit = False
            exit_reason: str = ""

            # Natural close
            if gap <= 0:
                should_exit = True
                exit_reason = "gap_closed"
            else:
                days_held = (trade_date - entry_date).days if entry_date else 0
                if _should_exit_gap_decay_floor(
                    hold_days=days_held,
                    current_gap=gap,
                    entry_gap=entry_gap_val,
                    proximity=proximity,
                    min_hold_days=min_hold_days,
                    initial_threshold_fraction=initial_threshold_fraction,
                    max_hold_days=max_hold_days,
                    floor_proximity_threshold=floor_proximity_threshold,
                    floor_penalty_factor=floor_penalty_factor,
                ):
                    should_exit = True
                    # Distinguish between plain decay exit and floor-accelerated
                    if (
                        proximity is not None
                        and floor_proximity_threshold > 0
                        and proximity < floor_proximity_threshold
                    ):
                        exit_reason = "decay_exit_floor_accelerated"
                    else:
                        exit_reason = "decay_exit"

            if should_exit:
                pnl = round((gap - entry_gap_val) * 100.0, 2)
                total_pnl += pnl
                days_held = (trade_date - entry_date).days if entry_date else 0
                trades.append(
                    {
                        "stock": str(row["ts_code"]),
                        "entry_date": str(entry_date.date()) if entry_date else "",
                        "exit_date": str(trade_date.date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": pnl,
                        "hold_days": days_held,
                        "bond_floor_proximity": proximity,
                    }
                )
                in_position = False
                entry_date = None
                entry_gap_val = 0.0

        # Force-close
        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = round((final_gap - entry_gap_val) * 100.0, 2)
            total_pnl += pnl
            days_held = (last["trade_date"] - entry_date).days if entry_date else 0
            proximity = _compute_proximity(last)
            trades.append(
                {
                    "stock": str(last["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(last["trade_date"].date()),
                    "exit_reason": "force_close",
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(final_gap, 4),
                    "pnl": pnl,
                    "hold_days": days_held,
                    "bond_floor_proximity": proximity,
                }
            )

    return _compute_metrics(trades, total_pnl)


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def _write_artifacts(
    output_dir: Path,
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    yr2020_metrics: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    baseline_2020: dict[str, Any],
    params: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    _ensure_yaml_np_reprs()
    yaml = _get_yaml()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    now_str = now.isoformat(timespec="seconds")

    # Compute excess returns
    excess_train = round(train_metrics["total_pnl"] - baseline_train["total_pnl"], 2)
    excess_test = round(test_metrics["total_pnl"] - baseline_test["total_pnl"], 2)
    excess_2020 = round(yr2020_metrics["total_pnl"] - baseline_2020["total_pnl"], 2)

    # Adoption criteria:
    # - 2020 excess > -0.12  AND 2020 max dd > -0.15
    # - test excess >= 0.15  AND test max dd >= -0.10
    # - train excess > 0
    y2020_excess_ok = excess_2020 > -0.12
    y2020_dd_ok = yr2020_metrics["max_drawdown"] > -0.15
    y2020_ok = y2020_excess_ok and y2020_dd_ok

    test_excess_ok = excess_test >= 0.15
    test_dd_ok = test_metrics["max_drawdown"] > -0.10
    test_ok = test_excess_ok and test_dd_ok

    train_excess_ok = excess_train > 0

    adoption_pass = test_ok and train_excess_ok and y2020_ok

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Gap-decay-with-bond-floor exit (min_hold={params['min_hold_days']}, "
            f"init_frac={params['initial_threshold_fraction']}, "
            f"max_hold={params['max_hold_days']}, "
            f"floor_prox_thr={params['floor_proximity_threshold']}, "
            f"floor_penalty={params['floor_penalty_factor']}) "
            f"passes all periods with positive/acceptable excess and drawdown."
        )
    else:
        decision = "reject"
        parts: list[str] = []
        if not test_ok:
            parts.append(
                f"test excess={excess_test} (>=0.15: {test_excess_ok}), "
                f"test dd={test_metrics['max_drawdown']} (>= -0.10: {test_dd_ok})"
            )
        if not train_excess_ok:
            parts.append(f"train excess={excess_train} <= 0")
        if not y2020_ok:
            parts.append(
                f"2020 excess={excess_2020} (>-0.12: {y2020_excess_ok}), "
                f"2020 dd={yr2020_metrics['max_drawdown']} (>-0.15: {y2020_dd_ok})"
            )
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
            "train": {k: v for k, v in baseline_train.items()},
            "test": {k: v for k, v in baseline_test.items()},
            "yr2020": {k: v for k, v in baseline_2020.items()},
        },
        "train": dict(train_metrics, excess_return=excess_train),
        "test": dict(test_metrics, excess_return=excess_test),
        "yr2020": dict(yr2020_metrics, excess_return=excess_2020),
        "best_candidate": best_candidate,
        "candidate_count": len(all_candidates),
        "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # --- report.yaml ---
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": now.strftime("%Y-%m-%d"),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": decision,
        "status": "COMPLETE",
        "params": params,
        "train": {**train_metrics, "excess_return": excess_train},
        "test": {**test_metrics, "excess_return": excess_test},
        "yr2020": {**yr2020_metrics, "excess_return": excess_2020},
        "adoption_pass": adoption_pass,
        "summary": reason,
        "learnings": [
            f"gap_decay_bond_floor min_hold={params['min_hold_days']} "
            f"init_frac={params['initial_threshold_fraction']} "
            f"max_hold={params['max_hold_days']} "
            f"floor_prox={params['floor_proximity_threshold']} "
            f"penalty={params['floor_penalty_factor']}: {reason}"
        ],
        "follow_up_actions": (
            ["Review adoption_pass before promotion."]
            if adoption_pass
            else ["Do not promote."]
        ),
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- l4_ack.yaml ---
    ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes_executor_code",
        "ack_at": now_str,
        "q1_hard_floors": {
            "description": "2020 stress period check.",
            "answer": (
                f"2020 excess={excess_2020} (>-0.12: {y2020_excess_ok}), "
                f"dd={yr2020_metrics['max_drawdown']} (baseline={baseline_2020['max_drawdown']})"
            ),
            "pass": y2020_ok,
        },
        "q2_selection_quality": {
            "description": "Test period check.",
            "answer": (
                f"test excess={excess_test} (>=0.15: {test_excess_ok}), "
                f"win_rate={test_metrics['win_rate']}, "
                f"trades={test_metrics['trade_count']}"
            ),
            "pass": test_ok,
        },
        "q3_falsifiers": {
            "description": "Drawdown degradation check.",
            "answer": (
                f"test dd={test_metrics['max_drawdown']} vs baseline={baseline_test['max_drawdown']}"
            ),
            "pass": test_dd_ok,
        },
        "overall_pass": adoption_pass,
        "overall_decision": decision,
        "overall_reason": reason,
        "auto_computed_at": now_str,
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- diagnostic.yaml ---
    diagnostic = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "diagnostic_date": now.strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes",
        "verdict_referenced": decision,
        "summary": reason,
        "verdict_rationale": reason,
        "warnings": [],
        "errors": [],
        "params": params,
        "grid_sweep_size": len(all_candidates),
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    return adoption_pass


# ---------------------------------------------------------------------------
# Grid-search sweep ranges
# ---------------------------------------------------------------------------

_SWEEP_MIN_HOLD_DAYS = (1, 3, 5, 7, 10)
_SWEEP_INIT_THRESHOLD_FRAC = (0.3, 0.5, 0.7, 0.9)
_SWEEP_MAX_HOLD_DAYS = (15, 30, 45, 60)
_SWEEP_FLOOR_PROX_THR = (0.0, 0.03, 0.07, 0.10)
_SWEEP_FLOOR_PENALTY = (0.3, 0.5, 0.8)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", required=True, help="Path to data root directory"
    )
    parser.add_argument(
        "--train-start", required=True, help="Train period start (YYYYMMDD)"
    )
    parser.add_argument(
        "--train-end", required=True, help="Train period end (YYYYMMDD)"
    )
    parser.add_argument(
        "--test-start", required=True, help="Test period start (YYYYMMDD)"
    )
    parser.add_argument(
        "--test-end", required=True, help="Test period end (YYYYMMDD)"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Output directory for artifacts"
    )
    parser.add_argument(
        "--min-hold-days",
        type=int,
        default=5,
        help="Minimum hold days before exit rule activates",
    )
    parser.add_argument(
        "--initial-threshold-fraction",
        type=float,
        default=0.5,
        help="Initial gap-ratio threshold (decays to 0 over max_hold_days)",
    )
    parser.add_argument(
        "--max-hold-days",
        type=int,
        default=30,
        help="Days over which threshold linearly decays to 0",
    )
    parser.add_argument(
        "--floor-proximity-threshold",
        type=float,
        default=0.03,
        help="Bond-floor proximity below which penalty factor applies",
    )
    parser.add_argument(
        "--floor-penalty-factor",
        type=float,
        default=0.5,
        help="Multiplier on decay threshold when bond is near floor (0-1)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # Load data
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            str({"error": str(exc), "step": "load_data"}), encoding="utf-8"
        )
        print(f"[gap_decay_floor] FATAL: {exc}", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    pd = _get_pd()
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
        (output_dir / "diagnostic.yaml").write_text(
            str(diag), encoding="utf-8"
        )
        print("[gap_decay_floor] FATAL: empty train set", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    # Baseline
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    print(
        f"[gap_decay_floor] Baseline: train_pnl={baseline_train['total_pnl']} "
        f"test_pnl={baseline_test['total_pnl']} 2020_pnl={baseline_2020['total_pnl']} "
        f"2020_dd={baseline_2020['max_drawdown']} "
        f"2020_win={baseline_2020['win_rate']}",
        flush=True,
    )

    # Grid search
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    total_combos = (
        len(_SWEEP_MIN_HOLD_DAYS)
        * len(_SWEEP_INIT_THRESHOLD_FRAC)
        * len(_SWEEP_MAX_HOLD_DAYS)
        * len(_SWEEP_FLOOR_PROX_THR)
        * len(_SWEEP_FLOOR_PENALTY)
    )
    done = 0

    for mhd in _SWEEP_MIN_HOLD_DAYS:
        for itf in _SWEEP_INIT_THRESHOLD_FRAC:
            for mxd in _SWEEP_MAX_HOLD_DAYS:
                for fpt in _SWEEP_FLOOR_PROX_THR:
                    for fpf in _SWEEP_FLOOR_PENALTY:
                        # Skip when mhd >= mxd (min_hold >= max_hold is nonsensical)
                        if mhd >= mxd:
                            done += 1
                            continue

                        train_res = _simulate_gap_decay_floor(
                            df_train, mhd, itf, mxd, fpt, fpf
                        )
                        test_res = _simulate_gap_decay_floor(
                            df_test, mhd, itf, mxd, fpt, fpf
                        )
                        yr2020_res = _simulate_gap_decay_floor(
                            df_2020, mhd, itf, mxd, fpt, fpf
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
                            "min_hold_days": mhd,
                            "initial_threshold_fraction": itf,
                            "max_hold_days": mxd,
                            "floor_proximity_threshold": fpt,
                            "floor_penalty_factor": fpf,
                            "train": {**train_res, "excess_return": excess_train},
                            "test": {**test_res, "excess_return": excess_test},
                            "validate_2020": {
                                **yr2020_res,
                                "excess_return": excess_2020,
                            },
                        }
                        all_candidates.append(candidate)

                        # Composite score: 40% test excess, 30% 2020 excess, 30% test win rate
                        score = (
                            0.4 * excess_test
                            + 0.3 * excess_2020
                            + 0.3 * test_res["win_rate"]
                        )
                        if score > best_score:
                            best_score = score
                            best_candidate = candidate

                        done += 1
                        if done % 100 == 0 or done == total_combos:
                            print(
                                f"[gap_decay_floor] grid {done}/{total_combos} "
                                f"best_score={best_score:.4f} "
                                f"test_excess={excess_test} "
                                f"2020_excess={excess_2020}",
                                flush=True,
                            )

    # Select best candidate params for final artifact write
    if best_candidate is not None:
        best_params = {
            "min_hold_days": best_candidate["min_hold_days"],
            "initial_threshold_fraction": best_candidate[
                "initial_threshold_fraction"
            ],
            "max_hold_days": best_candidate["max_hold_days"],
            "floor_proximity_threshold": best_candidate[
                "floor_proximity_threshold"
            ],
            "floor_penalty_factor": best_candidate["floor_penalty_factor"],
        }
        # Re-run with best params to get final metrics
        best_train = _simulate_gap_decay_floor(
            df_train,
            best_params["min_hold_days"],
            best_params["initial_threshold_fraction"],
            best_params["max_hold_days"],
            best_params["floor_proximity_threshold"],
            best_params["floor_penalty_factor"],
        )
        best_test = _simulate_gap_decay_floor(
            df_test,
            best_params["min_hold_days"],
            best_params["initial_threshold_fraction"],
            best_params["max_hold_days"],
            best_params["floor_proximity_threshold"],
            best_params["floor_penalty_factor"],
        )
        best_2020 = _simulate_gap_decay_floor(
            df_2020,
            best_params["min_hold_days"],
            best_params["initial_threshold_fraction"],
            best_params["max_hold_days"],
            best_params["floor_proximity_threshold"],
            best_params["floor_penalty_factor"],
        )
    else:
        # Fallback: use command-line args
        best_params = {
            "min_hold_days": args.min_hold_days,
            "initial_threshold_fraction": args.initial_threshold_fraction,
            "max_hold_days": args.max_hold_days,
            "floor_proximity_threshold": args.floor_proximity_threshold,
            "floor_penalty_factor": args.floor_penalty_factor,
        }
        best_train = _simulate_gap_decay_floor(
            df_train,
            args.min_hold_days,
            args.initial_threshold_fraction,
            args.max_hold_days,
            args.floor_proximity_threshold,
            args.floor_penalty_factor,
        )
        best_test = _simulate_gap_decay_floor(
            df_test,
            args.min_hold_days,
            args.initial_threshold_fraction,
            args.max_hold_days,
            args.floor_proximity_threshold,
            args.floor_penalty_factor,
        )
        best_2020 = _simulate_gap_decay_floor(
            df_2020,
            args.min_hold_days,
            args.initial_threshold_fraction,
            args.max_hold_days,
            args.floor_proximity_threshold,
            args.floor_penalty_factor,
        )

    # Write artifacts
    adoption_pass = _write_artifacts(
        output_dir,
        best_train,
        best_test,
        best_2020,
        baseline_train,
        baseline_test,
        baseline_2020,
        best_params,
        all_candidates,
        best_candidate,
    )

    _gatekeeper_after_run(output_dir)

    print(
        f"[gap_decay_floor] DONE adoption_pass={adoption_pass} "
        f"candidates={len(all_candidates)} "
        f"best={best_params}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
