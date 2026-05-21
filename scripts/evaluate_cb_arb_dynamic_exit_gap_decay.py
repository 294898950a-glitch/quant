"""Evaluate dynamic trailing exit gap decay for cb_arb value-gap switch.

Exit rule: if current value_gap_amount < entry_gap_amount * gap_decay_factor
AND days held >= min_hold_days, then close the position early.

Compares against baseline (hold until bond disappears from daily data).
Reports metrics for train / 2020 repair / test periods.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Data requirements
# ---------------------------------------------------------------------------

def declare_data_requirements(command: list[str], spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet",
                "description": "Daily value-gap amounts with per-bond gap, position cash, and buy qty.",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic reference table (stock code, conversion price).",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted daily stock prices.",
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
        "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet",
    )
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def load_reference_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load cb_basic and stk_daily_qfq (used for sanity / future extension)."""
    cb_basic = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_basic.parquet")
    stk_daily = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/stk_daily_qfq.parquet")
    return cb_basic, stk_daily


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def _daily_metrics_from_gap_changes(
    df: pd.DataFrame, position_col: str = "position"
) -> tuple[pd.DataFrame, float, float, float]:
    """Compute daily PnL, proportional returns, and drawdown.

    Returns (df with added columns, total_return, max_drawdown, win_rate).
    total_return and max_drawdown are in proportion-of-capital units.
    """
    df = df.sort_values(["ts_code", "trade_date"]).copy()

    # Daily PnL: change in gap × position flag
    df["prev_gap"] = df.groupby("ts_code")["value_gap_amount"].shift(1)
    df["daily_pnl"] = df[position_col] * (df["value_gap_amount"] - df["prev_gap"])
    df["daily_pnl"] = df["daily_pnl"].fillna(0.0)

    # Daily portfolio value: sum of position_cash for held bonds
    held_mask = df[position_col] > 0
    daily_portfolio = df[held_mask].groupby("trade_date")["position_cash"].sum()
    daily_portfolio = daily_portfolio.reindex(df["trade_date"].unique(), fill_value=0.0)

    # Map portfolio value to each row
    port_map = daily_portfolio.to_dict()
    df["daily_portfolio"] = df["trade_date"].map(port_map).fillna(0.0)

    # Aggregate to daily level for return computation
    daily_pnl_agg = df.groupby("trade_date")["daily_pnl"].sum()
    dates = daily_pnl_agg.index.tolist()
    pnl_vals = daily_pnl_agg.values
    port_vals = daily_portfolio.values

    daily_returns = np.divide(
        pnl_vals,
        port_vals,
        out=np.zeros_like(pnl_vals, dtype=float),
        where=port_vals > 0,
    )

    total_return = float(np.sum(daily_returns))

    # Max drawdown on cumulative proportional returns
    cum_returns = np.cumsum(daily_returns)
    running_max = np.maximum.accumulate(cum_returns)
    drawdowns = cum_returns - running_max
    max_dd = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    # Win rate: fraction of bonds with positive cumulative PnL
    bond_pnl = df.groupby("ts_code")["daily_pnl"].sum()
    win_rate = float((bond_pnl > 0).mean()) if len(bond_pnl) > 0 else 0.0

    return df, total_return, max_dd, win_rate


def _trade_count(df: pd.DataFrame) -> int:
    """Count distinct bond entries (first row per bond in the period)."""
    return df["ts_code"].nunique()


