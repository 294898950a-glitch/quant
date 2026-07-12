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
from datetime import datetime as _dt
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
    import numpy as _np  # noqa: E402
    return _np


def _get_pd():
    """Lazy import pandas."""
    import pandas as _pd  # noqa: E402
    return _pd


def _get_yaml():
    """Lazy import yaml."""
    import yaml as _yaml  # noqa: E402
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
# Data requirements
# ---------------------------------------------------------------------------

_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
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
# Gatekeeper helpers
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
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_reference_tables():
    pd = _get_pd()
    cb_basic = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_basic.parquet")
    stk_daily = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/stk_daily_qfq.parquet")
    return cb_basic, stk_daily


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------


def _daily_metrics_from_gap_changes(
    df, position_col: str = "position"
):
    """Compute daily PnL, proportional returns, and drawdown.

    Returns (df with added columns, total_return, max_drawdown, win_rate).
    total_return and max_drawdown are in proportion-of-capital units.
    """
    np = _get_np()
    pd = _get_pd()
    df = df.sort_values(["ts_code", "trade_date"]).copy()

    # Daily PnL: change in gap x position flag
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


def run_single_period(
    df,
    gap_decay_factor: float,
    min_hold_days: int,
) -> dict[str, Any]:
    """Run the dynamic exit strategy on a single time period.

    df must contain: ts_code, trade_date, value_gap_amount.
    All rows are assumed to be positions (buy_qty > 0 in source data).

    Returns metrics dict.
    """
    pd = _get_pd()

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
    entry_gaps: list[float] = []  # noqa: F841 — kept for consistency

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

    df["position"] = position_flags

    # Counts
    total_positions = df["ts_code"].nunique()
    early_exits = int(df["position"].min() == 0)  # any bond exited early

    # Baseline: always hold while bond appears (position = 1 always)
    df["baseline_position"] = 1

    # PnL for our strategy
    df, our_total, our_dd, our_wr = _daily_metrics_from_gap_changes(df, "position")

    # PnL for baseline
    df, bl_total, bl_dd, bl_wr = _daily_metrics_from_gap_changes(df, "baseline_position")  # noqa: F841

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
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass (bool).
    """
    yaml = _get_yaml()
    now = _dt.now().isoformat(timespec="seconds")

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
    _ensure_yaml_np_reprs()
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": _dt.now().strftime("%Y-%m-%d"),
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
        "diagnostic_date": _dt.now().strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes_executor_code",
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

    return adoption_pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    pd = _get_pd()
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

    # GateKeeper: pre-flight compliance checks
    _gatekeeper_before_run(output_dir)

    params = {
        "gap_decay_factor": args.gap_decay_factor,
        "min_hold_days": args.min_hold_days,
    }

    # Load data
    try:
        df_all = _load_gap_data(args.data_root)
        _cb_basic, _stk_daily = _load_reference_tables()
    except Exception as exc:
        yaml = _get_yaml()
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
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
    adoption_pass = _write_artifacts(output_dir, train_metrics, test_metrics, yr2020_metrics, params)

    # GateKeeper: post-run checks
    _gatekeeper_after_run(output_dir)

    print(
        f"[dynamic_exit_gap_decay] DONE adoption_pass={adoption_pass} "
        f"factor={args.gap_decay_factor} min_hold={args.min_hold_days}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
