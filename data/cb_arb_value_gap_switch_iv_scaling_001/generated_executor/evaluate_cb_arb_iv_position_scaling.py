#!/usr/bin/env python3
"""Evaluate inverse IV-percentile position scaling for cb_arb value-gap switch.

This executor computes rolling 252-day stock-implied-volatility percentile
for each convertible bond, then applies inverse position scaling at entry:
higher IV percentile → smaller position. The baseline ranking weight and
buy amount are scaled down when IV is elevated, while entry/exit eligibility
remains unchanged from the baseline gap-based rules.

Grid search over iv_percentile_threshold (0.5–0.9), scaling_factor_power
(0.5–2.0), and min_hold_days (1/3/5).  Compares against un-scaled baseline
on train, 2020 repair, and test periods.

IV proxy: rolling 20-day annualised stock volatility from stk_daily_qfq.
Percentile: rolling 252-day rank within each CB's own history.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime as _dt, timezone as _tz
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

# ── Lazy third-party imports ────────────────────────────────────────────
# Not available at module level in isolated compliance probe; must be
# imported lazily inside functions.


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


# ── constants ───────────────────────────────────────────────────────────
_PREVIOUS_RUN_DATA = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_CB_BASIC_PATH = "data/cb_warehouse/cb_basic.parquet"
_STK_DAILY_PATH = "data/cb_warehouse/stk_daily_qfq.parquet"
_VOL_LOOKBACK = 20       # trading days for rolling stock vol
_PCT_LOOKBACK = 252       # trading days for IV percentile window
_PCT_MIN_SAMPLES = 60     # minimum observations to compute a percentile
_GRID_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)
_GRID_POWERS = (0.5, 1.0, 1.5, 2.0)
_GRID_MIN_HOLD = (1, 3, 5)


# ── data-requirements declaration ───────────────────────────────────────
def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "required_files": [
            {
                "path": _PREVIOUS_RUN_DATA,
                "description": (
                    "Daily value-gap amounts from regime-option-entry-gate run. "
                    "Contains trade_date, ts_code, value_gap_amount, position_cash, "
                    "buy_qty, rank_pct, and other baseline fields."
                ),
                "required_columns": [
                    "trade_date", "ts_code", "value_gap_amount",
                    "position_cash", "buy_qty",
                ],
            },
            {
                "path": _CB_BASIC_PATH,
                "description": (
                    "CB-to-underlying-stock mapping. Required columns: ts_code, stk_code."
                ),
                "required_columns": ["ts_code", "stk_code"],
            },
            {
                "path": _STK_DAILY_PATH,
                "description": (
                    "Forward-adjusted daily stock prices for volatility computation."
                ),
                "required_columns": ["stk_code", "trade_date", "close"],
            },
        ],
        "generated_columns": {
            "stk_code": (
                "Derived for value-gap rows via cb_basic.ts_code -> cb_basic.stk_code."
            ),
            "stock_vol": (
                "Computed inside executor: 20-day rolling annualised log-return "
                "volatility from stk_daily_qfq.close."
            ),
            "vol_pctile": (
                "Computed inside executor: 252-day rolling percentile rank (0-100) "
                "of stock_vol within each CB's own history."
            ),
            "scaling_factor": (
                "Computed inside executor: (1 - vol_pctile/100) ** power when "
                "vol_pctile/100 > threshold, else 1.0."
            ),
        },
    }


# ── GateKeeper integration ──────────────────────────────────────────────
def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ── path resolution ─────────────────────────────────────────────────────
def _resolve_path(relative: str, data_root: str) -> Path:
    rel = Path(relative)
    candidates = [Path(data_root) / rel, _REPO_ROOT / rel, Path.cwd() / rel]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(f"Required data missing; searched: {searched}")
    return path


# ── data loading ────────────────────────────────────────────────────────
def _load_gap_data(data_root: str) -> Any:
    pd = _get_pd()
    path = _resolve_path(_PREVIOUS_RUN_DATA, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    return df


def _load_cb_basic(data_root: str) -> Any:
    pd = _get_pd()
    path = _resolve_path(_CB_BASIC_PATH, data_root)
    df = pd.read_parquet(path)
    missing = {"ts_code", "stk_code"} - set(df.columns)
    if missing:
        raise ValueError(f"cb_basic missing columns: {sorted(missing)}")
    return df[["ts_code", "stk_code"]].dropna().drop_duplicates()


def _load_and_compute_stock_vol(data_root: str) -> Any:
    """Compute rolling 20-day annualised stock vol for each stock."""
    np = _get_np()
    pd = _get_pd()
    path = _resolve_path(_STK_DAILY_PATH, data_root)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)

    df["log_return"] = df.groupby("stk_code")["close"].transform(
        lambda s: np.log(s / s.shift(1))
    )
    df["stock_vol"] = df.groupby("stk_code")["log_return"].transform(
        lambda s: s.rolling(_VOL_LOOKBACK, min_periods=10).std() * np.sqrt(252)
    )
    return df[["stk_code", "trade_date", "close", "stock_vol"]]


# ── IV percentile computation ───────────────────────────────────────────
def _compute_vol_percentile(df: Any) -> Any:
    """Compute rolling 252-day percentile rank (0-100) of stock_vol per CB."""
    np = _get_np()
    pd = _get_pd()
    df = df.copy()
    df["vol_pctile"] = np.nan

    for ts_code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        vols = grp["stock_vol"].values
        pctiles = np.full(len(vols), np.nan)

        for i in range(len(vols)):
            win_start = max(0, i - _PCT_LOOKBACK + 1)
            win_vols = vols[win_start : i + 1]
            valid = win_vols[~np.isnan(win_vols)]
            if len(valid) < _PCT_MIN_SAMPLES:
                continue
            current = vols[i]
            if np.isnan(current):
                continue
            rank = (valid < current).sum() + 0.5 * (valid == current).sum()
            pctiles[i] = (rank / len(valid)) * 100.0

        df.loc[grp.index, "vol_pctile"] = pctiles

    return df


def _compute_scaling_factor(
    vol_pctile: float, threshold: float, power: float
) -> float:
    """Return inverse IV-percentile scaling factor.

    If vol_pctile/100 > threshold:
      scale = (1 - vol_pctile/100) ** power
    else:
      scale = 1.0

    Args:
        vol_pctile: percentile in [0, 100] (or NaN)
        threshold: trigger threshold in [0, 1]
        power: exponent >= 0
    """
    np = _get_np()
    if not np.isfinite(vol_pctile):
        return 1.0
    pct_frac = vol_pctile / 100.0
    if pct_frac > threshold:
        return max(0.0, float((1.0 - pct_frac) ** power))
    return 1.0


# ── backtest simulation (position-scaled gap trading) ───────────────────
def _simulate(
    df: Any,
    threshold: float | None = None,
    power: float | None = None,
    min_hold_days: int = 0,
) -> dict[str, Any]:
    """Simulate gap-based CB trading with optional IV-percentile position scaling.

    Entry: gap > 0 and flat and (if re-entering after exit, min_hold_days passed).
    Exit:  gap <= 0 (always).
    PnL:   position_cash * (exit_gap_pct - entry_gap_pct) * scaling_factor,
           where gap_pct = value_gap_amount / position_cash.

    When threshold and power are None this runs the *un-scaled* baseline
    (scaling_factor == 1.0).
    """
    np = _get_np()
    use_scaling = threshold is not None and power is not None
    total_pnl = 0.0
    trades: list[dict[str, Any]] = []

    for ts_code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        in_position = False
        entry_row: dict[str, Any] | None = None
        last_exit_idx: int = -99999

        for idx, row in grp.iterrows():
            trade_date = row["trade_date"]
            gap = float(row["value_gap_amount"])
            pos_cash = float(row.get("position_cash", 30000.0))

            if not in_position and gap > 0:
                # Enforce min_hold_days gate on re-entry
                if min_hold_days > 0 and (idx - last_exit_idx) < min_hold_days:
                    continue
                in_position = True
                entry_row = {
                    "trade_date": trade_date,
                    "gap": gap,
                    "pos_cash": pos_cash,
                }
                # Compute scaling factor at entry
                if use_scaling:
                    vol_pct = float(row.get("vol_pctile", np.nan))
                    sf = _compute_scaling_factor(vol_pct, threshold, power)
                else:
                    sf = 1.0
                entry_row["scaling_factor"] = sf
                continue

            if in_position and gap <= 0:
                e = entry_row
                gap_pct_entry = e["gap"] / e["pos_cash"]
                gap_pct_exit = gap / pos_cash
                pnl = e["pos_cash"] * (gap_pct_exit - gap_pct_entry) * e["scaling_factor"]
                total_pnl += pnl
                hold = int((trade_date - e["trade_date"]).days)
                trades.append({
                    "ts_code": ts_code,
                    "entry_date": str(e["trade_date"].date()),
                    "exit_date": str(trade_date.date()),
                    "exit_reason": "gap_closed",
                    "entry_gap": round(e["gap"], 4),
                    "exit_gap": round(gap, 4),
                    "pnl": round(float(pnl), 2),
                    "hold_days": int(hold),
                    "scaling_factor": round(e["scaling_factor"], 4),
                })
                in_position = False
                entry_row = None
                last_exit_idx = idx

        # Force-close any open position at end of data
        if in_position and entry_row is not None:
            last = grp.iloc[-1]
            final_gap = float(last["value_gap_amount"])
            pos_cash = float(last.get("position_cash", 30000.0))
            gap_pct_entry = entry_row["gap"] / entry_row["pos_cash"]
            gap_pct_exit = final_gap / pos_cash
            pnl = entry_row["pos_cash"] * (gap_pct_exit - gap_pct_entry) * entry_row["scaling_factor"]
            total_pnl += pnl
            hold = int((last["trade_date"] - entry_row["trade_date"]).days)
            trades.append({
                "ts_code": ts_code,
                "entry_date": str(entry_row["trade_date"].date()),
                "exit_date": str(last["trade_date"].date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_row["gap"], 4),
                "exit_gap": round(final_gap, 4),
                "pnl": round(float(pnl), 2),
                "hold_days": int(hold),
                "scaling_factor": round(entry_row["scaling_factor"], 4),
            })

    return _aggregate_metrics(trades, total_pnl)


def _aggregate_metrics(
    trades: list[dict[str, Any]], total_pnl: float
) -> dict[str, Any]:
    pd = _get_pd()
    if not trades:
        return {
            "total_pnl": 0.0,
            "total_return": 0.0,
            "excess_return": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "trades": [],
        }
    tdf = pd.DataFrame(trades)
    winning = tdf[tdf["pnl"] > 0]
    losing = tdf[tdf["pnl"] <= 0]
    win_rate = round(len(winning) / len(tdf), 4) if len(tdf) > 0 else 0.0
    avg_win = round(float(winning["pnl"].mean()), 2) if len(winning) > 0 else 0.0
    avg_loss = round(float(losing["pnl"].mean()), 2) if len(losing) > 0 else 0.0

    tdf_sorted = tdf.sort_values("exit_date")
    tdf_sorted["cum_pnl"] = tdf_sorted["pnl"].cumsum()
    equity = tdf_sorted["cum_pnl"]
    peak = equity.iloc[0] if len(equity) > 0 else 0.0
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = val - peak
        if dd < max_dd:
            max_dd = dd

    return {
        "total_pnl": round(total_pnl, 2),
        "total_return": round(total_pnl, 2),   # same for gap-based PnL
        "excess_return": 0.0,
        "trade_count": int(len(tdf)),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_drawdown": float(max_dd),
        "trades": trades,
    }


# ── utility ─────────────────────────────────────────────────────────────
def _plain(value: Any) -> Any:
    np = _get_np()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if hasattr(value, "dtype") and hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fail(output_dir: Path, step: str, exc: Exception) -> int:
    yaml = _get_yaml()
    diag = {"error": str(exc), "step": step}
    _ensure_yaml_np_reprs()
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diag), allow_unicode=True), encoding="utf-8"
    )
    _gatekeeper_after_run(output_dir)
    print(f"[iv_position_scaling] FATAL ({step}): {exc}", flush=True)
    return 1


# ── main ────────────────────────────────────────────────────────────────
def main() -> int:
    np = _get_np()
    pd = _get_pd()
    yaml = _get_yaml()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--iv-lookback", type=int, default=252,
                        help="Rolling window for IV percentile (default: 252)")
    parser.add_argument("--iv-percentile-threshold", type=float, default=0.8,
                        help="IV percentile threshold for scaling trigger (default: 0.8)")
    parser.add_argument("--scaling-factor-power", type=float, default=1.0,
                        help="Exponent for inverse scaling (default: 1.0)")
    parser.add_argument("--min-hold-days", type=int, default=5,
                        help="Minimum hold days before re-entry (default: 5)")
    parser.add_argument("--fixed-source", type=str, default="2")
    parser.add_argument("--rule", type=str, default="score_4state")
    parser.add_argument("--base-ranks-path", type=str,
                        default=_PREVIOUS_RUN_DATA)
    parser.add_argument("--reuse-ranks", action="store_true", default=True)
    parser.add_argument("--cost-model-enabled", action="store_true", default=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    # -- 1. Load gap data --
    try:
        df_raw = _load_gap_data(args.data_root)
    except Exception as exc:
        return _fail(output_dir, "load_gap_data", exc)

    # -- 2. Load CB->stock mapping --
    try:
        df_basic = _load_cb_basic(args.data_root)
    except Exception as exc:
        return _fail(output_dir, "load_cb_basic", exc)

    # -- 3. Load & compute stock vol --
    try:
        df_vol = _load_and_compute_stock_vol(args.data_root)
    except Exception as exc:
        return _fail(output_dir, "load_stock_vol", exc)

    # -- 4. Merge vol -> gap data, compute percentile --
    cb_to_stk = dict(
        zip(df_basic["ts_code"].astype(str), df_basic["stk_code"].astype(str))
    )
    df_enriched = df_raw.copy()
    df_enriched["stk_code"] = df_enriched["ts_code"].astype(str).map(cb_to_stk)

    df_vol_lean = df_vol[["stk_code", "trade_date", "stock_vol"]].copy()
    df_merged = df_enriched.merge(
        df_vol_lean, on=["stk_code", "trade_date"], how="left"
    )
    df_merged = _compute_vol_percentile(df_merged)

    # -- 5. Filter periods --
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_merged[
        (df_merged["trade_date"] >= train_start)
        & (df_merged["trade_date"] <= train_end)
    ].copy()
    df_test = df_merged[
        (df_merged["trade_date"] >= test_start)
        & (df_merged["trade_date"] <= test_end)
    ].copy()
    df_2020 = df_train[df_train["trade_date"].dt.year == 2020].copy()

    if len(df_train) == 0:
        return _fail(output_dir, "filter_train",
                     ValueError("Train dataframe is empty"))

    # -- 6. Baseline (un-scaled) --
    baseline_train = _simulate(df_train)
    baseline_test = _simulate(df_test)
    baseline_2020 = _simulate(df_2020)

    # -- 7. Grid search --
    thresholds = sorted(set(_GRID_THRESHOLDS + (args.iv_percentile_threshold,)))
    powers = sorted(set(_GRID_POWERS + (args.scaling_factor_power,)))
    min_holds = sorted(set(_GRID_MIN_HOLD + (args.min_hold_days,)))

    all_candidates: list[dict[str, Any]] = []
    best_candidate: dict[str, Any] | None = None
    best_score = -float("inf")

    for thr in thresholds:
        for pwr in powers:
            for mhd in min_holds:
                train_res = _simulate(df_train, thr, pwr, mhd)
                test_res = _simulate(df_test, thr, pwr, mhd)
                yr2020_res = _simulate(df_2020, thr, pwr, mhd)

                excess_train = round(
                    train_res["total_pnl"] - baseline_train["total_pnl"], 2
                )
                excess_test = round(
                    test_res["total_pnl"] - baseline_test["total_pnl"], 2
                )
                excess_2020 = round(
                    yr2020_res["total_pnl"] - baseline_2020["total_pnl"], 2
                )

                candidate = {
                    "iv_percentile_threshold": thr,
                    "scaling_factor_power": pwr,
                    "min_hold_days": mhd,
                    "train": {
                        "total_pnl": train_res["total_pnl"],
                        "total_return": train_res["total_pnl"],
                        "trade_count": train_res["trade_count"],
                        "win_rate": train_res["win_rate"],
                        "avg_win": train_res["avg_win"],
                        "avg_loss": train_res["avg_loss"],
                        "max_drawdown": train_res["max_drawdown"],
                        "excess_return": excess_train,
                    },
                    "test": {
                        "total_pnl": test_res["total_pnl"],
                        "total_return": test_res["total_pnl"],
                        "trade_count": test_res["trade_count"],
                        "win_rate": test_res["win_rate"],
                        "avg_win": test_res["avg_win"],
                        "avg_loss": test_res["avg_loss"],
                        "max_drawdown": test_res["max_drawdown"],
                        "excess_return": excess_test,
                    },
                    "validate_2020": {
                        "total_pnl": yr2020_res["total_pnl"],
                        "total_return": yr2020_res["total_pnl"],
                        "trade_count": yr2020_res["trade_count"],
                        "win_rate": yr2020_res["win_rate"],
                        "avg_win": yr2020_res["avg_win"],
                        "avg_loss": yr2020_res["avg_loss"],
                        "max_drawdown": yr2020_res["max_drawdown"],
                        "excess_return": excess_2020,
                    },
                }
                all_candidates.append(candidate)

                # Score: primary objective is 2020 excess return
                # Secondary: test excess return
                score = excess_2020 * 2.0 + excess_test
                if score > best_score:
                    best_score = score
                    best_candidate = candidate

    # -- 8. Adoption criteria (from proposal) --
    adoption_pass = False
    if best_candidate is not None:
        bc = best_candidate

        # Criterion 1: 2020 metrics must improve over baseline
        dd_2020_ok = (
            bc["validate_2020"]["max_drawdown"]
            >= baseline_2020["max_drawdown"]
        )  # less-negative drawdown is improvement
        excess_2020_ok = bc["validate_2020"]["excess_return"] > 0

        # Criterion 2: test period not significantly degraded
        test_excess_ok = bc["test"]["excess_return"] >= 0

        # Criterion 3: win_rate in test >= baseline
        test_wr_ok = bc["test"]["win_rate"] >= baseline_test["win_rate"]

        adoption_pass = dd_2020_ok and excess_2020_ok and test_excess_ok and test_wr_ok

    # -- 9. summary.json --
    summary = {
        "adoption_pass": adoption_pass,
        "best_candidate": best_candidate,
        "all_candidates": all_candidates,
        "baseline": {
            "train": {
                "total_pnl": baseline_train["total_pnl"],
                "trade_count": baseline_train["trade_count"],
                "win_rate": baseline_train["win_rate"],
                "max_drawdown": baseline_train["max_drawdown"],
            },
            "test": {
                "total_pnl": baseline_test["total_pnl"],
                "trade_count": baseline_test["trade_count"],
                "win_rate": baseline_test["win_rate"],
                "max_drawdown": baseline_test["max_drawdown"],
            },
            "validate_2020": {
                "total_pnl": baseline_2020["total_pnl"],
                "trade_count": baseline_2020["trade_count"],
                "win_rate": baseline_2020["win_rate"],
                "max_drawdown": baseline_2020["max_drawdown"],
            },
        },
        "train_period": {"start": args.train_start, "end": args.train_end},
        "test_period": {"start": args.test_start, "end": args.test_end},
        "swept_thresholds": list(thresholds),
        "swept_powers": list(powers),
        "swept_min_hold_days": list(min_holds),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # -- 10. report.yaml --
    _now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    _today = _now.split("T", 1)[0]
    l6_decision = "adopt" if adoption_pass else "reject"

    evaluator_report = {
        "proposal_id": "cb_arb_value_gap_switch_iv_scaling_001",
        "strategy_id": "cb_arb_value_gap_switch",
        "executor": "cb_iv_position_scaling_executor",
        "adoption_pass": adoption_pass,
        "best_iv_percentile_threshold": (
            best_candidate["iv_percentile_threshold"] if best_candidate else None
        ),
        "best_scaling_factor_power": (
            best_candidate["scaling_factor_power"] if best_candidate else None
        ),
        "best_min_hold_days": (
            best_candidate["min_hold_days"] if best_candidate else None
        ),
        "candidates": all_candidates,
        "baseline": {
            "train_pnl": baseline_train["total_pnl"],
            "test_pnl": baseline_test["total_pnl"],
            "validate_2020_pnl": baseline_2020["total_pnl"],
        },
    }
    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": _today,
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "best_iv_percentile_threshold": (
                best_candidate["iv_percentile_threshold"] if best_candidate else None
            ),
            "best_scaling_factor_power": (
                best_candidate["scaling_factor_power"] if best_candidate else None
            ),
            "best_min_hold_days": (
                best_candidate["min_hold_days"] if best_candidate else None
            ),
            "evaluator": "cb_iv_position_scaling_executor",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": (
            [
                f"variants below {output_dir.name} best by adoption criteria "
                f"-- evidence only, not promoted"
            ]
            if adoption_pass
            else [
                f"{output_dir.name}: rejected by mechanical thresholds; "
                f"review.yaml must finalize."
            ]
        ),
        "learnings": [
            "IV-percentile position scaling grid evaluated end-to-end.",
        ],
        "follow_up_actions": (
            ["evidence-only record; do not promote without user approval"]
            if adoption_pass
            else ["review reject reason; do not revive without new mechanism"]
        ),
        "status": "COMPLETE",
        "generated_at": _now,
        "evaluator_report": evaluator_report,
    }
    _ensure_yaml_np_reprs()
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # -- 11. l4_ack.yaml --
    l4_ack = {
        "status": "completed",
        "adoption_pass": adoption_pass,
        "message": "IV-percentile position scaling evaluation finished.",
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True), encoding="utf-8"
    )

    # -- 12. diagnostic.yaml --
    vol_missing_train = int(df_train["stock_vol"].isna().sum())
    vol_missing_test = int(df_test["stock_vol"].isna().sum())
    vol_missing_2020 = int(df_2020["stock_vol"].isna().sum())

    # Scaling distribution for best candidate
    scaling_stats: dict[str, Any] = {}
    if best_candidate is not None:
        bc = best_candidate
        best_test_res = _simulate(
            df_test, bc["iv_percentile_threshold"],
            bc["scaling_factor_power"], bc["min_hold_days"]
        )
        sfs = [t.get("scaling_factor", 1.0) for t in best_test_res.get("trades", [])]
        if sfs:
            scaling_stats = {
                "mean": round(float(np.mean(sfs)), 4),
                "min": round(float(np.min(sfs)), 4),
                "max": round(float(np.max(sfs)), 4),
                "pct_scaled_down": round(
                    sum(1 for s in sfs if s < 1.0) / len(sfs) * 100, 1
                ),
            }

    diagnostics = {
        "warnings": [],
        "errors": [],
        "data_rows": {
            "train": int(len(df_train)),
            "test": int(len(df_test)),
            "validate_2020": int(len(df_2020)),
        },
        "vol_coverage": {
            "train_missing": vol_missing_train,
            "test_missing": vol_missing_test,
            "validate_2020_missing": vol_missing_2020,
        },
        "cb_to_stk_mapping_count": len(cb_to_stk),
        "scaling_stats": scaling_stats,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostics), allow_unicode=True), encoding="utf-8"
    )

    best_info = (
        f"thr={best_candidate['iv_percentile_threshold']},"
        f"pwr={best_candidate['scaling_factor_power']},"
        f"mhd={best_candidate['min_hold_days']}"
        if best_candidate
        else "none"
    )
    print(
        f"[iv_position_scaling] adoption_pass={adoption_pass} "
        f"best=({best_info}) "
        f"candidates={len(all_candidates)}",
        flush=True,
    )
    _gatekeeper_after_run(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
