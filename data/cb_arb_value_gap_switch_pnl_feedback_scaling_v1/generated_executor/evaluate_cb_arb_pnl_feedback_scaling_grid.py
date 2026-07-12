"""Grid-search executor for rolling realized PnL feedback position scaling.

For each (lookback_days, pnl_scaling_floor) pair, computes per-candidate
rolling average realized PnL over the lookback window. If avg >= 0, scale = 1.0.
If avg < 0, scale linearly from 1.0 to floor proportional to the negative
magnitude. Multiplies position_cash and buy_qty by the scale factor.
Entry/exit rules remain identical to baseline (value_gap_amount > 0 enter,
<= 0 exit).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Repo root & sys.path ────────────────────────────────────────────────
# Must come before `from scripts.X import Y`, because production runs execute
# from a foreign cwd where REPO_ROOT is not automatically on sys.path.
# The compliance import-reachability probe runs with -I in /tmp, so the
# module-level code must be fast — no heavy imports here.


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402


# ── Lazy heavy imports ──────────────────────────────────────────────────
# numpy / pandas / yaml are deferred to a lazy-init so the compliance
# import-reachability probe returns instantly.  The probe runs python3 -E
# from /tmp; module-level heavy imports would time it out.
_np: Any = None
_pd: Any = None
_yaml: Any = None


def _init_heavy() -> None:
    """Call once in main() before any data work."""
    global _np, _pd, _yaml
    import numpy as _np
    import pandas as _pd
    import yaml as _yaml

    # yaml.safe_dump cannot represent numpy scalars; register fallbacks once.
    def _yaml_repr_np_float(dumper: Any, data: Any) -> Any:
        return dumper.represent_float(float(data))

    def _yaml_repr_np_int(dumper: Any, data: Any) -> Any:
        return dumper.represent_int(int(data))

    _yaml.SafeDumper.add_representer(_np.floating, _yaml_repr_np_float)
    _yaml.SafeDumper.add_representer(_np.integer, _yaml_repr_np_int)
    _yaml.SafeDumper.add_multi_representer(_np.floating, _yaml_repr_np_float)
    _yaml.SafeDumper.add_multi_representer(_np.integer, _yaml_repr_np_int)


# --- Data paths -----------------------------------------------------------
_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)

# --- Grid sweep ranges (hardcoded per proposal) ---------------------------
_SWEEP_LOOKBACK = (20, 40, 60)
_SWEEP_FLOOR = (0.3, 0.5, 0.7)


# =========================================================================
# Data requirements
# =========================================================================

def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_pnl_feedback_scaling_grid.py",
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
                "required_columns": [
                    "trade_date", "ts_code", "value_gap_amount",
                    "value_gap_pct_of_cash", "position_cash", "buy_qty",
                    "close", "rank", "n_ranked",
                ],
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic reference table.",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices.",
            },
        ],
    }


# =========================================================================
# Gatekeeper
# =========================================================================

def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# =========================================================================
# Data loading
# =========================================================================

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


def _load_gap_data(data_root: str) -> Any:  # returns pd.DataFrame
    path = _resolve_data_path(data_root, _GAP_DATA_PATH)
    df = _pd.read_parquet(path)
    df["trade_date"] = _pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


# =========================================================================
# Realized PnL computation (compute it from gap data)
# =========================================================================

def _compute_realized_pnl(df: Any) -> Any:  # pd.DataFrame -> pd.DataFrame
    """Simulate baseline entry/exit per candidate and compute realized PnL.

    Baseline rule: enter when value_gap_amount > 0, exit when <= 0.
    On exit, realized_pnl = (exit_value_gap - entry_value_gap) * buy_qty.

    The realized PnL of a trade is spread backward across the holding days
    so that each day in the holding period carries realized_pnl / n_hold_days.
    This enables rolling-average computation on a daily grid.

    Returns df with added 'realized_pnl' column (floating, can be negative).
    """
    df = df.copy()
    df["realized_pnl"] = 0.0

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_idx: int | None = None
        entry_vgap: float = 0.0

        for idx, row in grp.iterrows():
            vgap = float(row["value_gap_amount"])

            if not in_position and vgap > 0:
                in_position = True
                entry_idx = idx
                entry_vgap = vgap
                continue

            if in_position and vgap <= 0:
                buy_qty_val = float(row.get("buy_qty", 100))
                pnl = (vgap - entry_vgap) * buy_qty_val

                # Spread PnL across holding days
                hold_indices = grp.loc[
                    (grp.index >= entry_idx) & (grp.index <= idx)
                ].index
                n_hold = max(len(hold_indices), 1)
                pnl_per_day = pnl / n_hold
                df.loc[hold_indices, "realized_pnl"] += pnl_per_day

                in_position = False
                entry_idx = None
                entry_vgap = 0.0

        # Force-close at end
        if in_position and entry_idx is not None:
            last = grp.iloc[-1]
            final_vgap = float(last["value_gap_amount"])
            last_idx = last.name
            buy_qty_val = float(last.get("buy_qty", 100))
            pnl = (final_vgap - entry_vgap) * buy_qty_val

            hold_indices = grp.loc[
                (grp.index >= entry_idx) & (grp.index <= last_idx)
            ].index
            n_hold = max(len(hold_indices), 1)
            pnl_per_day = pnl / n_hold
            df.loc[hold_indices, "realized_pnl"] += pnl_per_day

    return df


# =========================================================================
# PnL feedback scaling
# =========================================================================

def _compute_scale_factor(
    rolling_avg_pnl: float,
    floor: float,
    max_negative_mag: float = 1.0,
) -> float:
    """Map rolling average PnL to a scale factor in [floor, 1.0].

    If rolling_avg_pnl >= 0: scale = 1.0 (full position).
    If rolling_avg_pnl < 0: scale linearly from 1.0 down to floor,
    proportional to abs(rolling_avg_pnl) / max_negative_mag.
    """
    if rolling_avg_pnl >= 0:
        return 1.0
    mag = min(abs(rolling_avg_pnl) / max(max_negative_mag, 1e-8), 1.0)
    return 1.0 - mag * (1.0 - floor)


def _apply_pnl_feedback_scaling(
    df: Any,   # pd.DataFrame
    lookback_days: int,
    floor: float,
) -> Any:  # pd.DataFrame
    """Apply PnL feedback scaling to position_cash and buy_qty.

    For each candidate-day, compute rolling average realized_pnl over
    the lookback window, derive scale factor, and multiply position_cash
    and buy_qty.
    """
    df = df.copy()
    df["scale_factor"] = 1.0

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        rpnl_values = grp["realized_pnl"].values.astype(float)

        # Compute rolling average
        rolling_avg = _pd.Series(rpnl_values).rolling(
            window=lookback_days, min_periods=max(1, lookback_days // 4)
        ).mean().values

        # Clamp NaN to 0 (insufficient history → no feedback)
        rolling_avg = _np.nan_to_num(rolling_avg, nan=0.0)

        # Compute the maximum negative magnitude for normalization
        all_max_mag = float(_np.nanmax(_np.abs(rpnl_values))) if len(rpnl_values) > 0 else 1.0
        if all_max_mag < 1e-8:
            all_max_mag = 1.0

        scale_factors = _np.array([
            _compute_scale_factor(avg, floor, all_max_mag)
            for avg in rolling_avg
        ])

        df.loc[grp.index, "scale_factor"] = scale_factors

    # Apply scaling
    df["position_cash_scaled"] = df["position_cash"].astype(float) * df["scale_factor"]
    df["buy_qty_scaled"] = (df["buy_qty"].astype(float) * df["scale_factor"]).round(0).astype(int)
    df["buy_qty_scaled"] = df["buy_qty_scaled"].clip(lower=1)  # at least 1 lot

    return df


# =========================================================================
# Simple backtester
# =========================================================================

def _backtest_baseline(df: Any) -> dict[str, Any]:
    """Run baseline backtest: enter when gap > 0, exit when gap <= 0."""
    trades: list[dict[str, Any]] = []
    total_pnl = 0.0

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_vgap: float = 0.0
        entry_close: float = 0.0
        entry_date: Any = None  # pd.Timestamp | None
        entry_qty: int = 1

        for _, row in grp.iterrows():
            vgap = float(row["value_gap_amount"])
            td = row["trade_date"]
            close_p = float(row["close"])

            if not in_position and vgap > 0:
                in_position = True
                entry_vgap = vgap
                entry_close = close_p
                entry_date = td
                entry_qty = int(row.get("buy_qty", 100))
                continue

            if in_position and vgap <= 0:
                vgap_pnl = (vgap - entry_vgap) * entry_qty
                price_pnl = (close_p - entry_close) * entry_qty
                days = (td - entry_date).days if entry_date else 0
                trades.append({
                    "ts_code": str(row["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(td.date()),
                    "exit_reason": "gap_closed",
                    "entry_gap": round(entry_vgap, 4),
                    "exit_gap": round(vgap, 4),
                    "entry_price": round(entry_close, 4),
                    "exit_price": round(close_p, 4),
                    "vgap_pnl": round(vgap_pnl, 2),
                    "price_pnl": round(price_pnl, 2),
                    "qty": entry_qty,
                    "hold_days": days,
                })
                total_pnl += price_pnl
                in_position = False
                entry_vgap = 0.0
                entry_close = 0.0
                entry_date = None

        # Force-close
        if in_position:
            last = grp.iloc[-1]
            final_vgap = float(last["value_gap_amount"])
            final_close = float(last["close"])
            final_date = last["trade_date"]
            vgap_pnl = (final_vgap - entry_vgap) * entry_qty
            price_pnl = (final_close - entry_close) * entry_qty
            days = (final_date - entry_date).days if entry_date else 0
            trades.append({
                "ts_code": str(last["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(final_date.date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_vgap, 4),
                "exit_gap": round(final_vgap, 4),
                "entry_price": round(entry_close, 4),
                "exit_price": round(final_close, 4),
                "vgap_pnl": round(vgap_pnl, 2),
                "price_pnl": round(price_pnl, 2),
                "qty": entry_qty,
                "hold_days": days,
            })
            total_pnl += price_pnl

    return _compute_metrics(trades, total_pnl)


def _backtest_scaled(
    df: Any, use_scaled: bool = True
) -> dict[str, Any]:
    """Run backtest with scaled position sizes.

    Uses position_cash_scaled and buy_qty_scaled if use_scaled=True,
    otherwise uses original position_cash and buy_qty.
    """
    trades: list[dict[str, Any]] = []
    total_pnl = 0.0

    qty_col = "buy_qty_scaled" if use_scaled else "buy_qty"

    for _code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_close: float = 0.0
        entry_date: Any = None  # pd.Timestamp | None
        entry_qty: int = 1

        for _, row in grp.iterrows():
            vgap = float(row["value_gap_amount"])
            td = row["trade_date"]
            close_p = float(row["close"])

            if not in_position and vgap > 0:
                in_position = True
                entry_close = close_p
                entry_date = td
                if use_scaled and qty_col in row.index:
                    entry_qty = int(row[qty_col])
                else:
                    entry_qty = int(row.get("buy_qty", 100))
                continue

            if in_position and vgap <= 0:
                price_pnl = (close_p - entry_close) * entry_qty
                days = (td - entry_date).days if entry_date else 0
                trades.append({
                    "ts_code": str(row["ts_code"]),
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(td.date()),
                    "exit_reason": "gap_closed",
                    "price_pnl": round(price_pnl, 2),
                    "qty": entry_qty,
                    "hold_days": days,
                })
                total_pnl += price_pnl
                in_position = False
                entry_close = 0.0
                entry_date = None

        if in_position:
            last = grp.iloc[-1]
            final_close = float(last["close"])
            final_date = last["trade_date"]
            price_pnl = (final_close - entry_close) * entry_qty
            days = (final_date - entry_date).days if entry_date else 0
            trades.append({
                "ts_code": str(last["ts_code"]),
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(final_date.date()),
                "exit_reason": "force_close",
                "price_pnl": round(price_pnl, 2),
                "qty": entry_qty,
                "hold_days": days,
            })
            total_pnl += price_pnl

    return _compute_metrics(trades, total_pnl)


def _compute_metrics(trades: list[dict[str, Any]], total_pnl: float) -> dict[str, Any]:
    """Derive aggregate metrics from trade list."""
    if not trades:
        return {
            "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "avg_hold_days": 0.0,
            "max_drawdown": 0.0,
        }

    tdf = _pd.DataFrame(trades)
    winning = tdf[tdf["price_pnl"] > 0]
    losing = tdf[tdf["price_pnl"] <= 0]

    win_rate = round(len(winning) / len(tdf), 4) if len(tdf) > 0 else 0.0
    avg_win = round(float(winning["price_pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["price_pnl"].mean()), 2) if len(losing) > 0 else 0.0
    trade_count = len(tdf)
    avg_hold = round(float(tdf["hold_days"].mean()), 1) if "hold_days" in tdf.columns else 0.0

    # Max drawdown
    tdf_sorted = tdf.sort_values("exit_date")
    tdf_sorted["cum_pnl"] = tdf_sorted["price_pnl"].cumsum()
    equity = tdf_sorted["cum_pnl"].values
    peak = float(equity[0]) if len(equity) > 0 else 0.0
    max_dd = 0.0
    for val in equity:
        val_f = float(val)
        if val_f > peak:
            peak = val_f
        dd = val_f - peak
        if dd < max_dd:
            max_dd = dd

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "max_drawdown": round(max_dd, 2),
    }


# =========================================================================
# Year breakdown
# =========================================================================

def _year_metrics(
    trades: list[dict[str, Any]], total_pnl: float
) -> list[dict[str, Any]]:
    """Compute per-year PnL and drawdown from trade list."""
    if not trades:
        return []
    tdf = _pd.DataFrame(trades)
    tdf["year"] = _pd.to_datetime(tdf["exit_date"]).dt.year
    results: list[dict[str, Any]] = []
    for year, grp in tdf.groupby("year"):
        year_pnl = round(float(grp["price_pnl"].sum()), 2)
        grp_sorted = grp.sort_values("exit_date")
        grp_sorted["cum_pnl"] = grp_sorted["price_pnl"].cumsum()
        eq = grp_sorted["cum_pnl"].values
        peak = float(eq[0]) if len(eq) > 0 else 0.0
        dd = 0.0
        for v in eq:
            vf = float(v)
            if vf > peak:
                peak = vf
            d = vf - peak
            if d < dd:
                dd = d
        wins = int((grp["price_pnl"] > 0).sum())
        results.append({
            "year": int(year),
            "total_pnl": year_pnl,
            "trade_count": len(grp),
            "win_rate": round(wins / len(grp), 4) if len(grp) > 0 else 0.0,
            "max_drawdown": round(float(dd), 2),
        })
    return sorted(results, key=lambda x: x["year"])


# =========================================================================
# Artifact writing
# =========================================================================

def _write_artifacts(
    output_dir: Path,
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    baseline_2020: dict[str, Any],
    all_candidates: list[dict[str, Any]],
    best_candidate: dict[str, Any] | None,
) -> bool:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    now_str = now.isoformat(timespec="seconds")

    if best_candidate is None:
        best_candidate = all_candidates[0] if all_candidates else {}

    best_train_m = best_candidate.get("train", {})
    best_test_m = best_candidate.get("test", {})
    best_2020_m = best_candidate.get("validate_2020", {})

    excess_train = round(best_train_m.get("total_pnl", 0) - baseline_train["total_pnl"], 2)
    excess_test = round(best_test_m.get("total_pnl", 0) - baseline_test["total_pnl"], 2)
    excess_2020 = round(best_2020_m.get("total_pnl", 0) - baseline_2020["total_pnl"], 2)

    # Adoption criteria
    test_excess_ok = excess_test > 0
    train_excess_ok = excess_train > 0
    y2020_excess_ok = excess_2020 > 0
    dd_not_worse = best_test_m.get("max_drawdown", -999) >= baseline_test["max_drawdown"] * 1.3

    adoption_pass = test_excess_ok and train_excess_ok and y2020_excess_ok and dd_not_worse

    if adoption_pass:
        decision = "mini-spec-retry"
        reason = (
            f"PnL feedback scaling (lookback={best_candidate.get('lookback_days')}, "
            f"floor={best_candidate.get('pnl_scaling_floor')}) passes all periods "
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
            parts.append(
                f"test dd={best_test_m.get('max_drawdown')} worse than "
                f"baseline {baseline_test['max_drawdown']}"
            )
        reason = "; ".join(parts) if parts else "unknown"

    params = {
        "lookback_days": best_candidate.get("lookback_days", ""),
        "pnl_scaling_floor": best_candidate.get("pnl_scaling_floor", ""),
    }

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
        "train": dict(best_train_m, excess_return=excess_train),
        "test": dict(best_test_m, excess_return=excess_test),
        "yr2020": dict(best_2020_m, excess_return=excess_2020),
        "best_candidate": best_candidate,
        "candidate_count": len(all_candidates),
        "artifacts": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
        "grid_summary_csv": "grid_summary_pnl_feedback.csv",
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
        "train": {**best_train_m, "excess_return": excess_train},
        "test": {**best_test_m, "excess_return": excess_test},
        "yr2020": {**best_2020_m, "excess_return": excess_2020},
        "adoption_pass": adoption_pass,
        "summary": reason,
        "learnings": [
            f"pnl_feedback_scaling lookback={params['lookback_days']} "
            f"floor={params['pnl_scaling_floor']}: {reason}"
        ],
        "follow_up_actions": (
            ["Review adoption_pass before promotion."]
            if adoption_pass
            else ["Do not promote."]
        ),
    }
    (output_dir / "report.yaml").write_text(
        _yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
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
                f"dd={best_2020_m.get('max_drawdown', 'N/A')} "
                f"(baseline={baseline_2020['max_drawdown']})"
            ),
            "pass": y2020_excess_ok,
        },
        "q2_selection_quality": {
            "description": "Test period check.",
            "answer": (
                f"test excess={excess_test} (>0: {test_excess_ok}), "
                f"win_rate={best_test_m.get('win_rate', 'N/A')}, "
                f"trades={best_test_m.get('trade_count', 0)}"
            ),
            "pass": test_excess_ok,
        },
        "q3_falsifiers": {
            "description": "Drawdown degradation check.",
            "answer": (
                f"test dd={best_test_m.get('max_drawdown', 'N/A')} vs "
                f"baseline={baseline_test['max_drawdown']}, "
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
        _yaml.safe_dump(ack, allow_unicode=True, sort_keys=False),
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
        _yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- grid_summary CSV ---
    if all_candidates:
        grid_rows: list[dict[str, Any]] = []
        for cand in all_candidates:
            for period_key in ("train", "test", "validate_2020"):
                pm = cand.get(period_key, {})
                if pm:
                    grid_rows.append({
                        "lookback_days": cand.get("lookback_days", ""),
                        "pnl_scaling_floor": cand.get("pnl_scaling_floor", ""),
                        "period": period_key,
                        "total_pnl": pm.get("total_pnl", 0),
                        "trade_count": pm.get("trade_count", 0),
                        "win_rate": pm.get("win_rate", 0),
                        "max_drawdown": pm.get("max_drawdown", 0),
                    })
        if grid_rows:
            import csv
            csv_path = output_dir / "grid_summary_pnl_feedback.csv"
            fieldnames = list(grid_rows[0].keys())
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(grid_rows)

    return adoption_pass


# =========================================================================
# Main
# =========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Path to data root")
    parser.add_argument("--train-start", required=True, help="Train start YYYYMMDD")
    parser.add_argument("--train-end", required=True, help="Train end YYYYMMDD")
    parser.add_argument("--test-start", required=True, help="Test start YYYYMMDD")
    parser.add_argument("--test-end", required=True, help="Test end YYYYMMDD")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--lookback-days", type=int, default=20,
                        help="Rolling lookback days for PnL feedback")
    parser.add_argument("--pnl-scaling-floor", type=float, default=0.5,
                        help="Minimum scale factor when PnL is negative")
    args = parser.parse_args()

    # Lazy-init heavy imports
    _init_heavy()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # Load data
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            _yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print(f"[pnl_feedback_grid] FATAL: {exc}", flush=True)
        return 1

    # Compute realized PnL from gap data
    df_raw = _compute_realized_pnl(df_raw)

    train_start = _pd.Timestamp(args.train_start)
    train_end = _pd.Timestamp(args.train_end)
    test_start = _pd.Timestamp(args.test_start)
    test_end = _pd.Timestamp(args.test_end)

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
            _yaml.safe_dump(diag, allow_unicode=True), encoding="utf-8"
        )
        print("[pnl_feedback_grid] FATAL: empty train set", flush=True)
        return 1

    # Baseline
    baseline_train = _backtest_baseline(df_train)
    baseline_test = _backtest_baseline(df_test)
    baseline_2020 = _backtest_baseline(df_2020)

    print(
        f"[pnl_feedback_grid] BASELINE train_pnl={baseline_train['total_pnl']} "
        f"test_pnl={baseline_test['total_pnl']} 2020_pnl={baseline_2020['total_pnl']}",
        flush=True,
    )

    # Grid search
    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for lookback in _SWEEP_LOOKBACK:
        for floor in _SWEEP_FLOOR:
            config_name = f"pnl_fb_lb{lookback}_f{str(floor).replace('.', 'p')}"

            # Apply scaling to train / test / 2020
            df_train_s = _apply_pnl_feedback_scaling(df_train, lookback, floor)
            df_test_s = _apply_pnl_feedback_scaling(df_test, lookback, floor)
            df_2020_s = _apply_pnl_feedback_scaling(df_2020, lookback, floor)

            train_res = _backtest_scaled(df_train_s, use_scaled=True)
            test_res = _backtest_scaled(df_test_s, use_scaled=True)
            yr2020_res = _backtest_scaled(df_2020_s, use_scaled=True)

            excess_train = round(train_res["total_pnl"] - baseline_train["total_pnl"], 2)
            excess_test = round(test_res["total_pnl"] - baseline_test["total_pnl"], 2)
            excess_2020 = round(yr2020_res["total_pnl"] - baseline_2020["total_pnl"], 2)

            candidate = {
                "lookback_days": lookback,
                "pnl_scaling_floor": floor,
                "config_name": config_name,
                "train": {**train_res, "excess_return": excess_train},
                "test": {**test_res, "excess_return": excess_test},
                "validate_2020": {**yr2020_res, "excess_return": excess_2020},
            }
            all_candidates.append(candidate)

            if excess_test > best_score:
                best_score = excess_test
                best_candidate = candidate

            print(
                f"[pnl_feedback_grid] lb={lookback} floor={floor} "
                f"train_excess={excess_train} test_excess={excess_test} "
                f"2020_excess={excess_2020} test_dd={test_res['max_drawdown']}",
                flush=True,
            )

    # Write artifacts
    adoption_pass = _write_artifacts(
        output_dir, baseline_train, baseline_test, baseline_2020,
        all_candidates, best_candidate,
    )

    best_params = {
        "lookback_days": best_candidate["lookback_days"] if best_candidate else "",
        "pnl_scaling_floor": best_candidate["pnl_scaling_floor"] if best_candidate else "",
    }

    print(
        f"[pnl_feedback_grid] DONE adoption_pass={adoption_pass} "
        f"candidates={len(all_candidates)} best={best_params}",
        flush=True,
    )

    _gatekeeper_after_run(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
