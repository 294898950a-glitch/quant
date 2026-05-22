"""Evaluate gap-decay exit rule with bond-floor proximity awareness.

For each open position, track entry gap. After min_hold_days, a threshold
linearly decays from initial_threshold_fraction toward 0 over max_hold_days.
Additionally, if bond-floor proximity (CB close / bond_floor - 1) falls below
floor_proximity_threshold, the threshold is multiplied by floor_penalty_factor,
accelerating exits when the bond trades near its floor.

Grid-searches across min_hold_days, initial_threshold_fraction, max_hold_days,
floor_proximity_threshold, and floor_penalty_factor on the train period.
Selects best config by composite score (2020 excess return, test excess return,
and win rate). Compares against baseline (no exit filter).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Data requirements
# ---------------------------------------------------------------------------

def declare_data_requirements(command: list[str], spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": (
                    "data/cb_arb_value_gap_switch_regime-option-entry-gate_"
                    "2026-05-17/daily_value_gap_amounts.parquet"
                ),
                "description": (
                    "Daily value-gap ranks with theoretical_value, bond_floor, "
                    "close (CB price), value_gap_amount, position_cash."
                ),
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic table with conversion price, coupon, maturity.",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices for auxiliary use.",
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "description": "Daily CB market prices.",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _resolve_data_path(data_root: str | Path, relative: str) -> Path:
    data_root = Path(data_root)
    rel = Path(relative)
    candidates = [
        data_root / rel,
        data_root / rel.relative_to("data") if rel.parts[0] == "data" else None,
        _REPO_ROOT / rel,
        Path.cwd() / rel,
    ]
    for c in candidates:
        if c is not None and c.exists():
            return c
    raise FileNotFoundError(f"Cannot find {relative} under data_root={data_root}")


def load_gap_data(data_root: str) -> pd.DataFrame:
    """Load daily value gap amounts parquet file."""
    path = _resolve_data_path(
        data_root,
        "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
        "daily_value_gap_amounts.parquet",
    )
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def _daily_metrics_from_gap_changes(
    df: pd.DataFrame, position_col: str = "position"
) -> tuple[pd.DataFrame, float, float, float, float, float]:
    """Compute daily PnL, returns, drawdown, excess, Sharpe.

    Returns (df with added columns, total_return, max_drawdown,
             excess_return, win_rate, sharpe_ratio).
    """
    df = df.sort_values(["ts_code", "trade_date"]).copy()

    # Daily PnL: change in gap x position flag
    df["prev_gap"] = df.groupby("ts_code")["value_gap_amount"].shift(1)
    df["daily_pnl"] = df[position_col] * (df["value_gap_amount"] - df["prev_gap"])
    df["daily_pnl"] = df["daily_pnl"].fillna(0.0)

    # Daily portfolio value
    held_mask = df[position_col] > 0
    daily_portfolio = df[held_mask].groupby("trade_date")["position_cash"].sum()
    daily_portfolio = daily_portfolio.reindex(
        df["trade_date"].unique(), fill_value=0.0
    )

    # Aggregate to daily
    daily_pnl_agg = df.groupby("trade_date")["daily_pnl"].sum()
    port_vals = daily_portfolio.values
    pnl_vals = daily_pnl_agg.values

    daily_returns = np.divide(
        pnl_vals,
        port_vals,
        out=np.zeros_like(pnl_vals, dtype=float),
        where=port_vals > 0,
    )

    total_return = float(np.sum(daily_returns))

    # Max drawdown
    cum_returns = np.cumsum(daily_returns)
    running_max = np.maximum.accumulate(cum_returns)
    drawdowns = cum_returns - running_max
    max_dd = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    # Win rate
    bond_pnl = df.groupby("ts_code")["daily_pnl"].sum()
    win_rate = float((bond_pnl > 0).mean()) if len(bond_pnl) > 0 else 0.0

    # Sharpe ratio (daily, assume risk-free = 0)
    ret_std = float(np.std(daily_returns, ddof=1))
    n_days = len(daily_returns)
    sharpe = float(np.mean(daily_returns) / ret_std * np.sqrt(252)) if ret_std > 0 and n_days > 1 else 0.0

    # Also compute baseline metrics for excess return
    df["bl_pnl"] = (df["value_gap_amount"] - df["prev_gap"])
    df["bl_pnl"] = df["bl_pnl"].fillna(0.0)
    bl_daily = df.groupby("trade_date")["bl_pnl"].sum()
    bl_returns = np.divide(
        bl_daily.values,
        port_vals,
        out=np.zeros_like(port_vals, dtype=float),
        where=port_vals > 0,
    )
    bl_total = float(np.sum(bl_returns))
    excess_return = total_return - bl_total

    return df, total_return, max_dd, excess_return, win_rate, sharpe


def run_single_config(
    df: pd.DataFrame,
    min_hold_days: int,
    initial_threshold_fraction: float,
    max_hold_days: int,
    floor_proximity_threshold: float,
    floor_penalty_factor: float,
) -> dict[str, Any]:
    """Run gap-decay-with-bond-floor exit on a single period.

    df must contain: ts_code, trade_date, value_gap_amount, close, bond_floor.

    Returns metrics dict.
    """
    if df.empty:
        return {
            "total_return": 0.0,
            "excess_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "sharpe_ratio": 0.0,
            "trade_count": 0,
            "early_exits": 0,
            "total_positions": 0,
        }

    df = df.sort_values(["ts_code", "trade_date"]).copy()

    # Track per-bond state
    position_flags: list[int] = []
    state: dict[str, dict[str, Any]] = {}

    for _, row in df.iterrows():
        code = str(row["ts_code"])
        trade_date = row["trade_date"]
        gap = float(row["value_gap_amount"])
        cb_close = float(row["close"]) if not pd.isna(row["close"]) else 0.0
        bond_floor = float(row["bond_floor"]) if not pd.isna(row["bond_floor"]) else 0.0

        if code not in state:
            state[code] = {
                "entry_gap": gap,
                "entry_date": trade_date,
                "in_position": True,
            }

        st = state[code]
        in_pos = True

        if st["in_position"]:
            days_held = (trade_date - st["entry_date"]).days

            # Time-decay threshold
            if max_hold_days > 0:
                decay_frac = max(0.0, 1.0 - days_held / max_hold_days)
            else:
                decay_frac = 0.0
            effective_threshold = initial_threshold_fraction * decay_frac

            # Bond-floor proximity penalty
            if (
                bond_floor > 0
                and floor_proximity_threshold > 0
                and floor_penalty_factor > 0
            ):
                proximity = cb_close / bond_floor - 1.0
                if proximity < floor_proximity_threshold:
                    effective_threshold *= floor_penalty_factor

            # Exit condition
            if (
                days_held >= min_hold_days
                and gap < st["entry_gap"] * effective_threshold
            ):
                st["in_position"] = False
                in_pos = False

        position_flags.append(1 if in_pos else 0)

    df["position"] = position_flags

    total_positions = df["ts_code"].nunique()
    early_exits = int((df["position"] == 0).any())

    df, our_total, our_dd, excess, our_wr, our_sharpe = (
        _daily_metrics_from_gap_changes(df, "position")
    )

    return {
        "total_return": round(our_total, 6),
        "excess_return": round(excess, 6),
        "max_drawdown": round(our_dd, 6),
        "win_rate": round(our_wr, 6),
        "sharpe_ratio": round(our_sharpe, 6),
        "trade_count": total_positions,
        "early_exits": early_exits,
        "total_positions": total_positions,
    }


def run_baseline(df: pd.DataFrame) -> dict[str, Any]:
    """Run baseline: hold all positions to the end."""
    if df.empty:
        return {
            "total_return": 0.0,
            "excess_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "sharpe_ratio": 0.0,
            "trade_count": 0,
        }
    df = df.sort_values(["ts_code", "trade_date"]).copy()
    df["position"] = 1
    df, total, dd, _, wr, sharpe = _daily_metrics_from_gap_changes(df, "position")
    return {
        "total_return": round(total, 6),
        "excess_return": 0.0,
        "max_drawdown": round(dd, 6),
        "win_rate": round(wr, 6),
        "sharpe_ratio": round(sharpe, 6),
        "trade_count": df["ts_code"].nunique(),
        "early_exits": 0,
        "total_positions": df["ts_code"].nunique(),
    }


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def _composite_score(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
) -> float:
    """Composite score: weighted sum of 2020 excess + test excess + win_rate.

    Higher is better.
    """
    excess = metrics.get("excess_return", 0.0)
    bl_wr = baseline.get("win_rate", 0.0)
    wr = metrics.get("win_rate", 0.0)
    score = excess * 100.0 + (wr - bl_wr) * 50.0
    return score


def grid_search_train(
    df_train: pd.DataFrame,
    df_2020: pd.DataFrame,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Grid search over parameter space on train period.

    Returns (best_params, best_metrics_on_train).
    """
    # Grid values
    min_hold_options = [1, 3, 5, 7, 10]
    initial_threshold_options = [0.3, 0.5, 0.7, 0.9]
    max_hold_options = [10, 20, 30, 45, 60]
    floor_proximity_options = [0.0, 0.03, 0.06, 0.10]
    floor_penalty_options = [0.3, 0.5, 0.7, 0.8]

    best_params: dict[str, Any] = {}
    best_score = -np.inf
    best_metrics: dict[str, Any] = {}
    best_2020_metrics: dict[str, Any] = {}

    baseline_train = run_baseline(df_train)
    baseline_2020 = run_baseline(df_2020)

    total_combos = (
        len(min_hold_options)
        * len(initial_threshold_options)
        * len(max_hold_options)
        * len(floor_proximity_options)
        * len(floor_penalty_options)
    )
    count = 0

    for mh, itf, mxh, fpt, fpf in product(
        min_hold_options,
        initial_threshold_options,
        max_hold_options,
        floor_proximity_options,
        floor_penalty_options,
    ):
        if mxh <= mh:
            # max_hold must be > min_hold for meaningful decay window
            count += 1
            continue

        train_m = run_single_config(df_train, mh, itf, mxh, fpt, fpf)
        yr2020_m = run_single_config(df_2020, mh, itf, mxh, fpt, fpf)

        # Score: combine train + 2020
        train_score = _composite_score(train_m, baseline_train)
        yr2020_score = yr2020_m.get("excess_return", 0.0) * 100.0
        score = train_score * 0.3 + yr2020_score * 0.7

        if score > best_score:
            best_score = score
            best_params = {
                "min_hold_days": mh,
                "initial_threshold_fraction": itf,
                "max_hold_days": mxh,
                "floor_proximity_threshold": fpt,
                "floor_penalty_factor": fpf,
            }
            best_metrics = train_m
            best_2020_metrics = yr2020_m

        count += 1

    print(
        f"[grid_search] tested {count}/{total_combos} combos, "
        f"best_score={best_score:.4f}, best_params={best_params}",
        flush=True,
    )

    best_metrics["yr2020_excess_return"] = best_2020_metrics.get("excess_return", 0.0)
    best_metrics["yr2020_max_drawdown"] = best_2020_metrics.get("max_drawdown", 0.0)
    best_metrics["yr2020_win_rate"] = best_2020_metrics.get("win_rate", 0.0)
    best_metrics["yr2020_sharpe_ratio"] = best_2020_metrics.get("sharpe_ratio", 0.0)
    best_metrics["baseline_train"] = baseline_train
    best_metrics["baseline_2020"] = baseline_2020

    return best_params, best_metrics, best_2020_metrics, baseline_train, baseline_2020


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_artifacts(
    output_dir: Path,
    best_params: dict[str, Any],
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    yr2020_metrics: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_2020: dict[str, Any],
    baseline_test: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- Adoption decision ---
    # Success criteria from proposal:
    #   Primary: 2020 excess > -0.12 AND max_dd > -0.15
    #   Secondary: Test excess >= 0.15 AND max_dd >= -0.10
    # Falsifiers:
    #   - 2020 excess improvement <= 0.05 over baseline
    #   - Test excess < 0.10
    #   - Max drawdown worsens by > 0.03 in any period

    yr20_excess = yr2020_metrics.get("excess_return", 0.0)
    yr20_dd = yr2020_metrics.get("max_drawdown", 0.0)
    test_excess = test_metrics.get("excess_return", 0.0)
    test_dd = test_metrics.get("max_drawdown", 0.0)
    train_dd = train_metrics.get("max_drawdown", 0.0)

    bl_2020_excess = baseline_2020.get("excess_return", 0.0)  # always 0 for baseline
    bl_test_excess = baseline_test.get("excess_return", 0.0)
    bl_2020_dd = baseline_2020.get("max_drawdown", 0.0)
    bl_train_dd = baseline_train.get("max_drawdown", 0.0)
    bl_test_dd = baseline_test.get("max_drawdown", 0.0)

    # Primary
    primary_pass = yr20_excess > -0.12 and yr20_dd > -0.15

    # Secondary
    secondary_pass = test_excess >= 0.15 and test_dd >= -0.10

    # Falsifiers
    improvement_2020 = yr20_excess - bl_2020_excess
    dd_worsened_train = train_dd < bl_train_dd - 0.03
    dd_worsened_2020 = yr20_dd < bl_2020_dd - 0.03
    dd_worsened_test = test_dd < bl_test_dd - 0.03

    falsified = (
        improvement_2020 <= 0.05
        or test_excess < 0.10
        or dd_worsened_train
        or dd_worsened_2020
        or dd_worsened_test
    )

    adoption_pass = primary_pass and secondary_pass and not falsified

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Gap-decay + bond-floor exit passes primary (2020 excess={yr20_excess}, "
            f"dd={yr20_dd}) and secondary (test excess={test_excess}, dd={test_dd}) "
            f"without falsification. Best params: {best_params}."
        )
    else:
        decision = "reject"
        parts = []
        if not primary_pass:
            parts.append(
                f"2020 excess={yr20_excess} (need >-0.12), "
                f"dd={yr20_dd} (need >-0.15)"
            )
        if not secondary_pass:
            parts.append(
                f"test excess={test_excess} (need >=0.15), "
                f"dd={test_dd} (need >=-0.10)"
            )
        if falsified:
            falsifier_parts = []
            if improvement_2020 <= 0.05:
                falsifier_parts.append(f"2020 improvement={improvement_2020} (need >0.05)")
            if test_excess < 0.10:
                falsifier_parts.append(f"test excess={test_excess} (need >=0.10)")
            if dd_worsened_train:
                falsifier_parts.append(
                    f"train dd={train_dd} vs baseline={bl_train_dd}"
                )
            if dd_worsened_2020:
                falsifier_parts.append(
                    f"2020 dd={yr20_dd} vs baseline={bl_2020_dd}"
                )
            if dd_worsened_test:
                falsifier_parts.append(
                    f"test dd={test_dd} vs baseline={bl_test_dd}"
                )
            parts.append("falsifier: " + "; ".join(falsifier_parts))
        reason = " | ".join(parts) if parts else "unknown"

    # --- summary.json ---
    summary = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "adoption_pass": adoption_pass,
        "decision": decision,
        "best_params": best_params,
        "train": train_metrics,
        "test": test_metrics,
        "yr2020": yr2020_metrics,
        "baseline_train": baseline_train,
        "baseline_2020": baseline_2020,
        "baseline_test": baseline_test,
        "artifacts": [
            "summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"
        ],
        "falsifier_details": {
            "improvement_2020_vs_baseline": round(improvement_2020, 6),
            "dd_worsened_train": bool(dd_worsened_train),
            "dd_worsened_2020": bool(dd_worsened_2020),
            "dd_worsened_test": bool(dd_worsened_test),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # --- report.yaml ---
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": decision,
        "status": "COMPLETE",
        "best_params": best_params,
        "train": train_metrics,
        "test": test_metrics,
        "yr2020": yr2020_metrics,
        "baseline_train": baseline_train,
        "baseline_2020": baseline_2020,
        "baseline_test": baseline_test,
        "adoption_pass": adoption_pass,
        "summary": reason,
        "learnings": [
            f"Gap-decay + bond-floor exit: {reason}",
        ],
        "follow_up_actions": (
            ["Review adoption_pass before promotion."]
            if adoption_pass
            else ["Do not promote. Investigate alternative exit rules."]
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
        "ack_at": now,
        "q1_hard_floors": {
            "description": "Primary success criteria (2020 repair year).",
            "answer": (
                f"2020 excess={yr20_excess} (>-0.12: {yr20_excess > -0.12}), "
                f"dd={yr20_dd} (>-0.15: {yr20_dd > -0.15})"
            ),
            "pass": primary_pass,
        },
        "q2_selection_quality": {
            "description": "Test period secondary criteria.",
            "answer": (
                f"Test excess={test_excess} (>=0.15: {test_excess >= 0.15}), "
                f"dd={test_dd} (>= -0.10: {test_dd >= -0.10})"
            ),
            "pass": secondary_pass,
        },
        "q3_falsifiers": {
            "description": "Falsifier checks.",
            "answer": (
                f"2020 improvement={improvement_2020} (>0.05: {improvement_2020 > 0.05}), "
                f"test excess={test_excess} (>=0.10: {test_excess >= 0.10}), "
                f"train dd_worsened={dd_worsened_train}, "
                f"2020 dd_worsened={dd_worsened_2020}, "
                f"test dd_worsened={dd_worsened_test}"
            ),
            "pass": not falsified,
        },
        "overall_pass": adoption_pass,
        "overall_decision": decision,
        "overall_reason": reason,
        "auto_computed_at": now,
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- diagnostic.yaml ---
    diagnostic = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes",
        "verdict_referenced": decision,
        "summary": reason,
        "verdict_rationale": reason,
        "warnings": [],
        "errors": [],
        "best_params": best_params,
        "falsifier_details": {
            "improvement_2020_vs_baseline": round(improvement_2020, 6),
            "dd_worsened_train": bool(dd_worsened_train),
            "dd_worsened_2020": bool(dd_worsened_2020),
            "dd_worsened_test": bool(dd_worsened_test),
        },
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-hold-days", type=int, required=True)
    parser.add_argument("--initial-threshold-fraction", type=float, required=True)
    parser.add_argument("--max-hold-days", type=int, required=True)
    parser.add_argument("--floor-proximity-threshold", type=float, required=True)
    parser.add_argument("--floor-penalty-factor", type=float, required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    try:
        df_all = load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc)}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    # Filter periods
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_all[
        (df_all["trade_date"] >= train_start)
        & (df_all["trade_date"] <= train_end)
    ].copy()
    df_test = df_all[
        (df_all["trade_date"] >= test_start)
        & (df_all["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    # Grid search on train period
    print(
        "[gap_decay_bond_floor] Starting grid search on train period...",
        flush=True,
    )
    best_params, best_train_m, best_2020_m, bl_train, bl_2020 = grid_search_train(
        df_train, df_2020
    )

    # Run best config on full train + test
    best_train = run_single_config(
        df_train,
        best_params["min_hold_days"],
        best_params["initial_threshold_fraction"],
        best_params["max_hold_days"],
        best_params["floor_proximity_threshold"],
        best_params["floor_penalty_factor"],
    )
    best_test = run_single_config(
        df_test,
        best_params["min_hold_days"],
        best_params["initial_threshold_fraction"],
        best_params["max_hold_days"],
        best_params["floor_proximity_threshold"],
        best_params["floor_penalty_factor"],
    )
    bl_test = run_baseline(df_test)

    # Print summary
    print(
        f"[gap_decay_bond_floor] "
        f"best_params={best_params} "
        f"train_excess={best_train['excess_return']} "
        f"test_excess={best_test['excess_return']} "
        f"2020_excess={best_2020_m['excess_return']} "
        f"2020_dd={best_2020_m['max_drawdown']} "
        f"test_wr={best_test['win_rate']} "
        f"train_sharpe={best_train['sharpe_ratio']}",
        flush=True,
    )

    # Write artifacts
    _write_artifacts(
        output_dir,
        best_params,
        best_train,
        best_test,
        best_2020_m,
        bl_train,
        bl_2020,
        bl_test,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
