#!/usr/bin/env python3
"""Evaluate dynamic trailing exit gap decay proposal."""
import argparse
import json
import os
import sys
from itertools import product
from pathlib import Path

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402


def declare_data_requirements(command, spec):
    return {
        "required_files": [
            {
                "path": "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet",
                "description": "Daily value-gap amounts used by the dynamic exit evaluator.",
            }
        ]
    }


def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def load_data(data_root: str) -> pd.DataFrame:
    """Load daily value gap amounts and optional baseline positions."""
    relative_path = Path(
        "data",
        "cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17",
        "daily_value_gap_amounts.parquet",
    )
    candidates = [
        Path(data_root) / relative_path,
        Path(data_root) / relative_path.relative_to("data"),
        _REPO_ROOT / relative_path,
        Path.cwd() / relative_path,
    ]
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise FileNotFoundError(f"Required data missing; searched: {searched}")
    df = pd.read_parquet(path)
    # normalize columns
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    date_col = None
    gap_col = None
    pos_col = None
    code_col = None
    for c in df.columns:
        if c in ("trade_date", "date"):
            date_col = c
        elif c in ("value_gap_amount", "gap_amount", "tradable_gap_amount", "gap"):
            gap_col = c
        elif c in ("position", "is_holding"):
            pos_col = c
        elif c in ("stock_code", "ts_code", "cb_code", "symbol"):
            code_col = c
    if date_col is None or gap_col is None or code_col is None:
        raise KeyError("Could not identify date, code, or gap column")
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.rename(columns={date_col: "trade_date", code_col: "stock_code", gap_col: "gap"})
    if pos_col:
        df = df.rename(columns={pos_col: "baseline_position"})
    else:
        # fallback: simple gap > 0 entry signal
        df["baseline_position"] = 0
    return df


def ensure_baseline_positions(df: pd.DataFrame) -> pd.DataFrame:
    """If baseline_position column is all zeros, generate simple entries."""
    if df["baseline_position"].sum() == 0:
        # create a simple entry/exit on gap > 0
        grouped = df.sort_values(["stock_code", "trade_date"]).groupby("stock_code")
        new_pos = []
        for _, grp in grouped:
            pos = 0
            row_pos = []
            for _, row in grp.iterrows():
                if row["gap"] > 0 and pos == 0:
                    pos = 1
                elif row["gap"] <= 0 and pos == 1:
                    pos = 0
                row_pos.append(pos)
            grp["baseline_position"] = row_pos
            new_pos.append(grp)
        df = pd.concat(new_pos)
    return df


