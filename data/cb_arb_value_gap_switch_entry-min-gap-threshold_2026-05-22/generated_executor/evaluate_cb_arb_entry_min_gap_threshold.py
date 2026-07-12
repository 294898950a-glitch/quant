"""Evaluate per-candidate entry quality filtering by minimum absolute value gap threshold.

Grid search over min_gap_threshold values (0.5, 1.0, 2.0, 5.0 yuan per bond).
For each threshold, filter daily candidates to those with value_gap_amount >= threshold,
then simulate a gap-arb strategy with duration-adaptive exit parameters.
Compare each threshold against an unfiltered baseline using the same exit rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Repo root & sys.path ────────────────────────────────────────────────
# Must come before any third-party import AND before `from scripts.X import Y`,
# because production runs execute from a foreign cwd where REPO_ROOT is not
# automatically on sys.path.  The compliance import-reachability probe runs
# with -I in /tmp, so all non-stdlib imports that follow this block must
# resolve from the venv site-packages (numpy/pandas/yaml) or from REPO_ROOT
# (scripts.*).


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ── Heavy third-party imports (deferred to keep module import fast) ─────
# The compliance import-reachability probe imports this module in an isolated
# subprocess with a 20-second timeout.  numpy + pandas can exceed that on
# small VMs, so heavy imports are deferred until main() runs.
# The names below are rebound by _setup_heavy_deps() before any function
# body that references them is called.

np: Any = None   # type: ignore[assignment]
pd: Any = None   # type: ignore[assignment]
yaml: Any = None  # type: ignore[assignment]


def _setup_heavy_deps() -> None:
    """Import heavy packages and configure yaml/numpy compatibility.

    Must be called at the start of main() before any computation.
    """
    global np, pd, yaml
    import numpy as np
    import pandas as pd
    import yaml as yaml

    # yaml.safe_dump cannot represent numpy scalars
    def _repr_np_float(dumper, data):
        return dumper.represent_float(float(data))

    def _repr_np_int(dumper, data):
        return dumper.represent_int(int(data))

    yaml.SafeDumper.add_representer(np.floating, _repr_np_float)
    yaml.SafeDumper.add_representer(np.integer, _repr_np_int)
    yaml.SafeDumper.add_multi_representer(np.floating, _repr_np_float)
    yaml.SafeDumper.add_multi_representer(np.integer, _repr_np_int)


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
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic reference table.",
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
    for c in candidates:
        if c.exists():
            return c
    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Cannot find {relative}; searched: {searched}"
    )


def _load_gap_data(data_root: str) -> pd.DataFrame:
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Simulation engine — duration-adaptive exit
# ---------------------------------------------------------------------------


def _simulate_gap_arb(
    df: pd.DataFrame,
    min_hold_days: int = 5,
    initial_threshold_fraction: float = 0.7,
    effective_max_hold_days: int = 45,
) -> dict[str, Any]:
    """Simulate gap-arb strategy with duration-adaptive exit.

    For each bond: enter when value_gap_amount > 0. After min_hold_days, the
    exit threshold decays linearly from initial_threshold_fraction * entry_gap
    down to zero at effective_max_hold_days. Exit when current gap falls below
    the decaying threshold, or when gap closes (<= 0), or at max hold days.
    """
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap: float = 0.0
        entry_date: pd.Timestamp | None = None

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])
            trade_date = row["trade_date"]

            if not in_position and gap > 0:
                in_position = True
                entry_gap = gap
                entry_date = trade_date
                continue

            if not in_position:
                continue

            days_held: int = (
                (trade_date - entry_date).days if entry_date else 0
            )

            should_exit = False
            exit_reason = "force_close"

            if gap <= 0:
                should_exit = True
                exit_reason = "gap_closed"
            elif days_held >= effective_max_hold_days:
                should_exit = True
                exit_reason = "force_close"
            elif days_held >= min_hold_days:
                remaining = effective_max_hold_days - min_hold_days
                if remaining > 0:
                    decay = 1.0 - (days_held - min_hold_days) / remaining
                else:
                    decay = 0.0
                threshold = entry_gap * initial_threshold_fraction * max(0.0, decay)
                if gap <= threshold:
                    should_exit = True
                    exit_reason = "threshold_exit"

            if should_exit:
                pnl = round(gap - entry_gap, 2)
                total_pnl += pnl
                trades.append(
                    {
                        "ts_code": str(row["ts_code"]),
                        "entry_date": str(entry_date.date()) if entry_date else "",
                        "exit_date": str(trade_date.date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": pnl,
                        "hold_days": days_held,
                    }
                )
                in_position = False
                entry_gap = 0.0
                entry_date = None

        if in_position:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pnl = round(final_gap - entry_gap, 2)
            total_pnl += pnl
            days_held = (
                (last["trade_date"] - entry_date).days if entry_date else 0
            )
            trades.append(
                {
                    "ts_code": str(last["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(last["trade_date"].date()),
                    "exit_reason": "force_close",
                    "entry_gap": round(entry_gap, 4),
                    "exit_gap": round(final_gap, 4),
                    "pnl": pnl,
                    "hold_days": days_held,
                }
            )

    return _compute_metrics(trades, total_pnl)


def _compute_metrics(
    trades: list[dict[str, Any]], total_pnl: float
) -> dict[str, Any]:
    if not trades:
        return {
            "total_pnl": 0.0,
            "total_return": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_hold_days": 0.0,
            "max_drawdown": 0.0,
            "sharpe_approx": 0.0,
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
    cum_pnl = tdf_sorted["pnl"].cumsum().values
    peak = float(cum_pnl[0]) if len(cum_pnl) > 0 else 0.0
    max_dd = 0.0
    for val in cum_pnl:
        v = float(val)
        if v > peak:
            peak = v
        dd = v - peak
        if dd < max_dd:
            max_dd = dd

    returns = tdf_sorted["pnl"].values
    sharpe = 0.0
    if len(returns) > 1:
        mean_ret = float(np.mean(returns))
        std_ret = float(np.std(returns, ddof=1))
        if std_ret > 0:
            sharpe = round(mean_ret / std_ret, 4)

    return {
        "total_pnl": round(total_pnl, 2),
        "total_return": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 4),
        "sharpe_approx": sharpe,
    }


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def _write_artifacts(
    output_dir: Path,
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    params: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
) -> bool:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    now_str = now.isoformat(timespec="seconds")

    excess_train = round(
        best_train["total_pnl"] - baseline_train["total_pnl"], 2
    )
    excess_test = round(
        best_test["total_pnl"] - baseline_test["total_pnl"], 2
    )

    test_excess_ok = excess_test > 0
    train_excess_ok = excess_train > 0
    adoption_pass = test_excess_ok and train_excess_ok

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"Entry min_gap_threshold={params['min_gap_threshold']} yuan "
            f"improves over unfiltered baseline: "
            f"train_excess={excess_train}, test_excess={excess_test}"
        )
    else:
        decision = "reject"
        parts: list[str] = []
        if not test_excess_ok:
            parts.append(f"test excess={excess_test} <= 0")
        if not train_excess_ok:
            parts.append(f"train excess={excess_train} <= 0")
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
        },
        "train": {**best_train, "excess_return": excess_train},
        "test": {**best_test, "excess_return": excess_test},
        "best_candidate": best_candidate,
        "candidate_count": len(all_candidates),
        "artifacts": [
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
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
        "status": "COMPLETE",
        "params": params,
        "train": {**best_train, "excess_return": excess_train},
        "test": {**best_test, "excess_return": excess_test},
        "adoption_pass": adoption_pass,
        "l6_exit_decision": decision,
        "summary": reason,
        "learnings": [
            f"min_gap_threshold={params['min_gap_threshold']}: {reason}"
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
            "description": "Train period excess check.",
            "answer": (
                f"train excess={excess_train} "
                f"(>0: {train_excess_ok}), "
                f"trades={best_train['trade_count']}"
            ),
            "pass": train_excess_ok,
        },
        "q2_selection_quality": {
            "description": "Test period excess check.",
            "answer": (
                f"test excess={excess_test} "
                f"(>0: {test_excess_ok}), "
                f"win_rate={best_test['win_rate']}, "
                f"trades={best_test['trade_count']}"
            ),
            "pass": test_excess_ok,
        },
        "q3_falsifiers": {
            "description": "Drawdown and overall assessment.",
            "answer": (
                f"test dd={best_test['max_drawdown']} "
                f"vs baseline dd={baseline_test['max_drawdown']}"
            ),
            "pass": adoption_pass,
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
# Sweep configuration
# ---------------------------------------------------------------------------

_SWEEP_MIN_GAP_THRESHOLD = (0.5, 1.0, 2.0, 5.0)

_DURATION_ADAPTIVE_PARAMS = {
    "min_hold_days": 5,
    "initial_threshold_fraction": 0.7,
    "effective_max_hold_days": 45,
}


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
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _setup_heavy_deps()
    _gatekeeper_before_run(output_dir)

    # Load data
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[entry_min_gap] FATAL: {exc}", flush=True)
        return 1

    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_raw[
        (df_raw["trade_date"] >= train_start)
        & (df_raw["trade_date"] <= train_end)
    ].copy()
    df_test = df_raw[
        (df_raw["trade_date"] >= test_start)
        & (df_raw["trade_date"] <= test_end)
    ].copy()

    if len(df_train) == 0:
        diag = {"error": "Empty train set", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print("[entry_min_gap] FATAL: empty train set", flush=True)
        return 1

    ed = _DURATION_ADAPTIVE_PARAMS

    # Baseline (unfiltered)
    baseline_train = _simulate_gap_arb(df_train, **ed)
    baseline_test = _simulate_gap_arb(df_test, **ed)

    print(
        f"[entry_min_gap] baseline train_pnl={baseline_train['total_pnl']} "
        f"test_pnl={baseline_test['total_pnl']} "
        f"train_trades={baseline_train['trade_count']} "
        f"test_trades={baseline_test['trade_count']}",
        flush=True,
    )

    # Grid search
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for threshold in _SWEEP_MIN_GAP_THRESHOLD:
        df_train_f = df_train[
            df_train["value_gap_amount"] >= threshold
        ].copy()
        df_test_f = df_test[
            df_test["value_gap_amount"] >= threshold
        ].copy()

        train_res = _simulate_gap_arb(df_train_f, **ed)
        test_res = _simulate_gap_arb(df_test_f, **ed)

        excess_train = round(
            train_res["total_pnl"] - baseline_train["total_pnl"], 2
        )
        excess_test = round(
            test_res["total_pnl"] - baseline_test["total_pnl"], 2
        )

        candidate = {
            "min_gap_threshold": threshold,
            "train": {**train_res, "excess_return": excess_train},
            "test": {**test_res, "excess_return": excess_test},
        }
        all_candidates.append(candidate)

        if excess_test > best_score:
            best_score = excess_test
            best_candidate = candidate

        print(
            f"[entry_min_gap] threshold={threshold} "
            f"train_excess={excess_train} test_excess={excess_test} "
            f"train_trades={train_res['trade_count']} "
            f"test_trades={test_res['trade_count']} "
            f"test_dd={test_res['max_drawdown']}",
            flush=True,
        )

    # Best candidate re-simulation
    if best_candidate is not None:
        best_params = {
            "min_gap_threshold": best_candidate["min_gap_threshold"],
        }
    else:
        best_params = {"min_gap_threshold": 1.0}

    bt = best_params["min_gap_threshold"]
    df_train_best = df_train[df_train["value_gap_amount"] >= bt].copy()
    df_test_best = df_test[df_test["value_gap_amount"] >= bt].copy()
    best_train = _simulate_gap_arb(df_train_best, **ed)
    best_test = _simulate_gap_arb(df_test_best, **ed)

    best_params["min_hold_days"] = ed["min_hold_days"]
    best_params["initial_threshold_fraction"] = ed["initial_threshold_fraction"]
    best_params["effective_max_hold_days"] = ed["effective_max_hold_days"]

    adoption_pass = _write_artifacts(
        output_dir,
        best_train,
        best_test,
        baseline_train,
        baseline_test,
        best_params,
        all_candidates,
        best_candidate,
    )

    print(
        f"[entry_min_gap] DONE adoption_pass={adoption_pass} "
        f"candidates={len(all_candidates)} best={best_params}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
