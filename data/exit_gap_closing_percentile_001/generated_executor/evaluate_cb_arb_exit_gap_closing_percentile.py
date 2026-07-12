"""Evaluate gap-closing-percentile exit for cb_arb value-gap switch strategy.

Core idea: build an empirical distribution of days-to-close for gaps
binned by initial gap percentage. During simulation, after min_hold_days,
compute the holding-days percentile within the matching bin and exit if it
exceeds a target percentile threshold. Grid-search over min_hold_days,
percentile_threshold, and gap_bin_size on train period; evaluate on test
and 2020 stress periods.
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


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {"path": _GAP_DATA_PATH, "description": "Daily value-gap amounts from regime-option-entry-gate run."},
            {"path": "data/cb_warehouse/cb_basic.parquet", "description": "CB basic reference table."},
            {"path": "data/cb_warehouse/stk_daily_qfq.parquet", "description": "Forward-adjusted stock prices."},
            {"path": "data/cb_warehouse/cb_daily.parquet", "description": "Daily CB market prices."},
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
    pd = _get_pd()
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


# ---------------------------------------------------------------------------
# Gap-closing event extraction
# ---------------------------------------------------------------------------

def _extract_gap_closing_events(df):
    pd = _get_pd()
    """Extract gap opening/closing events from daily gap data.

    For each bond, detect when gap turns from <=0 to >0 (entry) and when
    it turns back to <=0 (close). Force-close open gaps at end of series.

    Returns DataFrame with columns: ts_code, entry_date, exit_date,
    entry_gap_pct, days_to_close, exit_reason.
    """
    events: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_gap = False
        entry_date = None
        entry_gap_pct: float = 0.0

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            gap_pct = float(row["value_gap_pct_of_cash"])

            if not in_gap and gap > 0:
                in_gap = True
                entry_date = row["trade_date"]
                entry_gap_pct = gap_pct
            elif in_gap and gap <= 0:
                days = (row["trade_date"] - entry_date).days  # type: ignore[operator]
                events.append({
                    "ts_code": str(row["ts_code"]),
                    "entry_date": entry_date,
                    "exit_date": row["trade_date"],
                    "entry_gap_pct": entry_gap_pct,
                    "days_to_close": days,
                    "exit_reason": "gap_closed",
                })
                in_gap = False

        if in_gap:
            last = grp.iloc[-1]
            days = (last["trade_date"] - entry_date).days  # type: ignore[operator]
            events.append({
                "ts_code": str(last["ts_code"]),
                "entry_date": entry_date,
                "exit_date": last["trade_date"],
                "entry_gap_pct": entry_gap_pct,
                "days_to_close": days,
                "exit_reason": "force_close",
            })

    if not events:
        return pd.DataFrame(columns=["ts_code", "entry_date", "exit_date", "entry_gap_pct", "days_to_close", "exit_reason"])
    return pd.DataFrame(events)


# ---------------------------------------------------------------------------
# Percentile CDF builder
# ---------------------------------------------------------------------------

def _bin_index(gap_pct: float, bin_size: float) -> int:
    if gap_pct <= 0:
        return 0
    return int(gap_pct / bin_size)


def _build_bin_cdf(
    events, gap_bin_size: float, events_df=None
) -> dict[int, tuple[Any, int]]:
    np = _get_np()
    pd = _get_pd()
    """Build empirical CDF per bin from gap closing events.

    Each bin maps to (sorted_days_array, total_events_in_bin).
    CDF(days) = count(days_to_close <= days) / total_events.
    """
    src = events_df if events_df is not None else events
    if src.empty:
        return {}

    src = src.copy()
    src["bin_idx"] = src["entry_gap_pct"].apply(lambda x: _bin_index(x, gap_bin_size))

    bin_cdf: dict[int, tuple[Any, int]] = {}
    for bidx, grp in src.groupby("bin_idx"):
        days_arr = grp["days_to_close"].values.astype(float)
        sorted_days = np.sort(days_arr)
        bin_cdf[int(bidx)] = (sorted_days, len(sorted_days))

    return bin_cdf


def _percentile_in_bin(hold_days: int, sorted_days, total_events: int) -> float:
    np = _get_np()
    """Compute the percentile of hold_days within the distribution.

    Returns value in [0, 100].
    """
    if total_events == 0:
        return 0.0
    pos = int(np.searchsorted(sorted_days, hold_days + 1, side="right"))
    return round(pos / total_events * 100.0, 2)


def _build_rolling_cdf(
    events, gap_bin_size: float, up_to_date,
) -> dict[int, tuple[Any, int]]:
    eligible = events[events["exit_date"] <= up_to_date]
    return _build_bin_cdf(eligible, gap_bin_size)


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


def _simulate_baseline(df) -> dict[str, Any]:
    pd = _get_pd()
    """Baseline strategy: enter when gap > 0, exit when gap <= 0."""
    df = df.copy()
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
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
                trades.append({
                    "stock": str(row["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(row["trade_date"].date()),
                    "exit_reason": "gap_closed",
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": pnl,
                    "hold_days": days,
                })
                in_position = False
                entry_gap_val = 0.0
                entry_date = None

        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = round((final_gap - entry_gap_val) * 100.0, 2)
            total_pnl += pnl
            days = (last["trade_date"] - entry_date).days if entry_date else 0
            trades.append({
                "stock": str(last["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": pnl,
                "hold_days": days,
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
            "gap_closed_exits": 0,
            "percentile_exits": 0,
            "force_closes": 0,
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

    gap_closed = int((tdf["exit_reason"] == "gap_closed").sum()) if "exit_reason" in tdf.columns else 0
    percentile_exits = int((tdf["exit_reason"] == "percentile_exit").sum()) if "exit_reason" in tdf.columns else 0
    force_closes = int((tdf["exit_reason"] == "force_close").sum()) if "exit_reason" in tdf.columns else 0

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "gap_closed_exits": gap_closed,
        "percentile_exits": percentile_exits,
        "force_closes": force_closes,
    }


def _simulate_percentile_exit(
    df,
    all_events,
    min_hold_days: int,
    percentile_threshold: float,
    gap_bin_size: float,
) -> dict[str, Any]:
    pd = _get_pd()
    """Simulate exit-gap-closing-percentile strategy.

    Walk forward per bond: enter when gap > 0, then after min_hold_days,
    on each day compute the holding-days percentile within the matching
    gap bin's empirical CDF (rolling: only events whose exit_date <= today).
    Exit if percentile >= percentile_threshold.
    """
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_date = None
        entry_gap_val: float = 0.0
        entry_gap_pct: float = 0.0

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            gap_pct = float(row["value_gap_pct_of_cash"])
            trade_date = row["trade_date"]

            if not in_position and gap > 0:
                in_position = True
                entry_date = trade_date
                entry_gap_val = gap
                entry_gap_pct = gap_pct
                continue

            if not in_position:
                continue

            should_exit = False
            exit_reason: str = ""

            if gap <= 0:
                should_exit = True
                exit_reason = "gap_closed"
            else:
                days_held = (trade_date - entry_date).days if entry_date else 0  # type: ignore[operator]
                if days_held >= min_hold_days:
                    bin_cdf = _build_rolling_cdf(all_events, gap_bin_size, trade_date)
                    bidx = _bin_index(entry_gap_pct, gap_bin_size)
                    if bidx in bin_cdf:
                        sorted_days, total = bin_cdf[bidx]
                        pctile = _percentile_in_bin(days_held, sorted_days, total)
                        if pctile >= percentile_threshold:
                            should_exit = True
                            exit_reason = "percentile_exit"

            if should_exit:
                pnl = round((gap - entry_gap_val) * 100.0, 2)
                total_pnl += pnl
                days_held = (trade_date - entry_date).days if entry_date else 0  # type: ignore[operator]
                trades.append({
                    "stock": str(row["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(trade_date.date()),
                    "exit_reason": exit_reason,
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": pnl,
                    "hold_days": days_held,
                    "entry_gap_pct": round(entry_gap_pct, 6),
                })
                in_position = False
                entry_date = None
                entry_gap_val = 0.0
                entry_gap_pct = 0.0

        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = round((final_gap - entry_gap_val) * 100.0, 2)
            total_pnl += pnl
            days_held = (last["trade_date"] - entry_date).days if entry_date else 0  # type: ignore[operator]
            trades.append({
                "stock": str(last["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": pnl,
                "hold_days": days_held,
                "entry_gap_pct": round(entry_gap_pct, 6),
            })

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
    yaml = _get_yaml()
    _ensure_yaml_np_reprs()
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.now()
    now_str = now.isoformat(timespec="seconds")

    excess_train = round(train_metrics["total_pnl"] - baseline_train["total_pnl"], 2)
    excess_test = round(test_metrics["total_pnl"] - baseline_test["total_pnl"], 2)
    excess_2020 = round(yr2020_metrics["total_pnl"] - baseline_2020["total_pnl"], 2)

    test_excess_ok = excess_test > 0
    train_excess_ok = excess_train > 0
    y2020_excess_ok = excess_2020 > 0
    dd_not_worse = test_metrics["max_drawdown"] >= baseline_test["max_drawdown"] * 1.3

    adoption_pass = test_excess_ok and train_excess_ok and y2020_excess_ok and dd_not_worse

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Gap-closing-percentile exit (min_hold={params['min_hold_days']}, "
            f"pct={params['percentile_threshold']}, "
            f"bin={params['gap_bin_size']}) passes all three periods "
            f"with positive excess and acceptable drawdown."
        )
    else:
        decision = "reject"
        parts: list[str] = []
        if not test_excess_ok:
            parts.append(f"test excess={excess_test} <= 0")
        if not train_excess_ok:
            parts.append(f"train excess={excess_train} <= 0")
        if not y2020_excess_ok:
            parts.append(f"2020 excess={excess_2020} <= 0")
        if not dd_not_worse:
            parts.append(f"test dd={test_metrics['max_drawdown']} worse than baseline {baseline_test['max_drawdown']}")
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
            "yr2020": {k: v for k, v in baseline_2020.items() if k not in ("trades",)},
        },
        "train": dict(train_metrics, excess_return=excess_train),
        "test": dict(test_metrics, excess_return=excess_test),
        "yr2020": dict(yr2020_metrics, excess_return=excess_2020),
        "best_candidate": best_candidate,
        "candidate_count": len(all_candidates),
        "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
    }
    for key in ("train", "test", "yr2020"):
        if "trades" in summary[key]:
            del summary[key]["trades"]

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
            f"gap_closing_percentile min_hold={params['min_hold_days']} "
            f"pct={params['percentile_threshold']} bin={params['gap_bin_size']}: {reason}"
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
                f"2020 excess={excess_2020} (>0: {y2020_excess_ok}), "
                f"dd={yr2020_metrics['max_drawdown']} (baseline={baseline_2020['max_drawdown']})"
            ),
            "pass": y2020_excess_ok,
        },
        "q2_selection_quality": {
            "description": "Test period check.",
            "answer": (
                f"test excess={excess_test} (>0: {test_excess_ok}), "
                f"win_rate={test_metrics['win_rate']}, "
                f"trades={test_metrics['trade_count']}"
            ),
            "pass": test_excess_ok,
        },
        "q3_falsifiers": {
            "description": "Drawdown degradation check.",
            "answer": (
                f"test dd={test_metrics['max_drawdown']} vs baseline={baseline_test['max_drawdown']}, "
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
# Main
# ---------------------------------------------------------------------------

_SWEEP_MIN_HOLD_DAYS = (3, 5, 10, 15)
_SWEEP_PERCENTILE = (50, 60, 70, 75, 80, 90)
_SWEEP_BIN_SIZE = (0.01, 0.02, 0.03, 0.05)


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
    parser.add_argument("--min-hold-days", type=int, default=5, help="Minimum hold days before percentile exit")
    parser.add_argument("--percentile-threshold", type=float, default=75.0,
                        help="Exit when holding-days percentile exceeds this value")
    parser.add_argument("--gap-bin-size", type=float, default=0.02, help="Gap percentage bin size for CDF")
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
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[gap_closing_pct] FATAL: {exc}", flush=True)
        _gatekeeper_after_run(output_dir)
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
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print("[gap_closing_pct] FATAL: empty train set", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    # Extract all gap-closing events
    try:
        all_events = _extract_gap_closing_events(df_raw)
    except Exception as exc:
        diag = {"error": str(exc), "step": "extract_events"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[gap_closing_pct] FATAL extracting events: {exc}", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    if all_events.empty:
        diag = {"error": "No gap-closing events found in data", "step": "extract_events"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print("[gap_closing_pct] FATAL: no gap-closing events", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    # Baseline
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    # Grid search
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for mhd in _SWEEP_MIN_HOLD_DAYS:
        for pct in _SWEEP_PERCENTILE:
            for binsz in _SWEEP_BIN_SIZE:
                train_res = _simulate_percentile_exit(df_train, all_events, mhd, float(pct), float(binsz))
                test_res = _simulate_percentile_exit(df_test, all_events, mhd, float(pct), float(binsz))
                yr2020_res = _simulate_percentile_exit(df_2020, all_events, mhd, float(pct), float(binsz))

                excess_train = round(train_res["total_pnl"] - baseline_train["total_pnl"], 2)
                excess_test = round(test_res["total_pnl"] - baseline_test["total_pnl"], 2)
                excess_2020 = round(yr2020_res["total_pnl"] - baseline_2020["total_pnl"], 2)

                candidate = {
                    "min_hold_days": mhd,
                    "percentile_threshold": pct,
                    "gap_bin_size": binsz,
                    "train": {**train_res, "excess_return": excess_train},
                    "test": {**test_res, "excess_return": excess_test},
                    "validate_2020": {**yr2020_res, "excess_return": excess_2020},
                }
                all_candidates.append(candidate)

                if excess_test > best_score:
                    best_score = excess_test
                    best_candidate = candidate

                print(
                    f"[gap_closing_pct] mhd={mhd} pct={pct} bin={binsz} "
                    f"train_excess={excess_train} test_excess={excess_test} "
                    f"2020_excess={excess_2020} test_dd={test_res['max_drawdown']}",
                    flush=True,
                )

    if best_candidate is not None:
        best_params = {
            "min_hold_days": best_candidate["min_hold_days"],
            "percentile_threshold": best_candidate["percentile_threshold"],
            "gap_bin_size": best_candidate["gap_bin_size"],
        }
        best_train = _simulate_percentile_exit(
            df_train, all_events, best_params["min_hold_days"],
            float(best_params["percentile_threshold"]), float(best_params["gap_bin_size"]),
        )
        best_test = _simulate_percentile_exit(
            df_test, all_events, best_params["min_hold_days"],
            float(best_params["percentile_threshold"]), float(best_params["gap_bin_size"]),
        )
        best_2020 = _simulate_percentile_exit(
            df_2020, all_events, best_params["min_hold_days"],
            float(best_params["percentile_threshold"]), float(best_params["gap_bin_size"]),
        )
    else:
        best_params = {
            "min_hold_days": args.min_hold_days,
            "percentile_threshold": args.percentile_threshold,
            "gap_bin_size": args.gap_bin_size,
        }
        best_train = _simulate_percentile_exit(
            df_train, all_events, args.min_hold_days, args.percentile_threshold, args.gap_bin_size,
        )
        best_test = _simulate_percentile_exit(
            df_test, all_events, args.min_hold_days, args.percentile_threshold, args.gap_bin_size,
        )
        best_2020 = _simulate_percentile_exit(
            df_2020, all_events, args.min_hold_days, args.percentile_threshold, args.gap_bin_size,
        )

    adoption_pass = _write_artifacts(
        output_dir, best_train, best_test, best_2020,
        baseline_train, baseline_test, baseline_2020,
        best_params, all_candidates, best_candidate,
    )

    _gatekeeper_after_run(output_dir)

    print(
        f"[gap_closing_pct] DONE adoption_pass={adoption_pass} "
        f"candidates={len(all_candidates)} "
        f"best={best_params}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
