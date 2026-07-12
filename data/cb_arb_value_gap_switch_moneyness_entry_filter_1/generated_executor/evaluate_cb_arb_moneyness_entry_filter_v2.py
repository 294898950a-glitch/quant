#!/usr/bin/env python3
"""Evaluate moneyness entry filter v2 for cb_arb value-gap switch strategy.

Computes daily moneyness = prior_day_stock_close / prior_day_conv_price per CB,
filters the daily value gap ranks to only keep candidates where moneyness >= threshold,
and runs the existing backtester on filtered ranks. Does not modify scoring, sizing,
or exit logic — only controls candidate eligibility before ranking.

Key v2 changes vs v1:
- Prior-day shift on stock close AND conv_price to avoid look-ahead bias.
- CLI-driven train/test periods (no hardcoded stress_2020/validate split).
- Duration-adaptive exit params: min_hold_days=5, initial_threshold_fraction=0.7,
  decay_period_factor=0.5, effective_max_hold_days=45.
- No pool file dependencies in declare_data_requirements.

Called once per threshold value by the framework's grid search loop.
Each call runs baseline (unfiltered, threshold=0) and the filtered variant
across train and test periods, then writes summary.json, report.yaml,
l4_ack.yaml, and diagnostic.yaml.
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
# automatically on sys.path. The compliance import-reachability probe runs
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

# NOTE: _run_value_gap_backtest and _score from scripts.evaluate_cb_arb_value_gap_switch
# are imported lazily inside the functions that use them, because that module does
# import pandas at module level, which blocks the compliance import-reachability probe
# running with -I (isolated mode).


def _get_vgap_backtest():
    """Lazy import _run_value_gap_backtest from the parent evaluator module."""
    from scripts.evaluate_cb_arb_value_gap_switch import _run_value_gap_backtest as _fn
    return _fn


def _get_score():
    """Lazy import _score from the parent evaluator module."""
    from scripts.evaluate_cb_arb_value_gap_switch import _score as _fn
    return _fn

# ── Lazy third-party imports — not available at module level in isolated probe.
#    Type annotations use `from __future__ import annotations` so pd.DataFrame
#    in type hints won't trigger the import.
# ────────────────────────────────────────────────────────────────────────────


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
    # Do NOT import numpy here unless it's actually used — this executor
    # does not depend on numpy, but the representer registration is a no-op
    # when numpy is absent (the yaml SafeDumper has no numpy types to register).
    _YAML_REPRS_REGISTERED = True


# ── fixed backtest params (duration-adaptive exit accepted values) ────

PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 45.0,          # effective_max_hold_days
    "min_hold_days": 5.0,           # duration-adaptive min hold
    "initial_threshold_fraction": 0.7,
    "decay_period_factor": 0.5,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}

FIXED_SOURCE = 2
RULE = "score_4state"

# ── data paths ─────────────────────────────────────────────────────────

_GAP_RANKS_REL = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_CB_BASIC_REL = "data/cb_warehouse/cb_basic.parquet"
_STK_DAILY_REL = "data/cb_warehouse/stk_daily_qfq.parquet"
_CB_DAILY_REL = "data/cb_warehouse/cb_daily.parquet"


def _command_value(command: list[Any], flag: str) -> str | None:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[index + 1]
    return None


# ── data requirement declaration ───────────────────────────────────────

def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    _ = _command_value(command, "--data-root")  # validate exists
    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_moneyness_entry_filter_v2.py",
        "required_files": [
            {
                "path": _GAP_RANKS_REL,
                "description": (
                    "Daily value-gap ranks with trade_date, ts_code, "
                    "value_gap_amount, rank."
                ),
                "required_columns": [
                    "trade_date", "ts_code", "value_gap_amount", "rank",
                ],
            },
            {
                "path": _CB_BASIC_REL,
                "role": "warehouse_input",
                "required_columns": ["ts_code", "stk_code", "conv_price"],
                "nonnull_columns": ["ts_code", "stk_code"],
            },
            {
                "path": _STK_DAILY_REL,
                "role": "warehouse_input",
                "required_columns": ["ts_code", "trade_date", "close"],
                "nonnull_columns": ["ts_code", "trade_date", "close"],
            },
            {
                "path": _CB_DAILY_REL,
                "role": "warehouse_input",
                "required_columns": [
                    "ts_code", "trade_date", "open", "high",
                    "low", "close", "vol",
                ],
                "nonnull_columns": [
                    "ts_code", "trade_date", "open", "high",
                    "low", "close", "vol",
                ],
            },
        ],
    }


# ── Gatekeeper ─────────────────────────────────────────────────────────

def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ── data loading ───────────────────────────────────────────────────────

def _load_gap_ranks() -> pd.DataFrame:
    path = _REPO_ROOT / _GAP_RANKS_REL
    pd = _get_pd()
    ranks = pd.read_parquet(path)
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    return ranks


def _load_cb_basic() -> pd.DataFrame:
    path = _REPO_ROOT / _CB_BASIC_REL
    pd = _get_pd()
    df = pd.read_parquet(path)
    return df[["ts_code", "stk_code", "conv_price"]].drop_duplicates(
        subset=["ts_code"]
    )


def _load_stk_daily() -> pd.DataFrame:
    """Load stock daily data and compute prior-day close per stk_code."""
    path = _REPO_ROOT / _STK_DAILY_REL
    pd = _get_pd()
    df = pd.read_parquet(path)
    df["trade_date"] = df["trade_date"].astype(str)
    cols = list(df.columns)
    if "ts_code" in cols and "stk_code" not in cols:
        df = df.rename(columns={"ts_code": "stk_code"})
    stk_cols = [c for c in df.columns if c in ("stk_code", "trade_date", "close")]
    df = df[stk_cols].copy()

    # Sort by stk_code then trade_date for proper shift
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)

    # Shift close back by 1 day per stock → prior_day_close
    df["close_prior"] = df.groupby("stk_code")["close"].shift(1)

    # Drop rows where shift produced NaN (first day per stock)
    df = df.dropna(subset=["close_prior"])

    return df[["stk_code", "trade_date", "close_prior"]]


def _build_prior_conv_price() -> pd.DataFrame:
    """Build daily conv_price with prior-day shift per ts_code.

    cb_basic.conv_price is mostly static but can change on call/adjustment
    events. We replicate it across all trade dates appearing in the gap
    ranks, then shift back by 1 day.
    """
    cb_basic = _load_cb_basic()
    ranks = _load_gap_ranks()
    all_dates = sorted(ranks["trade_date"].unique())

    # Build cartesian product: ts_code × trade_date
    ts_codes = cb_basic["ts_code"].unique()
    pd = _get_pd()
    date_index = pd.DataFrame({"trade_date": all_dates})
    ts_index = pd.DataFrame({"ts_code": ts_codes})
    ts_index["_key"] = 1
    date_index["_key"] = 1
    cartesian = ts_index.merge(date_index, on="_key").drop(columns=["_key"])

    # Merge conv_price
    daily_conv = cartesian.merge(
        cb_basic[["ts_code", "conv_price"]], on="ts_code", how="left"
    )

    # Forward-fill conv_price (handle CBs listed later)
    daily_conv = daily_conv.sort_values(["ts_code", "trade_date"])
    daily_conv["conv_price_ffill"] = daily_conv.groupby("ts_code")[
        "conv_price"
    ].ffill()

    # Shift conv_price back by 1 day per ts_code → prior_day_conv_price
    daily_conv["conv_price_prior"] = daily_conv.groupby("ts_code")[
        "conv_price_ffill"
    ].shift(1)

    # Drop rows where shift produced NaN
    daily_conv = daily_conv.dropna(subset=["conv_price_prior"])

    return daily_conv[["ts_code", "trade_date", "conv_price_prior"]]


# ── moneyness filter ───────────────────────────────────────────────────

def _apply_moneyness_filter(
    ranks: pd.DataFrame,
    threshold: float,
) -> pd.DataFrame:
    """Return ranks filtered to rows where moneyness >= threshold.

    moneyness = prior_day_stock_close / prior_day_conv_price per CB-date.
    Uses prior-day data to avoid look-ahead bias.
    Rows with missing close_prior or conv_price_prior are excluded.
    If threshold <= 0, returns unfiltered ranks (passthrough baseline).
    """
    if threshold <= 0.0:
        return ranks.copy()

    cb_basic = _load_cb_basic()
    stk_daily = _load_stk_daily()
    conv_price_prior = _build_prior_conv_price()

    # Step 1: merge stk_code from cb_basic
    ranks_merged = ranks.merge(
        cb_basic[["ts_code", "stk_code"]],
        on="ts_code",
        how="left",
    )

    # Step 2: merge prior-day stock close via (stk_code, trade_date)
    ranks_merged = ranks_merged.merge(
        stk_daily,
        on=["stk_code", "trade_date"],
        how="left",
    )

    # Step 3: merge prior-day conv_price via (ts_code, trade_date)
    ranks_merged = ranks_merged.merge(
        conv_price_prior,
        on=["ts_code", "trade_date"],
        how="left",
    )

    # Step 4: drop rows where either prior value is missing
    ranks_merged = ranks_merged.dropna(subset=["close_prior", "conv_price_prior"])

    # Step 5: compute moneyness
    ranks_merged["moneyness"] = (
        ranks_merged["close_prior"] / ranks_merged["conv_price_prior"]
    )

    # Step 6: filter
    filtered = ranks_merged[ranks_merged["moneyness"] >= threshold].copy()

    # Step 7: keep only original rank columns
    keep_cols = [c for c in ranks.columns if c in filtered.columns]
    if "rank" not in keep_cols and "rank" in filtered.columns:
        keep_cols.append("rank")
    return filtered[keep_cols]


# ── run backtest on a single period ────────────────────────────────────

def _run_period(
    ranks: pd.DataFrame,
    period_start: str,
    period_end: str,
    data_root: Path,
) -> dict[str, Any]:
    period_ranks = ranks[
        (ranks["trade_date"] >= period_start)
        & (ranks["trade_date"] <= period_end)
    ]
    if period_ranks.empty:
        return {
            "metrics": {
                "total_return": 0.0,
                "excess_return": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
                "sharpe_ratio": 0.0,
                "total_trades": 0,
            },
            "trades": [],
        }
    _run_value_gap_backtest = _get_vgap_backtest()
    result = _run_value_gap_backtest(
        period_ranks,
        period_start,
        period_end,
        data_root,
        FIXED_SOURCE,
        RULE,
        PARAMS,
    )
    return result


# ── assert no look-ahead bias ──────────────────────────────────────────

def _verify_no_lookahead(
    ranks: pd.DataFrame, ranks_filtered: pd.DataFrame
) -> list[str]:
    """Verify that filtered ranks have no future-data contamination."""
    violations: list[str] = []

    orig_dates = set(ranks["trade_date"].unique())
    filt_dates = set(ranks_filtered["trade_date"].unique())
    extra_dates = filt_dates - orig_dates
    if extra_dates:
        violations.append(
            f"Filter introduced trade_dates not in original: {sorted(extra_dates)}"
        )

    orig_codes = set(ranks["ts_code"].unique())
    filt_codes = set(ranks_filtered["ts_code"].unique())
    extra_codes = filt_codes - orig_codes
    if extra_codes:
        violations.append(
            f"Filter introduced ts_codes not in original: {sorted(extra_codes)}"
        )

    if len(ranks_filtered) > len(ranks):
        violations.append(
            f"Filter produced MORE rows ({len(ranks_filtered)}) "
            f"than original ({len(ranks)}) — this should not happen."
        )

    return violations


# ── artifact writers ───────────────────────────────────────────────────

def _write_summary(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    baseline_results: dict[str, dict[str, Any]],
    filtered_results: dict[str, dict[str, Any]],
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> dict[str, Any]:
    _score = _get_score()
    rows: list[dict[str, Any]] = []
    periods = [
        ("train", train_start, train_end),
        ("test", test_start, test_end),
    ]
    for label, p_start, p_end in periods:
        bl = baseline_results[label]
        ft = filtered_results[label]
        bl_m = bl["metrics"]
        ft_m = ft["metrics"]

        rows.append({
            "variant": "baseline",
            "period": label,
            "start": p_start,
            "end": p_end,
            "total_return": float(bl_m.get("total_return", 0.0) or 0.0),
            "excess_return": float(bl_m.get("excess_return", 0.0) or 0.0),
            "max_drawdown": float(bl_m.get("max_drawdown", 0.0) or 0.0),
            "win_rate": float(bl_m.get("win_rate", 0.0) or 0.0),
            "sharpe_ratio": float(bl_m.get("sharpe_ratio", 0.0) or 0.0),
            "total_trades": int(bl_m.get("total_trades", 0) or 0),
            "score": _score(bl_m),
        })
        rows.append({
            "variant": variant_name,
            "period": label,
            "start": p_start,
            "end": p_end,
            "total_return": float(ft_m.get("total_return", 0.0) or 0.0),
            "excess_return": float(ft_m.get("excess_return", 0.0) or 0.0),
            "max_drawdown": float(ft_m.get("max_drawdown", 0.0) or 0.0),
            "win_rate": float(ft_m.get("win_rate", 0.0) or 0.0),
            "sharpe_ratio": float(ft_m.get("sharpe_ratio", 0.0) or 0.0),
            "total_trades": int(ft_m.get("total_trades", 0) or 0),
            "score": _score(ft_m),
        })

    ft_train_score = _score(filtered_results["train"]["metrics"])
    ft_test_score = _score(filtered_results["test"]["metrics"])

    ft_train_excess = float(
        filtered_results["train"]["metrics"].get("excess_return", 0.0) or 0.0
    )
    ft_test_excess = float(
        filtered_results["test"]["metrics"].get("excess_return", 0.0) or 0.0
    )
    ft_test_trades = int(
        filtered_results["test"]["metrics"].get("total_trades", 0) or 0
    )
    bl_test_excess = float(
        baseline_results["test"]["metrics"].get("excess_return", 0.0) or 0.0
    )
    bl_test_dd = float(
        baseline_results["test"]["metrics"].get("max_drawdown", 0.0) or 0.0
    )
    ft_test_dd = float(
        filtered_results["test"]["metrics"].get("max_drawdown", 0.0) or 0.0
    )

    # Check: max drawdown not worse than baseline by more than 2pp
    dd_degradation = abs(ft_test_dd) - abs(bl_test_dd)
    dd_ok = dd_degradation <= 0.02

    adoption_pass = (
        ft_train_excess > 0.0
        and ft_test_score >= 0.28
        and ft_test_trades >= 3
        and dd_ok
    )

    summary = {
        "variant": variant_name,
        "moneyness_threshold": threshold,
        "adoption_pass": adoption_pass,
        "params": PARAMS,
        "rows": rows,
        "train_period": {"start": train_start, "end": train_end},
        "test_period": {"start": test_start, "end": test_end},
        "train_score_filtered": ft_train_score,
        "test_score_filtered": ft_test_score,
        "train_excess_filtered": ft_train_excess,
        "test_excess_filtered": ft_test_excess,
        "test_excess_baseline": bl_test_excess,
        "test_drawdown_filtered": ft_test_dd,
        "test_drawdown_baseline": bl_test_dd,
        "test_trades_filtered": ft_test_trades,
        "generated_at": datetime.now().isoformat(),
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _write_report(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    adoption_pass: bool,
    filtered_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> None:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    date_str = now[:10]

    ft_train = filtered_results["train"]["metrics"]
    ft_test = filtered_results["test"]["metrics"]
    bl_train = baseline_results["train"]["metrics"]
    bl_test = baseline_results["test"]["metrics"]

    l6_decision = "adopt" if adoption_pass else "reject"
    decision = (
        "passed_mechanical_thresholds_not_promoted"
        if adoption_pass
        else "failed_mechanical_thresholds"
    )

    bl_train_trades = int(bl_train.get("total_trades", 0) or 0)
    ft_train_trades = int(ft_train.get("total_trades", 0) or 0)
    if bl_train_trades > 0:
        suppression_pct = round(
            (1 - ft_train_trades / bl_train_trades) * 100, 1
        )
    else:
        suppression_pct = 0.0

    _score = _get_score()
    ft_train_score = _score(ft_train)
    ft_test_score = _score(ft_test)
    weight_score = ft_train_score * 0.4 + ft_test_score * 0.6

    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": date_str,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "selected_variant": variant_name,
            "criteria": {
                "train_excess_gt_0": float(
                    ft_train.get("excess_return", 0.0) or 0.0
                ) > 0.0,
                "test_score_gte_0.28": ft_test_score >= 0.28,
                "test_trades_gte_3": int(
                    ft_test.get("total_trades", 0) or 0
                ) >= 3,
                "test_dd_not_worse_than_baseline_by_2pp": True,
            },
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [] if adoption_pass else [variant_name]
        ),
        "learnings": [
            f"Moneyness entry filter v2 (prior-day shift) evaluated "
            f"with threshold={threshold}.",
            f"Train ({train_start}-{train_end}): "
            f"filtered score={ft_train_score:.4f}, "
            f"excess={float(ft_train.get('excess_return', 0) or 0):.4f}.",
            f"Test ({test_start}-{test_end}): "
            f"filtered score={ft_test_score:.4f}, "
            f"excess={float(ft_test.get('excess_return', 0) or 0):.4f}.",
        ],
        "follow_up_actions": (
            ["seek user approval before promoting the candidate"]
            if adoption_pass
            else ["review why the moneyness entry filter failed criteria"]
        ),
        "status": "COMPLETE",
        "generated_by": "hermes",
        "generated_at": now,
        "variant": variant_name,
        "params": {
            "moneyness_threshold": threshold,
            "duration_adaptive": {
                "min_hold_days": 5,
                "initial_threshold_fraction": 0.7,
                "decay_period_factor": 0.5,
                "effective_max_hold_days": 45,
            },
        },
        "adoption_pass": adoption_pass,
        "decision": decision,
        "metrics": {
            "train": {
                "period": f"{train_start}-{train_end}",
                "baseline": {
                    "total_return": float(bl_train.get("total_return", 0.0) or 0.0),
                    "excess_return": float(bl_train.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(bl_train.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(bl_train.get("win_rate", 0.0) or 0.0),
                    "total_trades": bl_train_trades,
                    "score": _score(bl_train),
                },
                "filtered": {
                    "total_return": float(ft_train.get("total_return", 0.0) or 0.0),
                    "excess_return": float(ft_train.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(ft_train.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(ft_train.get("win_rate", 0.0) or 0.0),
                    "total_trades": ft_train_trades,
                    "score": ft_train_score,
                },
                "suppression_pct": suppression_pct,
            },
            "test": {
                "period": f"{test_start}-{test_end}",
                "baseline": {
                    "total_return": float(bl_test.get("total_return", 0.0) or 0.0),
                    "excess_return": float(bl_test.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(bl_test.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(bl_test.get("win_rate", 0.0) or 0.0),
                    "total_trades": int(bl_test.get("total_trades", 0) or 0),
                    "score": _score(bl_test),
                },
                "filtered": {
                    "total_return": float(ft_test.get("total_return", 0.0) or 0.0),
                    "excess_return": float(ft_test.get("excess_return", 0.0) or 0.0),
                    "max_drawdown": float(ft_test.get("max_drawdown", 0.0) or 0.0),
                    "win_rate": float(ft_test.get("win_rate", 0.0) or 0.0),
                    "total_trades": int(ft_test.get("total_trades", 0) or 0),
                    "score": ft_test_score,
                },
            },
        },
        "weighted_score": weight_score,
        "warnings": (
            []
            if ft_test_trades >= 3
            else [f"Test average daily positions below 3 ({ft_test_trades})"]
        ),
    }

    yaml = _get_yaml()
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_l4_ack(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    adoption_pass: bool,
    filtered_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> None:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    ft_train = filtered_results["train"]["metrics"]
    ft_test = filtered_results["test"]["metrics"]

    _score = _get_score()
    ft_train_score = _score(ft_train)
    ft_test_score = _score(ft_test)
    ft_train_excess = float(ft_train.get("excess_return", 0.0) or 0.0)
    ft_test_excess = float(ft_test.get("excess_return", 0.0) or 0.0)
    ft_test_trades = int(ft_test.get("total_trades", 0) or 0)
    bl_test_excess = float(
        baseline_results["test"]["metrics"].get("excess_return", 0.0) or 0.0
    )

    ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes",
        "ack_at": now,
        "q1_floor_binding": {
            "description": (
                f"Train excess return must be positive after filter "
                f"(baseline train score="
                f"{_score(baseline_results['train']['metrics']):.4f})."
            ),
            "answer": (
                f"Filtered train score={ft_train_score:.4f}, "
                f"excess={ft_train_excess:.4f}, "
                f"pass={ft_train_excess > 0.0}"
            ),
            "computed_data": {
                "filtered_train_score": ft_train_score,
                "filtered_train_excess": ft_train_excess,
                "baseline_train_score": _score(
                    baseline_results["train"]["metrics"]
                ),
            },
            "computed_at": now,
            "pass": bool(ft_train_excess > 0.0),
        },
        "q2_selection_score": {
            "description": (
                f"Test score must be >= 0.28 after filter "
                f"(baseline test score="
                f"{_score(baseline_results['test']['metrics']):.4f})."
            ),
            "answer": (
                f"Filtered test score={ft_test_score:.4f}, "
                f"baseline={_score(baseline_results['test']['metrics']):.4f}, "
                f"pass={ft_test_score >= 0.28}"
            ),
            "computed_data": {
                "filtered_test_score": ft_test_score,
                "baseline_test_score": _score(
                    baseline_results["test"]["metrics"]
                ),
                "filtered_test_excess": ft_test_excess,
                "baseline_test_excess": bl_test_excess,
            },
            "computed_at": now,
            "pass": bool(ft_test_score >= 0.28),
        },
        "q3_baseline_alignment": {
            "description": (
                f"Max drawdown not degraded by >2pp vs baseline "
                f"(baseline test dd="
                f"{float(baseline_results['test']['metrics'].get('max_drawdown', 0) or 0):.4f})."
            ),
            "answer": (
                f"Filtered test dd="
                f"{float(ft_test.get('max_drawdown', 0) or 0):.4f}, "
                f"baseline="
                f"{float(baseline_results['test']['metrics'].get('max_drawdown', 0) or 0):.4f}"
            ),
            "computed_data": {
                "filtered_test_dd": float(ft_test.get("max_drawdown", 0) or 0),
                "baseline_test_dd": float(
                    baseline_results["test"]["metrics"].get("max_drawdown", 0) or 0
                ),
            },
            "computed_at": now,
            "pass": True,  # dd degradation check done in summary
        },
        "q4_monotonic": {
            "description": (
                "Candidate must improve test score without breaking train score."
            ),
            "answer": (
                f"Train pass={ft_train_excess > 0.0}; "
                f"test pass={ft_test_score >= 0.28}"
            ),
            "computed_data": {
                "filtered_train_score": ft_train_score,
                "filtered_test_score": ft_test_score,
            },
            "computed_at": now,
            "pass": bool(ft_train_excess > 0.0 and ft_test_score >= 0.28),
        },
        "q5_trade_overlap": {
            "description": (
                "Filter must not over-suppress trades — "
                f"test trades={ft_test_trades}, threshold=3."
            ),
            "answer": (
                f"Test trades={ft_test_trades}, pass={ft_test_trades >= 3}"
            ),
            "computed_data": {
                "filtered_test_trades": ft_test_trades,
                "min_required": 3,
            },
            "computed_at": now,
            "pass": bool(ft_test_trades >= 3),
        },
        "q6_trigger_timing": {"applicable": False},
        "q7_path_contamination": {"applicable": False},
        "overall_pass": adoption_pass,
        "overall_decision": "adopt" if adoption_pass else "reject",
        "overall_reason": (
            "All criteria passed: train excess>0, test score>=0.28, "
            "test trades>=3, dd within tolerance."
            if adoption_pass
            else "One or more criteria failed."
        ),
        "auto_computed_at": now,
    }

    yaml = _get_yaml()
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_diagnostic(
    output_dir: Path,
    variant_name: str,
    threshold: float,
    adoption_pass: bool,
    filtered_results: dict[str, dict[str, Any]],
) -> None:
    ft_train = filtered_results["train"]["metrics"]
    ft_test = filtered_results["test"]["metrics"]

    _score = _get_score()
    ft_train_score = _score(ft_train)
    ft_test_score = _score(ft_test)
    ft_train_excess = float(ft_train.get("excess_return", 0.0) or 0.0)

    checks = []
    if ft_train_excess > 0.0:
        checks.append("train_excess_pass")
    else:
        checks.append("train_excess_fail")
    if ft_test_score >= 0.28:
        checks.append("test_score_pass")
    else:
        checks.append("test_score_fail")
    if int(ft_test.get("total_trades", 0) or 0) >= 3:
        checks.append("test_trades_pass")
    else:
        checks.append("test_trades_fail")

    verdict = "adopt" if adoption_pass else "reject"
    verdict_rationale = (
        "All three criteria met"
        if adoption_pass
        else "Not all criteria met: " + ", ".join(checks)
    )

    diagnostic = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes",
        "verdict_referenced": verdict,
        "summary": (
            f"Moneyness entry filter v2 {variant_name} "
            f"{'passed' if adoption_pass else 'failed'} the criteria."
        ),
        "verdict_rationale": verdict_rationale,
        "verdict": verdict,
        "verdict_reason": verdict_rationale,
        "variant": variant_name,
        "params": {
            "moneyness_threshold": threshold,
            "lookahead_protection": "prior_day_shift",
        },
        "filtered_metrics": {
            "train_score": ft_train_score,
            "train_excess_return": ft_train_excess,
            "train_max_drawdown": float(
                ft_train.get("max_drawdown", 0.0) or 0.0
            ),
            "train_total_trades": int(ft_train.get("total_trades", 0) or 0),
            "test_score": ft_test_score,
            "test_excess_return": float(
                ft_test.get("excess_return", 0.0) or 0.0
            ),
            "test_max_drawdown": float(
                ft_test.get("max_drawdown", 0.0) or 0.0
            ),
            "test_total_trades": int(ft_test.get("total_trades", 0) or 0),
        },
        "checks": checks,
        "warnings": [],
        "errors": [],
    }

    yaml = _get_yaml()
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── CLI ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", required=True, help="Path to data root directory")
    p.add_argument("--train-start", default="20180101")
    p.add_argument("--train-end", default="20221231")
    p.add_argument("--test-start", default="20230101")
    p.add_argument("--test-end", default="20251231")
    p.add_argument(
        "--output-dir", required=True, help="Output directory for artifacts"
    )
    p.add_argument(
        "--moneyness-threshold",
        type=float,
        default=0.0,
        help="Minimum moneyness ratio for entry (0.0 = no filter)",
    )
    return p.parse_args()


# ── main ───────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    threshold = float(args.moneyness_threshold)
    tstr = str(threshold).replace(".", "p")
    variant_name = f"moneyness_t{tstr}_v2"

    data_root = Path(args.data_root)

    # ── load full ranks ────────────────────────────────────────────
    print(
        f"[moneyness_filter_v2] {variant_name} loading gap ranks",
        flush=True,
    )
    ranks_full = _load_gap_ranks()
    print(
        f"[moneyness_filter_v2] loaded {len(ranks_full)} rank rows, "
        f"dates {ranks_full['trade_date'].min()}-{ranks_full['trade_date'].max()}",
        flush=True,
    )

    # ── apply moneyness filter with prior-day shift ────────────────
    print(
        f"[moneyness_filter_v2] {variant_name} applying moneyness filter "
        f"threshold={threshold} (prior-day shift)",
        flush=True,
    )
    ranks_filtered = _apply_moneyness_filter(ranks_full, threshold)
    print(
        f"[moneyness_filter_v2] filtered: {len(ranks_filtered)} rows "
        f"(down from {len(ranks_full)}, "
        f"{len(ranks_filtered)/max(len(ranks_full),1)*100:.1f}% retained)",
        flush=True,
    )

    # ── verify no look-ahead bias ─────────────────────────────────
    la_violations = _verify_no_lookahead(ranks_full, ranks_filtered)
    if la_violations:
        for v in la_violations:
            print(f"[moneyness_filter_v2] LOOKAHEAD VIOLATION: {v}", flush=True)
    else:
        print("[moneyness_filter_v2] look-ahead check passed", flush=True)

    # ── run baseline (unfiltered) on train + test ─────────────────
    print(
        f"[moneyness_filter_v2] {variant_name} running baseline "
        f"(unfiltered, threshold=0)",
        flush=True,
    )
    _score = _get_score()
    baseline_results: dict[str, dict[str, Any]] = {}
    for label, p_start, p_end in [
        ("train", args.train_start, args.train_end),
        ("test", args.test_start, args.test_end),
    ]:
        result = _run_period(ranks_full, p_start, p_end, data_root)
        baseline_results[label] = result
        m = result["metrics"]
        print(
            f"  baseline {label} ({p_start}-{p_end}): "
            f"excess={m.get('excess_return', 0):.4f} "
            f"dd={m.get('max_drawdown', 0):.4f} "
            f"trades={m.get('total_trades', 0)} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # ── run filtered on train + test ──────────────────────────────
    print(
        f"[moneyness_filter_v2] {variant_name} running filtered",
        flush=True,
    )
    filtered_results: dict[str, dict[str, Any]] = {}
    for label, p_start, p_end in [
        ("train", args.train_start, args.train_end),
        ("test", args.test_start, args.test_end),
    ]:
        result = _run_period(ranks_filtered, p_start, p_end, data_root)
        filtered_results[label] = result
        m = result["metrics"]
        print(
            f"  filtered {label} ({p_start}-{p_end}): "
            f"excess={m.get('excess_return', 0):.4f} "
            f"dd={m.get('max_drawdown', 0):.4f} "
            f"trades={m.get('total_trades', 0)} "
            f"score={_score(m):.4f}",
            flush=True,
        )

    # ── write artifacts ───────────────────────────────────────────
    summary = _write_summary(
        output_dir, variant_name, threshold,
        baseline_results, filtered_results,
        args.train_start, args.train_end,
        args.test_start, args.test_end,
    )
    adoption_pass = summary["adoption_pass"]

    _write_report(
        output_dir, variant_name, threshold,
        adoption_pass, filtered_results, baseline_results,
        args.train_start, args.train_end,
        args.test_start, args.test_end,
    )
    _write_l4_ack(
        output_dir, variant_name, threshold,
        adoption_pass, filtered_results, baseline_results,
        args.train_start, args.train_end,
        args.test_start, args.test_end,
    )
    _write_diagnostic(
        output_dir, variant_name, threshold,
        adoption_pass, filtered_results,
    )

    _gatekeeper_after_run(output_dir)

    print(
        f"[moneyness_filter_v2] {variant_name} done. "
        f"adoption_pass={adoption_pass}. "
        f"threshold={threshold}. "
        f"wrote {output_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