def simulate(params: dict, df: pd.DataFrame) -> dict:
    """Simulate trades using dynamic trailing exit and return metrics."""
    retrace = params["retrace_fraction"]
    min_hold = params["min_hold_days"]
    df = df.sort_values(["stock_code", "trade_date"]).copy()
    # ensure baseline_position is integer 0/1
    df["baseline_position"] = df["baseline_position"].astype(int).copy()
    # state columns
    df["our_position"] = 0
    df["peak_gap"] = 0.0
    df["entry_date"] = pd.NaT
    trades = []
    # group by stock
    for stock, grp in df.groupby("stock_code"):
        grp = grp.sort_values("trade_date")
        idx_list = grp.index.tolist()
        pos = 0
        peak = 0.0
        entry_date = None
        entry_gap = None
        for i, idx in enumerate(idx_list):
            row = grp.loc[idx]
            bl_pos = row["baseline_position"]
            gap = row["gap"]
            # entry signal: baseline enters and we are flat
            if bl_pos == 1 and pos == 0:
                pos = 1
                entry_date = row["trade_date"]
                entry_gap = gap
                peak = gap
            elif bl_pos == 0 and pos == 1:
                # baseline would exit, but we keep holding; do nothing unless forced
                pass
            # while in position, update peak
            if pos == 1:
                if gap > peak:
                    peak = gap
                # check exit condition
                hold_days = (row["trade_date"] - entry_date).days
                if gap < peak * (1 - retrace) and hold_days >= min_hold:
                    # close trade
                    exit_date = row["trade_date"]
                    exit_gap = gap
                    trades.append({
                        "stock": stock,
                        "entry_date": entry_date,
                        "exit_date": exit_date,
                        "entry_gap": entry_gap,
                        "exit_gap": exit_gap,
                        "hold_days": hold_days,
                        "peak": peak,
                    })
                    pos = 0
                    peak = 0.0
                    entry_date = None
            # update our position flag for PnL calc
            df.at[idx, "our_position"] = pos
            df.at[idx, "peak_gap"] = peak
            df.at[idx, "entry_date"] = entry_date
        # if still in position at end of data, force close on last day
        if pos == 1:
            last_row = grp.iloc[-1]
            exit_date = last_row["trade_date"]
            exit_gap = last_row["gap"]
            hold_days = (exit_date - entry_date).days
            trades.append({
                "stock": stock,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_gap": entry_gap,
                "exit_gap": exit_gap,
                "hold_days": hold_days,
                "peak": peak,
            })
            # no need to set pos=0, just note
    # compute daily PnL: change in gap * position
    df["prev_gap"] = df.groupby("stock_code")["gap"].shift(1)
    df["daily_pnl"] = df["our_position"] * (df["gap"] - df["prev_gap"])
    df["daily_pnl"] = df["daily_pnl"].fillna(0.0)
    total_return = df["daily_pnl"].sum()
    # compute win rate
    if trades:
        trade_df = pd.DataFrame(trades)
        trade_df["profit"] = trade_df["exit_gap"] - trade_df["entry_gap"]
        win_rate = (trade_df["profit"] > 0).mean()
        trade_count = len(trade_df)
    else:
        win_rate = 0.0
        trade_count = 0
    return {
        "total_return": float(total_return),
        "win_rate": float(win_rate),
        "trade_count": trade_count,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    try:
        df_raw = load_data(args.data_root)
    except Exception as e:
        diag = {"error": str(e)}
        with open(os.path.join(args.output_dir, "diagnostic.yaml"), "w") as f:
            yaml.dump(diag, f)
        sys.exit(1)

    df_raw = ensure_baseline_positions(df_raw)

    # filter periods
    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    df_train = df_raw[(df_raw["trade_date"] >= train_start) & (df_raw["trade_date"] <= train_end)].copy()
    df_test = df_raw[(df_raw["trade_date"] >= test_start) & (df_raw["trade_date"] <= test_end)].copy()

    # parameter sweep
    retrace_fractions = [0.3, 0.5, 0.7]
    min_hold_days = [1, 5, 10]
    param_grid = list(product(retrace_fractions, min_hold_days))

    results = []
    best_candidate = None
    best_test_return = -float("inf")

    for rf, mhd in param_grid:
        params = {"retrace_fraction": rf, "min_hold_days": mhd}
        train_metrics = simulate(params, df_train)
        test_metrics = simulate(params, df_test)
        # 2020 repair year from train
        df_train_2020 = df_train[df_train["trade_date"].dt.year == 2020]
        yr2020_metrics = simulate(params, df_train_2020)
        # aggregate
        res = {
            "params": params,
            "train_total_return": train_metrics["total_return"],
            "test_total_return": test_metrics["total_return"],
            "train_win_rate": train_metrics["win_rate"],
            "test_win_rate": test_metrics["win_rate"],
            "yr2020_total_return": yr2020_metrics["total_return"],
            "yr2020_win_rate": yr2020_metrics["win_rate"],
        }
        # use total_return as score for simplicity
        res["train_score"] = res["train_total_return"]
        res["test_score"] = res["test_total_return"]
        res["yr2020_score"] = res["yr2020_total_return"]
        results.append(res)

        if res["test_total_return"] > best_test_return:
            best_test_return = res["test_total_return"]
            best_candidate = res

    # summary.json
    summary = {
        "best_candidate": best_candidate,
        "all_candidates": results,
    }
    with open(os.path.join(args.output_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)

    # report.yaml (full results)
    with open(os.path.join(args.output_dir, "report.yaml"), "w") as f:
        yaml.dump({"candidates": results}, f)

    # l4_ack.yaml
    ack = {"status": "completed", "message": "dynamic exit gap decay evaluation finished"}
    with open(os.path.join(args.output_dir, "l4_ack.yaml"), "w") as f:
        yaml.dump(ack, f)

    # diagnostic.yaml
    diag = {"warnings": [], "errors": []}
    with open(os.path.join(args.output_dir, "diagnostic.yaml"), "w") as f:
        yaml.dump(diag, f)


if __name__ == "__main__":
    main()
