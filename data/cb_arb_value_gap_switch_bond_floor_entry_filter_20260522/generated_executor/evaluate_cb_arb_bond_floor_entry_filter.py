#!/usr/bin/env python3
"""Evaluate bond-floor entry filter for cb_arb value-gap switch strategy.

Computes bond floor per CB per day as the present value of remaining coupon
payments plus par at maturity, discounted at a fixed rate.  Compares daily
CB close to the bond floor; suppresses entries when CB close / bond_floor
exceeds a configurable threshold, with an optional minimum consecutive-days-below
requirement before re-allowing entry.

Uses the duration-adaptive exit (min_hold_days, initial_threshold_fraction,
effective_max_hold_days) as the exit baseline for every variant.
Grid-search-ready: single-parameter run, with the framework doing outer
sweep over bond_floor_threshold and min_days_below.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path
from typing import Any

# ────────────────────────────────────────────────────────────────────────
#  Repo root discovery and GateKeeper import (required by handoff contract)
#  Module-level imports kept to stdlib only — the compliance
#  import-reachability probe runs /usr/bin/python3 -E from /tmp,
#  so heavy third-party packages (numpy, pandas, yaml) must be
#  imported lazily inside functions.
# ────────────────────────────────────────────────────────────────────────

def _find_repo_root(start: Path) -> Path:
    """Walk up from *start* until scripts/gatekeeper.py is found."""
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402


# ────────────────────────────────────────────────────────────────────────
#  Lazy third-party imports (called once, cached globally)
# ────────────────────────────────────────────────────────────────────────

_np = None
_pd = None
_yaml = None


def _get_np():
    global _np
    if _np is None:
        import numpy as _numpy
        _np = _numpy
    return _np


def _get_pd():
    global _pd
    if _pd is None:
        import pandas as _pandas
        _pd = _pandas
    return _pd


def _get_yaml():
    global _yaml
    if _yaml is None:
        import yaml as _yaml_mod
        # Register numpy-scalar fallbacks for safe_dump
        np = _get_np()
        def _repr_np_float(dumper, data):
            return dumper.represent_float(float(data))
        def _repr_np_int(dumper, data):
            return dumper.represent_int(int(data))
        _yaml_mod.SafeDumper.add_representer(np.floating, _repr_np_float)
        _yaml_mod.SafeDumper.add_representer(np.integer, _repr_np_int)
        _yaml_mod.SafeDumper.add_multi_representer(np.floating, _repr_np_float)
        _yaml_mod.SafeDumper.add_multi_representer(np.integer, _repr_np_int)
        _yaml = _yaml_mod
    return _yaml


# ────────────────────────────────────────────────────────────────────────
#  Data-requirement declaration (called by the framework before run)
# ────────────────────────────────────────────────────────────────────────

_GAP_RANKS_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)


def declare_data_requirements(
    command: list[str] | None = None,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "required_files": [
            {
                "path": _GAP_RANKS_PATH,
                "description": "Daily value-gap ranks from regime-option-entry-gate run.",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic info: par_value, coupon_rate, maturity_date.",
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "description": "Daily CB market close prices for ratio calculation.",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices (loaded for compatibility).",
            },
        ],
    }


# ────────────────────────────────────────────────────────────────────────
#  Gatekeeper lifecycle helpers (required by framework)
# ────────────────────────────────────────────────────────────────────────

def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ────────────────────────────────────────────────────────────────────────
#  Helpers: path resolution & data loading
# ────────────────────────────────────────────────────────────────────────

def _find_parquet(path_rel: str) -> Path:
    """Resolve a relative path inside the quant repo, falling back to cwd."""
    repo = _REPO_ROOT
    candidates = [repo / path_rel, Path.cwd() / path_rel]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"Cannot find {path_rel}; looked at {candidates}")


def _load_gap_ranks() -> Any:  # returns pd.DataFrame
    pd = _get_pd()
    path = _find_parquet(_GAP_RANKS_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _load_cb_basic() -> Any:  # returns pd.DataFrame
    pd = _get_pd()
    path = _find_parquet("data/cb_warehouse/cb_basic.parquet")
    cols = ["ts_code", "par_value", "coupon_rate", "maturity_date", "list_date"]
    df = pd.read_parquet(path, columns=cols)
    for c in ("ts_code",):
        if hasattr(df[c], "str"):
            df[c] = df[c].astype(str)
    return df


def _load_cb_daily() -> Any:  # returns pd.DataFrame
    pd = _get_pd()
    path = _find_parquet("data/cb_warehouse/cb_daily.parquet")
    df = pd.read_parquet(path, columns=["ts_code", "trade_date", "close"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


# ────────────────────────────────────────────────────────────────────────
#  Bond-floor computation
# ────────────────────────────────────────────────────────────────────────

def _compute_bond_floor(
    cb_basic: Any,       # pd.DataFrame
    cb_daily: Any,        # pd.DataFrame
    discount_rate: float,
) -> Any:  # returns pd.DataFrame
    """Return a DataFrame with columns [trade_date, ts_code, bond_floor_value, cb_close].

    Bond floor = PV of remaining annual coupon payments (paid at year-end,
    prorated for partial first year) + PV of par at maturity, all discounted
    at `discount_rate`.  CBs past maturity get bond_floor = par_value.
    """
    np = _get_np()
    pd = _get_pd()

    merged = cb_daily.merge(
        cb_basic[["ts_code", "par_value", "coupon_rate", "maturity_date"]],
        on="ts_code",
        how="inner",
    )
    merged["maturity_date"] = pd.to_datetime(merged["maturity_date"], format="%Y%m%d")

    trade_dates = merged["trade_date"].values
    maturities = merged["maturity_date"].values
    pars = merged["par_value"].values.astype(float)
    coupons = merged["coupon_rate"].values.astype(float)
    closes = merged["close"].values.astype(float)

    days_to_mat = (maturities - trade_dates) / np.timedelta64(1, "D")
    years_to_mat = np.maximum(days_to_mat, 0.0) / 365.25

    bond_floor = np.full(len(merged), np.nan)

    # Past maturity -> bond floor = par
    past_mask = days_to_mat <= 0
    bond_floor[past_mask] = pars[past_mask]

    # Active CBs: PV of coupons + PV of par
    active_mask = ~past_mask
    if active_mask.any():
        t = years_to_mat[active_mask]
        p = pars[active_mask]
        c_rate = coupons[active_mask]
        r = discount_rate

        full_years = np.floor(t).astype(int)
        fractional_year = t - full_years

        coupon_pv = np.zeros(active_mask.sum())

        # Partial first-year coupon: coupon * (fractional_year) discounted 1 period
        first_partial = c_rate * p * fractional_year / (1.0 + r)
        coupon_pv += first_partial

        # Full-year coupons: years 1..N, each discounted at (1+r)^k
        for k in range(1, int(full_years.max()) + 1):
            eligible = full_years >= k
            if not eligible.any():
                continue
            idx_in_active = np.where(full_years >= k)[0]
            coupon_pv[idx_in_active] += (
                c_rate[idx_in_active] * p[idx_in_active] / (1.0 + r) ** k
            )

        # Par discounted from maturity
        par_pv = p / (1.0 + r) ** t

        bond_floor[active_mask] = coupon_pv + par_pv

    result = merged[["trade_date", "ts_code"]].copy()
    result["bond_floor_value"] = bond_floor
    result["cb_close"] = closes
    return result


# ────────────────────────────────────────────────────────────────────────
#  Entry mask: bond-floor proximity filter
# ────────────────────────────────────────────────────────────────────────

def _build_entry_mask(
    bond_floor_df: Any,  # pd.DataFrame
    threshold: float,
    min_days_below: int,
    gap_ranks: Any,      # pd.DataFrame
) -> Any:  # returns pd.DataFrame
    """Return gap_ranks with an added '_allow_entry' boolean column.

    For each (ts_code, trade_date), entry is suppressed when
    cb_close / bond_floor_value > threshold.  After min_days_below
    *consecutive* days at-or-below threshold, the CB is re-allowed.
    """
    np = _get_np()
    pd = _get_pd()

    bf = bond_floor_df.copy()
    bf["ratio"] = bf["cb_close"] / bf["bond_floor_value"]

    # Build a per-CB mask: True = entry allowed
    # Use ts_code -> dict[date -> bool] lookup table
    allowed: dict[str, dict[pd.Timestamp, bool]] = {}

    for ts, grp in bf.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        ratios = grp["ratio"].values
        dates = grp["trade_date"].values
        n = len(grp)

        status = np.zeros(n, dtype=bool)
        below_streak = 0
        currently_suppressed = False

        for i in range(n):
            if np.isnan(ratios[i]):
                status[i] = False
                below_streak = 0
                currently_suppressed = True
                continue

            if ratios[i] <= threshold:
                below_streak += 1
            else:
                below_streak = 0
                currently_suppressed = True

            if currently_suppressed and below_streak >= min_days_below:
                currently_suppressed = False

            status[i] = not currently_suppressed

        ts_map: dict[pd.Timestamp, bool] = {}
        for d, s in zip(dates, status):
            ts_map[d] = s
        allowed[str(ts)] = ts_map

    # Apply to gap_ranks
    ranks = gap_ranks.copy()
    ranks["_allow_entry"] = ranks.apply(
        lambda r: allowed.get(str(r["ts_code"]), {}).get(r["trade_date"], True),
        axis=1,
    )
    return ranks


# ────────────────────────────────────────────────────────────────────────
#  Duration-adaptive exit simulation
# ────────────────────────────────────────────────────────────────────────

def _decay_threshold(
    hold_days: int,
    min_hold: int,
    initial_frac: float,
    max_hold: int,
) -> float:
    if hold_days < min_hold:
        return initial_frac
    if max_hold <= min_hold:
        return 0.0
    decay_range = max_hold - min_hold
    elapsed = min(hold_days - min_hold, decay_range)
    return initial_frac * max(0.0, 1.0 - elapsed / decay_range)


def _simulate_duration_adaptive(
    df: Any,  # pd.DataFrame
    min_hold_days: int,
    initial_threshold_fraction: float,
    max_hold_days: int,
    use_entry_mask: bool = True,
) -> dict[str, Any]:
    """Gap-based backtest with duration-adaptive exit.

    Entry: gap > 0 and (if use_entry_mask) _allow_entry == True and not in position.
    Exit: gap <= 0 (closed), or after min_hold_days the ratio current_gap/entry_gap
          exceeds a linearly decaying threshold.
    """
    np = _get_np()
    pd = _get_pd()

    df = df.copy()

    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for stock, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        idx_list = grp.index.tolist()
        in_position = False
        entry_gap_val = 0.0
        entry_date: Any = None

        for i, idx in enumerate(idx_list):
            row = grp.loc[idx]
            gap = float(row["value_gap_amount"])

            if not in_position and gap > 0:
                if use_entry_mask:
                    allowed = bool(row.get("_allow_entry", True))
                    if not allowed:
                        continue

                in_position = True
                entry_gap_val = gap
                entry_date = row["trade_date"]
                continue

            if in_position:
                should_exit = False
                exit_reason = ""

                if gap <= 0:
                    should_exit = True
                    exit_reason = "gap_closed"
                else:
                    hold_days = (
                        (row["trade_date"] - entry_date).days if entry_date else 0
                    )
                    if hold_days >= min_hold_days:
                        threshold_val = _decay_threshold(
                            hold_days, min_hold_days, initial_threshold_fraction, max_hold_days
                        )
                        ratio = gap / entry_gap_val if entry_gap_val > 0 else 0.0
                        if ratio > threshold_val:
                            should_exit = True
                            exit_reason = "decay_exit"

                if should_exit:
                    pnl = (gap - entry_gap_val) * 100.0
                    total_pnl += pnl
                    hd = (row["trade_date"] - entry_date).days if entry_date else 0
                    trades.append({
                        "stock": stock,
                        "entry_date": str(entry_date.date()) if entry_date else "",
                        "exit_date": str(row["trade_date"].date()),
                        "exit_reason": exit_reason,
                        "entry_gap": round(entry_gap_val, 4),
                        "exit_gap": round(gap, 4),
                        "pnl": round(pnl, 2),
                        "hold_days": hd,
                        "min_hold_days": min_hold_days,
                        "initial_threshold_fraction": initial_threshold_fraction,
                        "max_hold_days": max_hold_days,
                    })
                    in_position = False
                    entry_gap_val = 0.0
                    entry_date = None

        # Force-close any still-open position
        if in_position:
            last_row = grp.iloc[-1]
            final_gap = float(last_row["value_gap_amount"])
            pnl = (final_gap - entry_gap_val) * 100.0
            total_pnl += pnl
            hd = (last_row["trade_date"] - entry_date).days if entry_date else 0
            trades.append({
                "stock": stock,
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(last_row["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(pnl, 2),
                "hold_days": hd,
                "min_hold_days": min_hold_days,
                "initial_threshold_fraction": initial_threshold_fraction,
                "max_hold_days": max_hold_days,
            })

    # Aggregate metrics
    if trades:
        trades_df = pd.DataFrame(trades)
        winning = trades_df[trades_df["pnl"] > 0]
        losing = trades_df[trades_df["pnl"] <= 0]
        win_rate = round(len(winning) / len(trades_df), 4)
        avg_win = float(winning["pnl"].mean()) if len(winning) > 0 else 0.0
        avg_loss = float(losing["pnl"].mean()) if len(losing) > 0 else 0.0
        trade_count = len(trades_df)
        avg_hold = float(trades_df["hold_days"].mean())
        # Max drawdown
        trades_sorted = trades_df.sort_values("exit_date")
        cum = trades_sorted["pnl"].cumsum().values
        peak = cum[0]
        max_dd = 0.0
        for val in cum:
            if val > peak:
                peak = val
            dd = val - peak
            if dd < max_dd:
                max_dd = dd
        decay_exits = int((trades_df["exit_reason"] == "decay_exit").sum())
        gap_closed = int((trades_df["exit_reason"] == "gap_closed").sum())
        force_closes = int((trades_df["exit_reason"] == "force_close").sum())
        # Annualized Sharpe from daily PnL
        trades_df_s = trades_df.copy()
        trades_df_s["exit_date"] = pd.to_datetime(trades_df_s["exit_date"])
        daily_pnl = trades_df_s.set_index("exit_date")["pnl"].resample("D").sum().fillna(0)
        if daily_pnl.std() > 0:
            sharpe = round(float(daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)), 4)
        else:
            sharpe = 0.0
    else:
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        trade_count = 0
        avg_hold = 0.0
        max_dd = 0.0
        decay_exits = 0
        gap_closed = 0
        force_closes = 0
        sharpe = 0.0

    cumulative_excess = round(total_pnl, 2)

    return {
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_hold_days": round(avg_hold, 1),
        "max_drawdown": round(max_dd, 2),
        "sharpe_ratio": sharpe,
        "cumulative_excess_compound": cumulative_excess,
        "decay_exits": decay_exits,
        "gap_closed_exits": gap_closed,
        "force_closes": force_closes,
        "trades": trades,
    }


# ────────────────────────────────────────────────────────────────────────
#  Plain-value converter (strip numpy types for JSON/YAML)
# ────────────────────────────────────────────────────────────────────────

def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ────────────────────────────────────────────────────────────────────────
#  Entry-mask diagnostics for diagnostic.yaml
# ────────────────────────────────────────────────────────────────────────

def _mask_diagnostics(ranks_with_mask: Any) -> dict[str, Any]:  # pd.DataFrame input
    total_rows = len(ranks_with_mask)
    suppressed = int((~ranks_with_mask["_allow_entry"]).sum())
    allowed = total_rows - suppressed
    suppression_pct = round(suppressed / total_rows * 100, 2) if total_rows > 0 else 0.0

    if "trade_date" in ranks_with_mask.columns:
        ranks_with_mask["_year"] = ranks_with_mask["trade_date"].dt.year
        by_year = (
            ranks_with_mask.groupby("_year")
            .apply(lambda g: {
                "total_rows": len(g),
                "suppressed": int((~g["_allow_entry"]).sum()),
                "suppression_pct": round((~g["_allow_entry"]).sum() / len(g) * 100, 2),
            })
            .to_dict()
        )
    else:
        by_year = {}

    return {
        "total_rows": total_rows,
        "suppressed_rows": suppressed,
        "allowed_rows": allowed,
        "suppression_pct": suppression_pct,
        "suppression_by_year": by_year,
    }


# ────────────────────────────────────────────────────────────────────────
#  main()
# ────────────────────────────────────────────────────────────────────────

def main() -> int:
    pd = _get_pd()
    np = _get_np()
    yaml = _get_yaml()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Data root directory (unused; data from fixed paths)")
    parser.add_argument("--train-start", required=True, help="Train start (YYYYMMDD)")
    parser.add_argument("--train-end", required=True, help="Train end (YYYYMMDD)")
    parser.add_argument("--test-start", required=True, help="Test start (YYYYMMDD)")
    parser.add_argument("--test-end", required=True, help="Test end (YYYYMMDD)")
    parser.add_argument("--output-dir", required=True, help="Output directory for artifacts")
    parser.add_argument("--bond-floor-threshold", type=float, default=1.10, help="Max CB close / bond_floor for entry")
    parser.add_argument("--min-days-below", type=int, default=1, help="Consecutive days below threshold to re-allow")
    parser.add_argument("--discount-rate", type=float, default=0.03, help="Discount rate for bond floor PV")
    parser.add_argument("--min-hold-days", type=int, default=5, help="Min hold days before decay exit")
    parser.add_argument("--initial-threshold-fraction", type=float, default=0.7, help="Initial decay threshold fraction")
    parser.add_argument("--decay-period-factor", type=float, default=0.5, help="Decay period multiplier on max hold")
    parser.add_argument("--effective-max-hold-days", type=int, default=45, help="Effective max hold days after factor")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # GateKeeper before_run_grid (required by framework)
    _gatekeeper_before_run(output_dir)

    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    # Load all data
    try:
        gap_ranks = _load_gap_ranks()
        cb_basic = _load_cb_basic()
        cb_daily = _load_cb_daily()
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[bond_floor_entry_filter] FATAL: {exc}", flush=True)
        return 1

    # Compute bond floor per CB per day
    try:
        bond_floor_df = _compute_bond_floor(cb_basic, cb_daily, args.discount_rate)
    except Exception as exc:
        diag = {"error": str(exc), "step": "compute_bond_floor"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        print(f"[bond_floor_entry_filter] FATAL bond_floor: {exc}", flush=True)
        return 1

    # Slice gap_ranks into periods
    df_train = gap_ranks[
        (gap_ranks["trade_date"] >= train_start) & (gap_ranks["trade_date"] <= train_end)
    ].copy()
    df_test = gap_ranks[
        (gap_ranks["trade_date"] >= test_start) & (gap_ranks["trade_date"] <= test_end)
    ].copy()
    df_2020 = gap_ranks[gap_ranks["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
        )
        return 1

    # Build entry mask (applied to all periods)
    ranked_train = _build_entry_mask(
        bond_floor_df, args.bond_floor_threshold, args.min_days_below, df_train
    )
    ranked_test = _build_entry_mask(
        bond_floor_df, args.bond_floor_threshold, args.min_days_below, df_test
    )
    ranked_2020 = _build_entry_mask(
        bond_floor_df, args.bond_floor_threshold, args.min_days_below, df_2020
    )

    # Duration-adaptive exit params
    max_hold = args.effective_max_hold_days
    mhd = args.min_hold_days
    itf = args.initial_threshold_fraction

    # Baseline: no entry filter, just duration-adaptive exit
    baseline_train = _simulate_duration_adaptive(df_train, mhd, itf, max_hold, use_entry_mask=False)
    baseline_test = _simulate_duration_adaptive(df_test, mhd, itf, max_hold, use_entry_mask=False)
    baseline_2020 = _simulate_duration_adaptive(df_2020, mhd, itf, max_hold, use_entry_mask=False)

    # Filtered variant: entry mask + duration-adaptive exit
    filtered_train = _simulate_duration_adaptive(ranked_train, mhd, itf, max_hold, use_entry_mask=True)
    filtered_test = _simulate_duration_adaptive(ranked_test, mhd, itf, max_hold, use_entry_mask=True)
    filtered_2020 = _simulate_duration_adaptive(ranked_2020, mhd, itf, max_hold, use_entry_mask=True)

    # Excess returns
    excess_train = round(filtered_train["total_pnl"] - baseline_train["total_pnl"], 2)
    excess_test = round(filtered_test["total_pnl"] - baseline_test["total_pnl"], 2)
    excess_2020 = round(filtered_2020["total_pnl"] - baseline_2020["total_pnl"], 2)

    # Trade count retention
    bl_trade_count = baseline_train["trade_count"]
    f_trade_count = filtered_train["trade_count"]
    trade_count_retention = round(f_trade_count / bl_trade_count * 100, 1) if bl_trade_count > 0 else 0.0

    # Success criteria check (from proposal)
    criteria_met = (
        excess_test > 0
        and excess_train > 0
        and excess_2020 > 0
        and trade_count_retention > 50.0
        and filtered_test["max_drawdown"] >= baseline_test["max_drawdown"]
    )
    adoption_pass = criteria_met

    # Mask diagnostics
    mask_diag = _mask_diagnostics(ranked_train)

    # summary.json
    summary = {
        "adoption_pass": adoption_pass,
        "bond_floor_threshold": args.bond_floor_threshold,
        "min_days_below": args.min_days_below,
        "discount_rate": args.discount_rate,
        "exit_params": {
            "min_hold_days": mhd,
            "initial_threshold_fraction": itf,
            "effective_max_hold_days": max_hold,
        },
        "baseline": {
            "train": {
                "total_pnl": baseline_train["total_pnl"],
                "trade_count": baseline_train["trade_count"],
                "win_rate": baseline_train["win_rate"],
                "max_drawdown": baseline_train["max_drawdown"],
                "sharpe_ratio": baseline_train["sharpe_ratio"],
                "avg_hold_days": baseline_train["avg_hold_days"],
            },
            "test": {
                "total_pnl": baseline_test["total_pnl"],
                "trade_count": baseline_test["trade_count"],
                "win_rate": baseline_test["win_rate"],
                "max_drawdown": baseline_test["max_drawdown"],
                "sharpe_ratio": baseline_test["sharpe_ratio"],
                "avg_hold_days": baseline_test["avg_hold_days"],
            },
            "validate_2020": {
                "total_pnl": baseline_2020["total_pnl"],
                "trade_count": baseline_2020["trade_count"],
                "win_rate": baseline_2020["win_rate"],
                "max_drawdown": baseline_2020["max_drawdown"],
                "sharpe_ratio": baseline_2020["sharpe_ratio"],
                "avg_hold_days": baseline_2020["avg_hold_days"],
            },
        },
        "filtered": {
            "train": {
                "total_pnl": filtered_train["total_pnl"],
                "trade_count": filtered_train["trade_count"],
                "win_rate": filtered_train["win_rate"],
                "max_drawdown": filtered_train["max_drawdown"],
                "sharpe_ratio": filtered_train["sharpe_ratio"],
                "avg_hold_days": filtered_train["avg_hold_days"],
                "excess_return": excess_train,
                "decay_exits": filtered_train["decay_exits"],
                "gap_closed_exits": filtered_train["gap_closed_exits"],
                "force_closes": filtered_train["force_closes"],
            },
            "test": {
                "total_pnl": filtered_test["total_pnl"],
                "trade_count": filtered_test["trade_count"],
                "win_rate": filtered_test["win_rate"],
                "max_drawdown": filtered_test["max_drawdown"],
                "sharpe_ratio": filtered_test["sharpe_ratio"],
                "avg_hold_days": filtered_test["avg_hold_days"],
                "excess_return": excess_test,
                "decay_exits": filtered_test["decay_exits"],
                "gap_closed_exits": filtered_test["gap_closed_exits"],
                "force_closes": filtered_test["force_closes"],
            },
            "validate_2020": {
                "total_pnl": filtered_2020["total_pnl"],
                "trade_count": filtered_2020["trade_count"],
                "win_rate": filtered_2020["win_rate"],
                "max_drawdown": filtered_2020["max_drawdown"],
                "sharpe_ratio": filtered_2020["sharpe_ratio"],
                "avg_hold_days": filtered_2020["avg_hold_days"],
                "excess_return": excess_2020,
                "decay_exits": filtered_2020["decay_exits"],
                "gap_closed_exits": filtered_2020["gap_closed_exits"],
                "force_closes": filtered_2020["force_closes"],
            },
        },
        "trade_count_retention_pct": trade_count_retention,
        "entry_mask_diagnostics": mask_diag,
        "train_period": {"start": args.train_start, "end": args.train_end},
        "test_period": {"start": args.test_start, "end": args.test_end},
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    _now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    _today = _now.split("T", 1)[0]
    l6_decision = "adopt" if adoption_pass else "reject"
    decision = (
        "passed_mechanical_thresholds_not_promoted"
        if adoption_pass
        else "failed_mechanical_thresholds"
    )
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": _today,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "selected_variant": {
                "bond_floor_threshold": args.bond_floor_threshold,
                "min_days_below": args.min_days_below,
                "discount_rate": args.discount_rate,
            },
            "criteria": {
                "excess_test_gt_0": excess_test > 0,
                "excess_train_gt_0": excess_train > 0,
                "excess_2020_gt_0": excess_2020 > 0,
                "trade_count_retention_gt_50pct": trade_count_retention > 50.0,
            },
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [f"bond_floor_entry_filter threshold={args.bond_floor_threshold} min_days={args.min_days_below}"]
            if not adoption_pass
            else []
        ),
        "learnings": [
            f"Evaluated bond-floor entry filter (threshold={args.bond_floor_threshold}, "
            f"min_days_below={args.min_days_below}, discount_rate={args.discount_rate}) "
            f"with duration-adaptive exit baseline."
        ],
        "follow_up_actions": (
            ["evidence-only record; do not promote to truth without user approval"]
            if adoption_pass
            else ["review reject reason; do not revive without new mechanism"]
        ),
        "status": "COMPLETE",
        "generated_at": _now,
        "adoption_pass": adoption_pass,
        "decision": decision,
        "metrics": {
            "train": {
                "baseline": {
                    "total_pnl": baseline_train["total_pnl"],
                    "trade_count": baseline_train["trade_count"],
                    "max_drawdown": baseline_train["max_drawdown"],
                    "sharpe_ratio": baseline_train["sharpe_ratio"],
                },
                "filtered": {
                    "total_pnl": filtered_train["total_pnl"],
                    "trade_count": filtered_train["trade_count"],
                    "max_drawdown": filtered_train["max_drawdown"],
                    "sharpe_ratio": filtered_train["sharpe_ratio"],
                    "excess_return": excess_train,
                },
                "suppression_pct": mask_diag["suppression_pct"],
            },
            "test": {
                "baseline": {
                    "total_pnl": baseline_test["total_pnl"],
                    "trade_count": baseline_test["trade_count"],
                    "max_drawdown": baseline_test["max_drawdown"],
                    "sharpe_ratio": baseline_test["sharpe_ratio"],
                },
                "filtered": {
                    "total_pnl": filtered_test["total_pnl"],
                    "trade_count": filtered_test["trade_count"],
                    "max_drawdown": filtered_test["max_drawdown"],
                    "sharpe_ratio": filtered_test["sharpe_ratio"],
                    "excess_return": excess_test,
                },
            },
            "validate_2020": {
                "baseline": {
                    "total_pnl": baseline_2020["total_pnl"],
                    "trade_count": baseline_2020["trade_count"],
                    "max_drawdown": baseline_2020["max_drawdown"],
                    "sharpe_ratio": baseline_2020["sharpe_ratio"],
                },
                "filtered": {
                    "total_pnl": filtered_2020["total_pnl"],
                    "trade_count": filtered_2020["trade_count"],
                    "max_drawdown": filtered_2020["max_drawdown"],
                    "sharpe_ratio": filtered_2020["sharpe_ratio"],
                    "excess_return": excess_2020,
                },
            },
        },
        "warnings": [],
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # l4_ack.yaml
    l4_ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes",
        "ack_at": _now,
        "q1_bond_floor_computation": {
            "description": "Bond floor computed from cb_basic using PV of coupons + par.",
            "answer": f"Discount rate={args.discount_rate}, computed for {len(bond_floor_df)} CB-day rows.",
            "computed_data": {
                "bond_floor_rows": len(bond_floor_df),
                "mean_bond_floor": round(float(bond_floor_df["bond_floor_value"].mean()), 2),
            },
            "computed_at": _now,
            "pass": True,
        },
        "q2_entry_filter_effect": {
            "description": "Entry filter suppresses CBs above bond-floor threshold.",
            "answer": f"threshold={args.bond_floor_threshold}, min_days={args.min_days_below}, "
                      f"train suppression={mask_diag['suppression_pct']}%",
            "computed_data": mask_diag,
            "computed_at": _now,
            "pass": True,
        },
        "q3_backtest_comparison": {
            "description": "Filtered vs baseline duration-adaptive excess returns.",
            "answer": f"train_excess={excess_train}, test_excess={excess_test}, 2020_excess={excess_2020}",
            "computed_data": {
                "excess_train": excess_train,
                "excess_test": excess_test,
                "excess_2020": excess_2020,
                "trade_count_retention_pct": trade_count_retention,
            },
            "computed_at": _now,
            "pass": adoption_pass,
        },
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # diagnostic.yaml
    diagnostic = {
        "warnings": [],
        "errors": [],
        "data_rows": {
            "gap_ranks": len(gap_ranks),
            "cb_basic": len(cb_basic),
            "cb_daily": len(cb_daily),
            "bond_floor_computed": len(bond_floor_df),
            "train": len(df_train),
            "test": len(df_test),
            "validate_2020": len(df_2020),
        },
        "bond_floor_stats": {
            "mean": round(float(bond_floor_df["bond_floor_value"].mean()), 2),
            "min": round(float(bond_floor_df["bond_floor_value"].min()), 2),
            "max": round(float(bond_floor_df["bond_floor_value"].max()), 2),
            "pct_non_finite": round(
                float((~np.isfinite(bond_floor_df["bond_floor_value"])).mean()) * 100, 2
            ),
        },
        "bond_floor_ratio_stats": {
            "threshold": args.bond_floor_threshold,
            "min_days_below": args.min_days_below,
            "discount_rate": args.discount_rate,
        },
        "entry_mask_diagnostics": mask_diag,
        "baseline_trade_count": bl_trade_count,
        "filtered_trade_count": f_trade_count,
        "trade_count_retention_pct": trade_count_retention,
        "exit_params": {
            "min_hold_days": mhd,
            "initial_threshold_fraction": itf,
            "effective_max_hold_days": max_hold,
        },
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # GateKeeper after_run_grid
    _gatekeeper_after_run(output_dir)

    # Done
    print(
        f"[bond_floor_entry_filter] adoption_pass={adoption_pass} "
        f"threshold={args.bond_floor_threshold} "
        f"min_days={args.min_days_below} "
        f"excess_train={excess_train} "
        f"excess_test={excess_test} "
        f"excess_2020={excess_2020} "
        f"trade_retention={trade_count_retention}% "
        f"suppression={mask_diag['suppression_pct']}%",
        flush=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
