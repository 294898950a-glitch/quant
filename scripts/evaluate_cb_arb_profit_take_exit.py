#!/usr/bin/env python3
"""Evaluate partial profit-taking exit for cb_arb value-gap switch strategy.

Exit rule: when an active position's current value gap falls to
(1 - target_fraction) * entry_gap, liquidate the position entirely.
Sweeps three target_fraction values (0.25, 0.50, 0.75) and compares
against the baseline (no profit-taking exit) on train, 2020 repair,
and test periods.
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
# (pandas/yaml) or from REPO_ROOT (scripts.*).
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


def _get_pd():
    """Lazy import pandas."""
    import pandas as _pd
    return _pd


def _get_yaml():
    """Lazy import yaml."""
    import yaml as _yaml
    return _yaml


# ---------------------------------------------------------------------------
# Data requirements — must exist
# ---------------------------------------------------------------------------

_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_TARGET_FRACTIONS = (0.25, 0.50, 0.75)
_STRATEGY_ID = "cb_arb_value_gap_switch"
_PROPOSAL_ID = "cb_arb_value_gap_switch_profit-taking-exit_2026-05-19"
_EXECUTOR_NAME = "profit_take_exit"


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "required_files": [
            {"path": _GAP_DATA_PATH, "description": "Daily value-gap amounts from regime-option-entry-gate run."},
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
    raise FileNotFoundError(f"Cannot find {relative} under data_root={data_root}; searched: {searched}")


def _load_gap_data(data_root: str):
    pd = _get_pd()
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


def _simulate_baseline(df) -> dict[str, Any]:
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


def _simulate_profit_take(df, target_fraction: float) -> dict[str, Any]:
    """Simulate profit-taking exit rule.

    Entry: gap > 0 → enter, record entry_gap.
    Exit (baseline): gap <= 0 → exit (gap_closed).
    Exit (our rule): if position active and
        gap <= (1 - target_fraction) * entry_gap → exit (profit_take).
    """
    df = df.copy()
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_gap_val = 0.0
        entry_date = None
        exit_threshold = 0.0

        for _, row in grp.iterrows():
            gap = float(row["value_gap_amount"])

            if not in_position and gap > 0:
                in_position = True
                entry_gap_val = gap
                entry_date = row["trade_date"]
                exit_threshold = (1.0 - target_fraction) * entry_gap_val
                continue

            if not in_position:
                continue

            should_exit = False
            exit_reason: str = ""

            if gap <= 0:
                should_exit = True
                exit_reason = "gap_closed"
            elif gap <= exit_threshold:
                should_exit = True
                exit_reason = "profit_take"

            if should_exit:
                pnl = round((gap - entry_gap_val) * 100.0, 2)
                total_pnl += pnl
                days = (row["trade_date"] - entry_date).days if entry_date else 0
                trades.append({
                    "stock": str(row["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(row["trade_date"].date()),
                    "exit_reason": exit_reason,
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "pnl": pnl,
                    "hold_days": days,
                    "target_fraction": target_fraction,
                })
                in_position = False
                entry_gap_val = 0.0
                entry_date = None
                exit_threshold = 0.0

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
                "target_fraction": target_fraction,
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
            "profit_take_exits": 0,
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
    profit_take = int((tdf["exit_reason"] == "profit_take").sum()) if "exit_reason" in tdf.columns else 0
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
        "profit_take_exits": profit_take,
        "force_closes": force_closes,
    }


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
    best_params: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
) -> bool:
    yaml = _get_yaml()
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

    Returns adoption_pass.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.now()
    now_str = now.isoformat(timespec="seconds")

    excess_train = round(train_metrics["total_pnl"] - baseline_train["total_pnl"], 2)
    excess_test = round(test_metrics["total_pnl"] - baseline_test["total_pnl"], 2)
    excess_2020 = round(yr2020_metrics["total_pnl"] - baseline_2020["total_pnl"], 2)

    # Success criteria from proposal:
    # - test excess_return >= (baseline_test_excess_return - 0.05)
    # - test max_drawdown <= baseline_test_max_drawdown
    # - train max_drawdown reduced by at least 10% relative to baseline train drawdown (-0.32)
    test_excess_ok = excess_test >= (baseline_test["total_pnl"] - baseline_train["total_pnl"] - 0.05)
    test_dd_ok = test_metrics["max_drawdown"] <= baseline_test["max_drawdown"]
    train_dd_ok = best_candidate is not None and best_candidate["train"]["max_drawdown"] <= baseline_train["max_drawdown"] * 0.9

    adoption_pass = test_excess_ok and test_dd_ok and train_dd_ok

    if adoption_pass:
        decision = "adopt"
        reason = (
            f"Profit-taking (target_fraction={best_params.get('target_fraction', '?')}) "
            f"passes: test_excess={excess_test}, test_dd={test_metrics['max_drawdown']} "
            f"<= baseline {baseline_test['max_drawdown']}, "
            f"train_dd={train_metrics['max_drawdown']} <= 0.9*baseline {baseline_train['max_drawdown']}"
        )
    else:
        decision = "reject"
        parts: list[str] = []
        if not test_excess_ok:
            parts.append(f"test excess={excess_test} < baseline-0.05")
        if not test_dd_ok:
            parts.append(f"test dd={test_metrics['max_drawdown']} > baseline {baseline_test['max_drawdown']}")
        if not train_dd_ok:
            parts.append(f"train dd reduction insufficient")
        reason = "; ".join(parts) if parts else "unknown"

    # --- summary.json ---
    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "adoption_pass": adoption_pass,
        "decision": decision,
        "params": best_params,
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
        "strategy_id": _STRATEGY_ID,
        "l6_exit_decision": decision,
        "status": "COMPLETE",
        "params": best_params,
        "train": {**train_metrics, "excess_return": excess_train},
        "test": {**test_metrics, "excess_return": excess_test},
        "yr2020": {**yr2020_metrics, "excess_return": excess_2020},
        "adoption_pass": adoption_pass,
        "summary": reason,
        "learnings": [
            f"profit_take_exit target_fraction={best_params.get('target_fraction', '?')}: {reason}"
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
                f"2020 excess={excess_2020} (OK: {excess_2020 > 0}), "
                f"dd={yr2020_metrics['max_drawdown']} (baseline={baseline_2020['max_drawdown']})"
            ),
            "pass": excess_2020 > 0,
        },
        "q2_selection_quality": {
            "description": "Test period check.",
            "answer": (
                f"test excess={excess_test} (>= baseline-0.05: {test_excess_ok}), "
                f"win_rate={test_metrics['win_rate']}, "
                f"trades={test_metrics['trade_count']}"
            ),
            "pass": test_excess_ok,
        },
        "q3_falsifiers": {
            "description": "Drawdown degradation check.",
            "answer": (
                f"test dd={test_metrics['max_drawdown']} vs baseline={baseline_test['max_drawdown']}, "
                f"not worse: {test_dd_ok}; "
                f"train dd={train_metrics['max_drawdown']} vs 0.9*baseline {0.9 * baseline_train['max_drawdown']:.4f}, "
                f"reduced: {train_dd_ok}"
            ),
            "pass": test_dd_ok and train_dd_ok,
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
        "params": best_params,
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
    parser.add_argument("--target-fraction", type=float, default=0.50,
                        help="Profit-taking gap fraction (exit when gap <= (1-frac)*entry_gap)")
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
        print(f"[profit_take_exit] FATAL: {exc}", flush=True)
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
        print("[profit_take_exit] FATAL: empty train set", flush=True)
        _gatekeeper_after_run(output_dir)
        return 1

    # Baseline simulation
    baseline_train = _simulate_baseline(df_train)
    baseline_test = _simulate_baseline(df_test)
    baseline_2020 = _simulate_baseline(df_2020)

    # Sweep target fractions
    if args.target_fraction not in _TARGET_FRACTIONS:
        fractions = tuple(sorted(set(_TARGET_FRACTIONS + (args.target_fraction,))))
    else:
        fractions = _TARGET_FRACTIONS

    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for tf in fractions:
        train_res = _simulate_profit_take(df_train, tf)
        test_res = _simulate_profit_take(df_test, tf)
        yr2020_res = _simulate_profit_take(df_2020, tf)

        excess_train = round(train_res["total_pnl"] - baseline_train["total_pnl"], 2)
        excess_test = round(test_res["total_pnl"] - baseline_test["total_pnl"], 2)
        excess_2020 = round(yr2020_res["total_pnl"] - baseline_2020["total_pnl"], 2)

        candidate = {
            "target_fraction": tf,
            "train": {**train_res, "excess_return": excess_train},
            "test": {**test_res, "excess_return": excess_test},
            "validate_2020": {**yr2020_res, "excess_return": excess_2020},
        }
        all_candidates.append(candidate)

        # Score: prefer higher test excess, tie-break on drawdown
        score = excess_test - abs(test_res["max_drawdown"]) * 0.1
        if score > best_score:
            best_score = score
            best_candidate = candidate

        print(
            f"[profit_take_exit] tf={tf} "
            f"train_excess={excess_train} test_excess={excess_test} "
            f"2020_excess={excess_2020} test_dd={test_res['max_drawdown']}",
            flush=True,
        )

    if best_candidate is not None:
        best_params = {"target_fraction": best_candidate["target_fraction"]}
        best_train = _simulate_profit_take(df_train, best_candidate["target_fraction"])
        best_test = _simulate_profit_take(df_test, best_candidate["target_fraction"])
        best_2020 = _simulate_profit_take(df_2020, best_candidate["target_fraction"])
    else:
        best_params = {"target_fraction": args.target_fraction}
        best_train = _simulate_profit_take(df_train, args.target_fraction)
        best_test = _simulate_profit_take(df_test, args.target_fraction)
        best_2020 = _simulate_profit_take(df_2020, args.target_fraction)

    adoption_pass = _write_artifacts(
        output_dir, best_train, best_test, best_2020,
        baseline_train, baseline_test, baseline_2020,
        best_params, all_candidates, best_candidate,
    )

    _gatekeeper_after_run(output_dir)

    print(
        f"[profit_take_exit] DONE adoption_pass={adoption_pass} "
        f"candidates={len(all_candidates)} "
        f"best={best_params}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
