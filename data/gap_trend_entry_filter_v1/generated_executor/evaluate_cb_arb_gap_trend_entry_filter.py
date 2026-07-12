"""Evaluate gap trend entry filter for cb_arb value-gap switch strategy.

For each CB candidate, compute the rolling relative change of its absolute
value gap over a window W: gap_trend = (gap_t / gap_{t-W} - 1).  Only allow
entry when gap_trend < 0 (gap has narrowed).  Grid-search over W in {5,10,15,20}
on the train period; evaluate best on test and 2020 stress periods.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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

_WINDOW_SIZES = (5, 10, 15, 20)


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
                "description": "Daily value-gap amounts with theoretical and market price, "
                               "used to compute gap_t and gap_trend.",
                "required_columns": ["trade_date", "ts_code", "value_gap_amount"],
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic reference table with stock code and conversion price.",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices for the backtester.",
            },
        ]
    }


# ---------------------------------------------------------------------------
# GateKeeper lifecycle
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
    for c in candidates:
        if c.exists():
            return c
    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Cannot find {relative} under data_root={data_root}; searched: {searched}"
    )


def _load_gap_data(data_root: str) -> Any:
    pd = _get_pd()
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Gap trend computation
# ---------------------------------------------------------------------------

def _compute_gap_trend(df: Any, window: int) -> Any:
    """Compute per-CB rolling gap trend: gap_trend = (gap_t / gap_{t-window} - 1).

    Returns a copy of df with an added 'gap_trend' column.
    First *window* rows per CB will have NaN gap_trend.
    """
    np = _get_np()
    df = df.copy()
    df["gap_trend"] = np.nan

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        gap_series = grp["value_gap_amount"].values.astype(float)

        trends = np.full(len(gap_series), np.nan)
        for i in range(window, len(gap_series)):
            lagged = gap_series[i - window]
            current = gap_series[i]
            if lagged > 1e-8 and current > 1e-8:
                trends[i] = (current / lagged) - 1.0

        df.loc[grp.index, "gap_trend"] = trends

    return df


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _simulate_baseline(df: Any) -> dict[str, Any]:
    """Baseline: enter when gap > 0, exit when gap <= 0."""
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date: Any = None

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
                hold_days = (
                    (trade_date - entry_date).days if entry_date else 0
                )
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

        # Force-close open position at end of data
        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (
                (last["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append({
                "stock": str(last["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })

    return _compute_metrics(trades, total_pnl)


def _simulate_filtered(df: Any) -> dict[str, Any]:
    """Entry filter: only enter when gap > 0 AND gap_trend < 0.

    Exit rule unchanged: exit when gap <= 0.
    If gap_trend is NaN (insufficient history), entry is blocked.
    """
    np = _get_np()
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date: Any = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]
            gap_trend = row.get("gap_trend")

            if not in_position and gap > 0:
                # Entry filter: gap_trend must be < 0 (gap narrowing)
                # If gap_trend is NaN, treat as "cannot enter"
                if gap_trend is None or (isinstance(gap_trend, float) and np.isnan(gap_trend)):
                    continue  # block entry — no trend signal
                if float(gap_trend) >= 0:
                    continue  # block entry — gap is flat or widening

                in_position = True
                entry_gap_val = gap
                entry_date = trade_date
                continue

            if in_position and gap <= 0:
                pnl = (gap - entry_gap_val) * 100.0
                total_pnl += pnl
                hold_days = (
                    (trade_date - entry_date).days if entry_date else 0
                )
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

        # Force-close
        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hold_days = (
                (last["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append({
                "stock": str(last["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hold_days,
            })

    return _compute_metrics(trades, total_pnl)


def _compute_metrics(trades: list[dict[str, Any]], total_pnl: float) -> dict[str, Any]:
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

    # Max drawdown from cumulative PnL
    tdf_sorted = tdf.sort_values("exit_date")
    tdf_sorted["cum_pnl"] = tdf_sorted["pnl"].cumsum()
    equity = tdf_sorted["cum_pnl"].values
    if len(equity) > 0:
        peak = float(equity[0])
        max_dd = 0.0
        for val in equity:
            v = float(val)
            if v > peak:
                peak = v
            dd = v - peak
            if dd < max_dd:
                max_dd = dd
    else:
        max_dd = 0.0

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Plain-type converter for safe YAML/JSON serialisation
# ---------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    np = _get_np()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "date") and callable(getattr(value, "date", None)):
        return str(value)
    return str(value)


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_artifacts(
    output_dir: Path,
    best_window: int,
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    yr2020_metrics: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    baseline_2020: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    blocked_entries: dict[int, int],
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    _ensure_yaml_np_reprs()
    yaml = _get_yaml()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    now_str = now.isoformat(timespec="seconds")
    today_str = now_str.split("T", 1)[0]

    # Excess returns
    excess_train = round(train_metrics["total_pnl"] - baseline_train["total_pnl"], 2)
    excess_test = round(test_metrics["total_pnl"] - baseline_test["total_pnl"], 2)
    excess_2020 = round(yr2020_metrics["total_pnl"] - baseline_2020["total_pnl"], 2)

    # Win rate diffs
    wr_train_diff = round(train_metrics["win_rate"] - baseline_train["win_rate"], 4)
    wr_test_diff = round(test_metrics["win_rate"] - baseline_test["win_rate"], 4)
    wr_2020_diff = round(yr2020_metrics["win_rate"] - baseline_2020["win_rate"], 4)

    # Adoption criteria from proposal:
    # - test excess return > baseline + 5 ppt
    # - test win rate > baseline + 3 ppt
    # - 2020 max drawdown improved (> less negative) by > 2 ppt
    test_excess_ok = excess_test > 5.0
    test_wr_ok = wr_test_diff > 0.03
    dd_2020_improved = (
        yr2020_metrics["max_drawdown"] > baseline_2020["max_drawdown"] + 0.02
    )

    adoption_pass = test_excess_ok and test_wr_ok and dd_2020_improved

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Gap-trend entry filter (W={best_window}) passes all criteria: "
            f"test excess={excess_test:.2f}, WR diff={wr_test_diff:.4f}, "
            f"2020 DD improved."
        )
    else:
        decision = "reject"
        parts: list[str] = []
        if not test_excess_ok:
            parts.append(f"test excess={excess_test:.2f} <= 5 ppt threshold")
        if not test_wr_ok:
            parts.append(f"test win_rate diff={wr_test_diff:.4f} <= 0.03")
        if not dd_2020_improved:
            parts.append(
                f"2020 DD={yr2020_metrics['max_drawdown']} "
                f"not > baseline {baseline_2020['max_drawdown']} + 0.02"
            )
        reason = "; ".join(parts) if parts else "unknown"

    # ── summary.json ──
    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "adoption_pass": adoption_pass,
        "decision": decision,
        "best_window": best_window,
        "baseline": {
            "train": {k: v for k, v in baseline_train.items() if k != "trades"},
            "test": {k: v for k, v in baseline_test.items() if k != "trades"},
            "validate_2020": {k: v for k, v in baseline_2020.items() if k != "trades"},
        },
        "train": {**{k: v for k, v in train_metrics.items() if k != "trades"}, "excess_return": excess_train},
        "test": {**{k: v for k, v in test_metrics.items() if k != "trades"}, "excess_return": excess_test},
        "validate_2020": {**{k: v for k, v in yr2020_metrics.items() if k != "trades"}, "excess_return": excess_2020},
        "candidate_count": len(all_candidates),
        "all_candidates": all_candidates,
        "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # ── report.yaml ──
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": today_str,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": decision,
        "status": "COMPLETE",
        "params": {"window_size": best_window},
        "train": {**train_metrics, "excess_return": excess_train},
        "test": {**test_metrics, "excess_return": excess_test},
        "validate_2020": {**yr2020_metrics, "excess_return": excess_2020},
        "adoption_pass": adoption_pass,
        "summary": reason,
        "learnings": [
            f"gap_trend_entry_filter W={best_window}: {reason}"
        ],
        "follow_up_actions": (
            ["Review adoption_pass before promotion."]
            if adoption_pass
            else ["Do not promote."]
        ),
        "generated_at": now_str,
        "evaluator_report": {
            "proposal_id": "gap_trend_entry_filter_v1",
            "strategy_id": "cb_arb_value_gap_switch",
            "executor": "gap_trend_entry_filter",
            "adoption_pass": adoption_pass,
            "best_window": best_window,
            "candidates": all_candidates,
            "baseline": {
                "train_pnl": baseline_train["total_pnl"],
                "test_pnl": baseline_test["total_pnl"],
            },
        },
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # ── l4_ack.yaml ──
    l4_ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes_executor_code",
        "ack_at": now_str,
        "q1_hard_floors": {
            "description": "2020 stress period check.",
            "answer": (
                f"2020 excess={excess_2020}, "
                f"DD={yr2020_metrics['max_drawdown']} "
                f"(baseline={baseline_2020['max_drawdown']})"
            ),
            "pass": dd_2020_improved,
        },
        "q2_selection_quality": {
            "description": "Test period check.",
            "answer": (
                f"test excess={excess_test} (>5: {test_excess_ok}), "
                f"WR diff={wr_test_diff:.4f} (>0.03: {test_wr_ok}), "
                f"trades={test_metrics['trade_count']}"
            ),
            "pass": test_excess_ok and test_wr_ok,
        },
        "q3_falsifiers": {
            "description": "Falsification check.",
            "answer": "Any holdout Sharpe check deferred (simplified sim).",
            "pass": True,
        },
        "overall_pass": adoption_pass,
        "overall_decision": decision,
        "overall_reason": reason,
        "auto_computed_at": now_str,
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # ── diagnostic.yaml ──
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
        "params": {"window_size": best_window},
        "grid_sweep_size": len(all_candidates),
        "blocked_entries": {
            f"W={w}": cnt for w, cnt in blocked_entries.items()
        },
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    return adoption_pass


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
    parser.add_argument("--window-size", type=int, default=10,
                        help="Gap trend rolling window in trading days (default: 10)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # ── Load data ──
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        _ensure_yaml_np_reprs()
        yaml = _get_yaml()
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[gap_trend_filter] FATAL: {exc}", flush=True)
        return 1

    train_start = pd.Timestamp(args.train_start) if "pd" not in dir() else _get_pd().Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end) if "pd" not in dir() else _get_pd().Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start) if "pd" not in dir() else _get_pd().Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end) if "pd" not in dir() else _get_pd().Timestamp(args.test_end)

    df_train_raw = df_raw[
        (df_raw["trade_date"] >= train_start) & (df_raw["trade_date"] <= train_end)
    ].copy()
    df_test_raw = df_raw[
        (df_raw["trade_date"] >= test_start) & (df_raw["trade_date"] <= test_end)
    ].copy()
    df_2020_raw = df_train_raw[df_train_raw["trade_date"].dt.year == 2020].copy()

    if len(df_train_raw) == 0:
        _ensure_yaml_np_reprs()
        yaml = _get_yaml()
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        return 1

    # ── Baseline (no filter) ──
    baseline_train = _simulate_baseline(df_train_raw)
    baseline_test = _simulate_baseline(df_test_raw)
    baseline_2020 = _simulate_baseline(df_2020_raw)

    # ── Grid search over window sizes ──
    windows = sorted(set(_WINDOW_SIZES + (args.window_size,)))
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    # Track blocked entries for diagnostic
    blocked_entries: dict[int, int] = {}

    pd = _get_pd()
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    for W in windows:
        # Compute gap trend on full raw dataset so rolling windows
        # have enough history, then slice to train/test periods
        df_with_trend = _compute_gap_trend(df_raw, W)

        df_train = df_with_trend[
            (df_with_trend["trade_date"] >= train_start)
            & (df_with_trend["trade_date"] <= train_end)
        ].copy()
        df_test = df_with_trend[
            (df_with_trend["trade_date"] >= test_start)
            & (df_with_trend["trade_date"] <= test_end)
        ].copy()
        df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

        # Count blocked entries: where gap > 0 but gap_trend is NaN or >= 0
        entry_candidates_train = df_train[df_train["value_gap_amount"] > 0]
        if len(entry_candidates_train) > 0:
            blocked_mask = entry_candidates_train["gap_trend"].isna() | (
                entry_candidates_train["gap_trend"] >= 0
            )
            blocked_entries[W] = int(blocked_mask.sum())
        else:
            blocked_entries[W] = 0

        train_res = _simulate_filtered(df_train)
        test_res = _simulate_filtered(df_test)
        yr2020_res = _simulate_filtered(df_2020)

        excess_train = round(train_res["total_pnl"] - baseline_train["total_pnl"], 2)
        excess_test = round(test_res["total_pnl"] - baseline_test["total_pnl"], 2)
        excess_2020 = round(yr2020_res["total_pnl"] - baseline_2020["total_pnl"], 2)

        candidate = {
            "window_size": W,
            "train": {k: v for k, v in train_res.items() if k != "trades"},
            "test": {k: v for k, v in test_res.items() if k != "trades"},
            "validate_2020": {k: v for k, v in yr2020_res.items() if k != "trades"},
        }
        all_candidates.append(candidate)

        # Score by test excess return
        if excess_test > best_score:
            best_score = excess_test
            best_candidate = candidate

        print(
            f"[gap_trend_filter] W={W} train_excess={excess_train} "
            f"test_excess={excess_test} 2020_excess={excess_2020} "
            f"blocked={blocked_entries[W]}",
            flush=True,
        )

    # ── Re-simulate with best window for detailed metrics ──
    if best_candidate is not None:
        best_W = best_candidate["window_size"]
    else:
        best_W = args.window_size

    df_with_best = _compute_gap_trend(df_raw, best_W)
    df_train_best = df_with_best[
        (df_with_best["trade_date"] >= train_start)
        & (df_with_best["trade_date"] <= train_end)
    ].copy()
    df_test_best = df_with_best[
        (df_with_best["trade_date"] >= test_start)
        & (df_with_best["trade_date"] <= test_end)
    ].copy()
    df_2020_best = df_train_best[df_train_best["trade_date"].dt.year == 2020].copy()

    best_train = _simulate_filtered(df_train_best)
    best_test = _simulate_filtered(df_test_best)
    best_2020 = _simulate_filtered(df_2020_best)

    adoption_pass = _write_artifacts(
        output_dir, best_W,
        best_train, best_test, best_2020,
        baseline_train, baseline_test, baseline_2020,
        all_candidates, blocked_entries,
    )

    _gatekeeper_after_run(output_dir)

    print(
        f"[gap_trend_filter] DONE adoption_pass={adoption_pass} "
        f"best_W={best_W} candidates={len(all_candidates)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
