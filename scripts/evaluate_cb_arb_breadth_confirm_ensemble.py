"""Search cb_arb breadth + pool-mean confirm panic protection.

Both signals are exogenous to strategy PnL: they use cross-sectional
close-to-close CB returns on signal date T, then apply the opportunity
protection action on trading date T+1. T+1 lag is mandatory to avoid
same-close lookahead.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_selfpnl_regime_switch import (  # noqa: E402
    _annual_window,
    _baseline_rows,
    _daily_frame,
    _default_ranks_path,
    _evaluate_candidate_year,
    _fmt_float,
    _init_worker,
    _load_benchmark_daily,
    _medium_params,
    _parse_int_list,
    _run_year,
    _write_rows,
)
from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_cb_daily,
    _load_or_build_value_ranks,
    _score,
)

MAIN_FLOORS = {
    2020: -0.130604,
    2021: -0.050441,
    2022: 0.014425,
    2023: -0.031027,
}


def _candidate_grid() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for drop_threshold, panic_ratio, recovery, hurdle, min_days, confirm_threshold in itertools.product(
        [-0.03, -0.05],
        [0.20, 0.30],
        [1, 2, 3],
        [0.05, 0.08, 0.10],
        [1],
        [-0.005, -0.010, -0.015],
    ):
        rows.append(
            {
                "name": (
                    f"breadth_drop{_fmt_float(drop_threshold)}_"
                    f"ratio{_fmt_float(panic_ratio)}_rec{recovery}_"
                    f"h{_fmt_float(hurdle)}_min{min_days}_"
                    f"confirm{_fmt_float(confirm_threshold)}"
                ),
                "breadth_drop_threshold": float(drop_threshold),
                "breadth_panic_ratio": float(panic_ratio),
                "breadth_risk_recovery_days": int(recovery),
                "breadth_risk_switch_hurdle_pct": float(hurdle),
                "breadth_normal_to_risk_min_days": int(min_days),
                "confirm_pool_mean_threshold": float(confirm_threshold),
                "breadth_effective_lag_days": 1,
            }
        )
    return rows


def _build_breadth_frame(start: str, end: str) -> pd.DataFrame:
    cb = _load_cb_daily().copy()
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb = cb[(cb["trade_date"] >= start) & (cb["trade_date"] <= end)].copy()
    cb = cb.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    cb["prev_close"] = cb.groupby("ts_code")["close"].shift(1)
    cb["bond_day_ret"] = cb["close"].astype(float) / cb["prev_close"].astype(float) - 1.0
    grouped = cb.groupby("trade_date")
    frame = grouped.agg(
        n_bonds=("ts_code", "nunique"),
        valid_returns=("bond_day_ret", "count"),
        pool_mean_return=("bond_day_ret", "mean"),
        pool_median_return=("bond_day_ret", "median"),
        min_bond_return=("bond_day_ret", "min"),
        median_bond_return=("bond_day_ret", "median"),
    ).reset_index()
    for threshold in [-0.03, -0.05]:
        col = f"drop_share_{_fmt_float(abs(threshold))}"
        frame[col] = grouped["bond_day_ret"].apply(
            lambda s, threshold=threshold: float((s <= threshold).mean())
        ).to_numpy()
    frame["missing_return_ratio"] = 1.0 - frame["valid_returns"] / frame["n_bonds"]
    return frame.sort_values("trade_date").reset_index(drop=True)


def _effective_dates_for_candidate(
    breadth: pd.DataFrame,
    candidate: dict[str, Any],
    trading_days: list[str],
    start: str,
    end: str,
) -> set[str]:
    threshold_col = f"drop_share_{_fmt_float(abs(float(candidate['breadth_drop_threshold'])))}"
    breadth_raw = breadth[threshold_col].astype(float) >= float(candidate["breadth_panic_ratio"])
    confirm_raw = breadth["pool_mean_return"].astype(float) <= float(candidate["confirm_pool_mean_threshold"])
    raw = breadth_raw & confirm_raw
    consecutive: list[int] = []
    count = 0
    for value in raw:
        count = count + 1 if bool(value) else 0
        consecutive.append(count)
    signal_mask = raw & (pd.Series(consecutive, index=breadth.index) >= int(candidate["breadth_normal_to_risk_min_days"]))
    signal_dates = breadth.loc[signal_mask, "trade_date"].astype(str).tolist()
    day_index = {day: idx for idx, day in enumerate(trading_days)}
    lag = int(candidate.get("breadth_effective_lag_days", 1))
    risk_dates: set[str] = set()
    for signal_date in signal_dates:
        idx = day_index.get(signal_date)
        if idx is None:
            continue
        effective_idx = idx + lag
        if 0 <= effective_idx < len(trading_days):
            effective_date = trading_days[effective_idx]
            if start <= effective_date <= end:
                risk_dates.add(effective_date)
    return risk_dates


def _candidate_summary_row(
    candidate: dict[str, Any],
    metrics_by_year: dict[int, dict[str, Any]],
    years: list[int],
) -> dict[str, Any]:
    row = {"name": candidate["name"], "kind": "candidate", **candidate}
    for year in years:
        metrics = metrics_by_year[year]
        row[f"y{year}_excess"] = metrics["excess_return"]
        row[f"y{year}_total"] = metrics["total_return"]
        row[f"y{year}_dd"] = metrics["max_drawdown"]
        row[f"y{year}_trades"] = metrics["total_trades"]
        row[f"y{year}_risk_days"] = metrics["risk_days"]
    row["main_floor_pass_count"] = sum(
        1
        for year, floor in MAIN_FLOORS.items()
        if year in metrics_by_year and float(metrics_by_year[year]["excess_return"]) >= floor - 1e-12
    )
    applicable_floors = sum(1 for year in MAIN_FLOORS if year in metrics_by_year)
    row["passes_main_floors"] = int(
        applicable_floors > 0 and row["main_floor_pass_count"] == applicable_floors
    )
    row["params_json"] = json.dumps(candidate, sort_keys=True)
    return row


def _rank_for_leave_year(
    base_row: dict[str, Any],
    leave_year: int,
    years: list[int],
) -> dict[str, Any]:
    selection_years = [year for year in years if year != leave_year] or list(years)
    row = dict(base_row)
    row["leave_year"] = leave_year
    selection_excess = [float(row[f"y{year}_excess"]) for year in selection_years]
    selection_dd = [abs(float(row[f"y{year}_dd"])) for year in selection_years]
    avg_excess = sum(selection_excess) / len(selection_excess)
    avg_dd = sum(selection_dd) / len(selection_dd)
    floor_penalty = 0.0
    for year, floor in MAIN_FLOORS.items():
        if year not in years:
            continue
        miss = floor - float(row[f"y{year}_excess"])
        if miss > 0:
            floor_penalty += 10.0 * miss
    row["selection_avg_excess"] = round(avg_excess, 6)
    row["selection_avg_abs_dd"] = round(avg_dd, 6)
    row["selection_score"] = round(avg_excess - 0.25 * avg_dd - floor_penalty, 6)
    row[f"replay_{leave_year}_excess"] = row[f"y{leave_year}_excess"]
    row[f"replay_{leave_year}_total"] = row[f"y{leave_year}_total"]
    row[f"replay_{leave_year}_trades"] = row[f"y{leave_year}_trades"]
    return row


def _baseline_detail_rows(
    baseline_results: dict[int, dict[str, Any]],
    benchmark_daily: pd.Series,
    years: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trade_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    for year in years:
        result = baseline_results[year]
        for trade in result["trades"]:
            trade_rows.append(
                {
                    "candidate": "medium_baseline",
                    "leave_year": "",
                    "year": year,
                    **trade,
                }
            )
        daily = _daily_frame(result["equity_curve"], benchmark_daily, 1)
        for drow in daily.itertuples(index=False):
            equity_rows.append(
                {
                    "candidate": "medium_baseline",
                    "leave_year": "",
                    "year": year,
                    "date": drow.date,
                    "equity": round(float(drow.equity), 6),
                    "strategy_return": round(float(drow.strategy_return), 10),
                    "benchmark_return": round(float(drow.benchmark_return), 10),
                    "daily_excess": round(float(drow.daily_excess), 10),
                    "risk_state": 0,
                }
            )
    return trade_rows, equity_rows


def _detail_rows_for_selected(
    ranks: pd.DataFrame,
    data_root: Path,
    fixed_source: int,
    rule: str,
    panic_file: Path,
    benchmark_daily: pd.Series,
    selected: list[dict[str, Any]],
    candidates_by_name: dict[str, dict[str, Any]],
    risk_dates_by_key: dict[tuple[int, int], set[str]],
    breadth: pd.DataFrame,
    years: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trade_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    trigger_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    breadth_by_date = {str(row.trade_date): row for row in breadth.itertuples(index=False)}
    trading_days = breadth["trade_date"].astype(str).tolist()
    day_index = {day: idx for idx, day in enumerate(trading_days)}
    for row in selected:
        candidate_key = str(row["name"])
        candidate = candidates_by_name[candidate_key]
        params = _medium_params(
            panic_file,
            int(candidate["breadth_risk_recovery_days"]),
            float(candidate["breadth_risk_switch_hurdle_pct"]),
        )
        leave_year = int(row["leave_year"])
        threshold_col = f"drop_share_{_fmt_float(abs(float(candidate['breadth_drop_threshold'])))}"
        for year in years:
            key = (candidate_key, year)
            if key in seen:
                continue
            seen.add(key)
            risk_dates = risk_dates_by_key[(int(row["_candidate_idx"]), year)]
            result = _run_year(ranks, data_root, fixed_source, rule, year, params, risk_dates)
            for trade in result["trades"]:
                trade_rows.append(
                    {
                        "candidate": candidate_key,
                        "leave_year": leave_year,
                        "year": year,
                        **trade,
                    }
                )
            detail = _daily_frame(result["equity_curve"], benchmark_daily, 1)
            detail["risk_state"] = detail["date"].isin(risk_dates).astype(int)
            for drow in detail.itertuples(index=False):
                brow = breadth_by_date.get(str(drow.date))
                equity_rows.append(
                    {
                        "candidate": candidate_key,
                        "leave_year": leave_year,
                        "year": year,
                        "date": drow.date,
                        "equity": round(float(drow.equity), 6),
                        "strategy_return": round(float(drow.strategy_return), 10),
                        "benchmark_return": round(float(drow.benchmark_return), 10),
                        "daily_excess": round(float(drow.daily_excess), 10),
                        "risk_state": int(drow.risk_state),
                        "n_bonds": "" if brow is None else int(brow.n_bonds),
                        "drop_share": "" if brow is None else round(float(getattr(brow, threshold_col)), 10),
                        "pool_mean_return": "" if brow is None else round(float(brow.pool_mean_return), 10),
                        "confirm_hit": (
                            ""
                            if brow is None
                            else int(float(brow.pool_mean_return) <= float(candidate["confirm_pool_mean_threshold"]))
                        ),
                    }
                )
            prev_risk = False
            for risk_date in [d for d in trading_days if f"{year}0101" <= d <= f"{year}1231"]:
                risk = risk_date in risk_dates
                if risk and not prev_risk:
                    signal_idx = day_index[risk_date] - int(candidate["breadth_effective_lag_days"])
                    signal_date = trading_days[signal_idx] if signal_idx >= 0 else ""
                    brow = breadth_by_date.get(signal_date)
                    trigger_rows.append(
                        {
                            "candidate": candidate_key,
                            "leave_year": leave_year,
                            "year": year,
                            "signal_date": signal_date,
                            "effective_date": risk_date,
                            "breadth_drop_threshold": candidate["breadth_drop_threshold"],
                            "breadth_panic_ratio": candidate["breadth_panic_ratio"],
                            "breadth_normal_to_risk_min_days": candidate["breadth_normal_to_risk_min_days"],
                            "confirm_pool_mean_threshold": candidate["confirm_pool_mean_threshold"],
                            "breadth_effective_lag_days": candidate["breadth_effective_lag_days"],
                            "n_bonds": "" if brow is None else int(brow.n_bonds),
                            "valid_returns": "" if brow is None else int(brow.valid_returns),
                            "drop_share": "" if brow is None else round(float(getattr(brow, threshold_col)), 10),
                            "pool_mean_return": "" if brow is None else round(float(brow.pool_mean_return), 10),
                            "confirm_hit": (
                                ""
                                if brow is None
                                else int(float(brow.pool_mean_return) <= float(candidate["confirm_pool_mean_threshold"]))
                            ),
                            "missing_return_ratio": "" if brow is None else round(float(brow.missing_return_ratio), 10),
                        }
                    )
                prev_risk = risk
    return trade_rows, equity_rows, trigger_rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/cb_arb_concurrent_supervised_20260511_094500"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_breadth_confirm_ensemble_2026-05-15"),
    )
    parser.add_argument(
        "--benchmark-csv",
        type=Path,
        default=Path(
            "data/cb_arb_concurrent_supervised_20260511_094500/"
            "phase_loss_review/current_2019_2024_equity_vs_benchmark.csv"
        ),
    )
    parser.add_argument("--panic-file", type=Path, default=None)
    parser.add_argument("--ranks-path", type=Path, default=None)
    parser.add_argument("--years", default="2019,2020,2021,2022,2023,2024")
    parser.add_argument("--fixed-source", type=int, default=2)
    parser.add_argument("--rule", default="score_4state")
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--limit-candidates", type=int, default=0)
    parser.add_argument("--skip-detail-export", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    panic_file = args.panic_file or args.data_root / "panic_detector_training/panic_detector_trained_daily.csv"
    ranks_path = args.ranks_path or _default_ranks_path(args.data_root)
    years = _parse_int_list(args.years)
    if not years:
        raise SystemExit("--years cannot be empty")

    start = f"{min(years)}0101"
    end = f"{max(years)}1231"
    benchmark_daily = _load_benchmark_daily(args.benchmark_csv)
    ranks = _load_or_build_value_ranks(
        args.data_root,
        start,
        end,
        args.fixed_source,
        args.rule,
        ranks_path,
        True,
    )
    breadth = _build_breadth_frame(start, end)
    breadth.to_csv(args.output_dir / "breadth_daily.csv", index=False)
    pool_cols = [
        "trade_date",
        "n_bonds",
        "valid_returns",
        "pool_mean_return",
        "pool_median_return",
        "missing_return_ratio",
    ]
    breadth[pool_cols].to_csv(args.output_dir / "pool_mean_daily.csv", index=False)
    trading_days = breadth["trade_date"].astype(str).tolist()

    baseline_params = _medium_params(panic_file, 4, 0.15)
    baseline_metrics: dict[int, dict[str, Any]] = {}
    baseline_results: dict[int, dict[str, Any]] = {}
    print(f"[breadth-confirm] deriving baseline years={years}", flush=True)
    for year in years:
        result = _run_year(
            ranks,
            args.data_root,
            args.fixed_source,
            args.rule,
            year,
            baseline_params,
            None,
        )
        baseline_results[year] = result
        baseline_metrics[year] = dict(result["metrics"])
        print(
            f"[breadth-confirm] baseline year={year} "
            f"excess={baseline_metrics[year]['excess_return']}",
            flush=True,
        )

    candidates = _candidate_grid()
    if args.limit_candidates > 0:
        candidates = candidates[: args.limit_candidates]
    candidates_by_name = {str(candidate["name"]): candidate for candidate in candidates}
    candidate_params = [
        _medium_params(
            panic_file,
            int(candidate["breadth_risk_recovery_days"]),
            float(candidate["breadth_risk_switch_hurdle_pct"]),
        )
        for candidate in candidates
    ]
    risk_dates_by_idx_year: dict[tuple[int, int], set[str]] = {}
    for idx, candidate in enumerate(candidates):
        for year in years:
            y_start, y_end = _annual_window(year)
            risk_dates_by_idx_year[(idx, year)] = _effective_dates_for_candidate(
                breadth,
                candidate,
                trading_days,
                y_start,
                y_end,
            )

    candidate_metrics: dict[int, dict[int, dict[str, Any]]] = {
        idx: {} for idx in range(len(candidates))
    }
    total_tasks = len(candidates) * len(years)
    print(
        f"[breadth-confirm] candidates={len(candidates)} years={len(years)} "
        f"tasks={total_tasks} workers={max(1, args.max_workers)}",
        flush=True,
    )
    with ProcessPoolExecutor(
        max_workers=max(1, args.max_workers),
        initializer=_init_worker,
        initargs=(ranks, args.data_root, args.fixed_source, args.rule),
    ) as executor:
        futures = []
        for idx, candidate in enumerate(candidates):
            for year in years:
                futures.append(
                    executor.submit(
                        _evaluate_candidate_year,
                        idx,
                        candidate,
                        year,
                        candidate_params[idx],
                        risk_dates_by_idx_year[(idx, year)],
                    )
                )
        for done, fut in enumerate(as_completed(futures), 1):
            idx, year, metrics = fut.result()
            candidate_metrics[idx][year] = metrics
            if done % max(1, len(years) * max(1, args.max_workers)) == 0 or done == total_tasks:
                print(f"[breadth-confirm] tasks_done={done}/{total_tasks}", flush=True)

    summary_rows = _baseline_rows(baseline_metrics, years)
    candidate_summary_rows: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates):
        row = _candidate_summary_row(candidate, candidate_metrics[idx], years)
        row["_candidate_idx"] = idx
        candidate_summary_rows.append(row)
    summary_rows.extend(candidate_summary_rows)

    ranked_rows: list[dict[str, Any]] = []
    selected_rows: list[dict[str, Any]] = []
    for leave_year in years:
        rows = [_rank_for_leave_year(row, leave_year, years) for row in candidate_summary_rows]
        rows.sort(
            key=lambda r: (
                int(r["passes_main_floors"]),
                float(r["selection_score"]),
                float(r["selection_avg_excess"]),
            ),
            reverse=True,
        )
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
            row["selected_for_leave"] = int(rank == 1)
            baseline_excess = float(baseline_metrics[leave_year]["excess_return"])
            row["holdout_baseline_excess"] = baseline_excess
            row["holdout_improved_or_equal"] = int(
                float(row[f"replay_{leave_year}_excess"]) >= baseline_excess - 1e-12
            )
            ranked_rows.append(row)
        selected_rows.append(rows[0])
        print(
            f"[breadth-confirm] leave={leave_year} top={rows[0]['name']} "
            f"score={rows[0]['selection_score']} "
            f"holdout={rows[0][f'replay_{leave_year}_excess']} "
            f"baseline={baseline_metrics[leave_year]['excess_return']}",
            flush=True,
        )

    selection_passes = sum(int(row["holdout_improved_or_equal"]) for row in selected_rows)
    adoption_row = {
        "name": "cv_selected_top1",
        "kind": "cv_summary",
        "selected_passes": selection_passes,
        "selected_total": len(selected_rows),
        "adoption_pass": int(selection_passes >= 5),
    }
    summary_rows.append(adoption_row)

    _write_rows(args.output_dir / "summary.csv", summary_rows)
    _write_rows(args.output_dir / "ranked.csv", ranked_rows)
    _write_rows(args.output_dir / "selected.csv", selected_rows)

    if not args.skip_detail_export:
        baseline_trades, baseline_equity = _baseline_detail_rows(
            baseline_results,
            benchmark_daily,
            years,
        )
        trades, daily_equity, triggers = _detail_rows_for_selected(
            ranks,
            args.data_root,
            args.fixed_source,
            args.rule,
            panic_file,
            benchmark_daily,
            selected_rows,
            candidates_by_name,
            risk_dates_by_idx_year,
            breadth,
            years,
        )
        _write_rows(args.output_dir / "trades.csv", baseline_trades + trades)
        _write_rows(args.output_dir / "daily_equity.csv", baseline_equity + daily_equity)
        _write_rows(args.output_dir / "trigger_dates.csv", triggers)

    run_summary = {
        "years": years,
        "candidate_count": len(candidates),
        "task_count": total_tasks,
        "main_floors": MAIN_FLOORS,
        "breadth_effective_lag_days": 1,
        "confirm_signal": "pool_mean_return",
        "selected_passes": selection_passes,
        "selected_total": len(selected_rows),
        "adoption_pass": selection_passes >= 5,
    }
    (args.output_dir / "run_summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[breadth-confirm] summary", json.dumps(run_summary, ensure_ascii=False), flush=True)
    print(f"[breadth-confirm] wrote {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