def run_single_period(
    df: pd.DataFrame,
    gap_decay_factor: float,
    min_hold_days: int,
) -> dict[str, Any]:
    """Run the dynamic exit strategy on a single time period.

    df must contain: ts_code, trade_date, value_gap_amount.
    All rows are assumed to be positions (buy_qty > 0 in source data).

    Returns metrics dict.
    """
    if df.empty:
        return {
            "total_return": 0.0,
            "excess_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "trade_count": 0,
            "early_exits": 0,
            "total_positions": 0,
        }

    df = df.sort_values(["ts_code", "trade_date"]).copy()

    # Track per-bond state
    position_flags: list[int] = []
    entry_gaps: list[float] = []
    entry_dates: list[pd.Timestamp | pd.NaT] = []

    # Per-group state
    state: dict[str, dict[str, Any]] = {}

    for _, row in df.iterrows():
        code = str(row["ts_code"])
        trade_date = row["trade_date"]
        gap = float(row["value_gap_amount"])

        if code not in state:
            # First appearance = entry
            state[code] = {
                "entry_gap": gap,
                "entry_date": trade_date,
                "in_position": True,
            }

        st = state[code]
        if st["in_position"]:
            days_held = (trade_date - st["entry_date"]).days
            # Exit condition: gap < entry_gap * factor AND held >= min_hold_days
            if gap < st["entry_gap"] * gap_decay_factor and days_held >= min_hold_days:
                st["in_position"] = False

        position_flags.append(1 if st["in_position"] else 0)
        entry_gaps.append(st["entry_gap"])
        entry_dates.append(st["entry_date"])

    df["position"] = position_flags
    df["entry_gap"] = entry_gaps
    df["entry_date"] = entry_dates

    # Counts
    total_positions = df["ts_code"].nunique()
    early_exits = int(df["position"].min() == 0)  # any bond exited early

    # Baseline: always hold while bond appears (position = 1 always)
    df["baseline_position"] = 1

    # PnL for our strategy
    df, our_total, our_dd, our_wr = _daily_metrics_from_gap_changes(df, "position")

    # PnL for baseline
    df, bl_total, bl_dd, bl_wr = _daily_metrics_from_gap_changes(df, "baseline_position")
    # Note: baseline_position = 1 always, so baseline metrics come from the
    # same df with position_col = "baseline_position"

    excess = our_total - bl_total

    return {
        "total_return": round(our_total, 6),
        "excess_return": round(excess, 6),
        "max_drawdown": round(our_dd, 6),
        "win_rate": round(our_wr, 6),
        "trade_count": total_positions,
        "early_exits": early_exits,
        "total_positions": total_positions,
    }


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_artifacts(
    output_dir: Path,
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    yr2020_metrics: dict[str, Any],
    params: dict[str, Any],
) -> None:
    now = datetime.now().isoformat(timespec="seconds")

    # --- Adoption decision ---
    # Success criteria from proposal:
    #   primary:   2020 excess_return > -0.10 AND max_drawdown > -0.179
    #   secondary: Test total_return >= 0.70 AND win_rate >= 0.55
    # Falsifiers (any triggers rejection):
    #   - Test total_return < 0.50 OR win_rate < 0.45
    #   - 2020 max_drawdown not improved (deeper than -0.179)

    test_tr = test_metrics["total_return"]
    test_wr = test_metrics["win_rate"]
    yr20_excess = yr2020_metrics["excess_return"]
    yr20_dd = yr2020_metrics["max_drawdown"]
    train_dd = train_metrics["max_drawdown"]

    # Primary
    primary_pass = yr20_excess > -0.10 and yr20_dd > -0.179

    # Secondary
    secondary_pass = test_tr >= 0.70 and test_wr >= 0.55

    # Falsifiers
    falsified = (
        test_tr < 0.50
        or test_wr < 0.45
        or yr20_dd <= -0.179
    )

    adoption_pass = primary_pass and secondary_pass and not falsified

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Gap decay exit (factor={params['gap_decay_factor']}, "
            f"min_hold={params['min_hold_days']}) passes primary (2020) "
            f"and secondary (test) criteria without falsification."
        )
    else:
        decision = "reject"
        parts = []
        if not primary_pass:
            parts.append(
                f"2020 excess={yr20_excess} (need >-0.10), dd={yr20_dd} (need >-0.179)"
            )
        if not secondary_pass:
            parts.append(
                f"test total_return={test_tr} (need >=0.70), win_rate={test_wr} (need >=0.55)"
            )
        if falsified:
            parts.append("falsifier triggered")
        reason = "; ".join(parts) if parts else "unknown"

    # --- summary.json ---
    summary = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "adoption_pass": adoption_pass,
        "decision": decision,
        "params": params,
        "train": train_metrics,
        "test": test_metrics,
        "yr2020": yr2020_metrics,
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
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": decision,
        "status": "COMPLETE",
        "params": params,
        "train": train_metrics,
        "test": test_metrics,
        "yr2020": yr2020_metrics,
        "adoption_pass": adoption_pass,
        "summary": reason,
        "learnings": [
            f"Gap decay factor={params['gap_decay_factor']}, "
            f"min_hold_days={params['min_hold_days']}: {reason}",
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
        "ack_at": now,
        "q1_hard_floors": {
            "description": "Primary success criteria check (2020 repair year).",
            "answer": (
                f"2020 excess={yr20_excess} (>-0.10: {yr20_excess > -0.10}), "
                f"dd={yr20_dd} (>-0.179: {yr20_dd > -0.179})"
            ),
            "pass": primary_pass,
        },
        "q2_selection_quality": {
            "description": "Test period secondary criteria.",
            "answer": (
                f"Test total_return={test_tr} (>=0.70: {test_tr >= 0.70}), "
                f"win_rate={test_wr} (>=0.55: {test_wr >= 0.55})"
            ),
            "pass": secondary_pass,
        },
        "q3_falsifiers": {
            "description": "Falsifier checks.",
            "answer": (
                f"Test total_return={test_tr} (<0.50: {test_tr < 0.50}), "
                f"win_rate={test_wr} (<0.45: {test_wr < 0.45}), "
                f"2020 dd={yr20_dd} (<= -0.179: {yr20_dd <= -0.179})"
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
        "params": params,
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
    parser.add_argument("--gap-decay-factor", type=float, required=True)
    parser.add_argument("--min-hold-days", type=int, required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    params = {
        "gap_decay_factor": args.gap_decay_factor,
        "min_hold_days": args.min_hold_days,
    }

    # Load data
    try:
        df_all = load_gap_data(args.data_root)
        _cb_basic, _stk_daily = load_reference_tables()
    except Exception as exc:
        diag = {"error": str(exc), "traceback": str(exc)}
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
        (df_all["trade_date"] >= train_start) & (df_all["trade_date"] <= train_end)
    ].copy()
    df_test = df_all[
        (df_all["trade_date"] >= test_start) & (df_all["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    # Run
    train_metrics = run_single_period(df_train, args.gap_decay_factor, args.min_hold_days)
    test_metrics = run_single_period(df_test, args.gap_decay_factor, args.min_hold_days)
    yr2020_metrics = run_single_period(df_2020, args.gap_decay_factor, args.min_hold_days)

    # Print summary
    print(
        f"[dynamic_exit_gap_decay] factor={args.gap_decay_factor} "
        f"min_hold={args.min_hold_days} "
        f"train_excess={train_metrics['excess_return']} "
        f"test_excess={test_metrics['excess_return']} "
        f"2020_excess={yr2020_metrics['excess_return']} "
        f"2020_dd={yr2020_metrics['max_drawdown']} "
        f"test_tr={test_metrics['total_return']} "
        f"test_wr={test_metrics['win_rate']}",
        flush=True,
    )

    # Write artifacts
    _write_artifacts(output_dir, train_metrics, test_metrics, yr2020_metrics, params)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
