"""Search cb_arb self-PnL regime switches.

The regime signal is derived from the medium baseline daily equity only.  A
candidate classifies date T from rolling excess return ending at T-1, then
feeds those risk dates into the existing value-gap opportunity protection
mechanics with candidate-specific recovery/hurdle parameters.
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

from scripts.evaluate_cb_arb_panic_option_weight import BASE, CONFIGS  # noqa: E402
from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
    _score,
)

MAIN_FLOORS = {
    2020: -0.138588,
    2021: -0.033534,
    2022: 0.028891,
    2023: -0.027744,
}

_WORKER_RANKS: pd.DataFrame | None = None
_WORKER_DATA_ROOT: Path | None = None
_WORKER_FIXED_SOURCE: int | None = None
_WORKER_RULE: str | None = None


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fmt_float(value: float) -> str:
    text = f"{value:.3f}".replace("-", "m").replace(".", "p")
    return text.rstrip("0").rstrip("p")


def _medium_params(
    panic_file: Path,
    recovery_days: int = 4,
    switch_hurdle_pct: float = 0.15,
) -> dict[str, Any]:
    mid_weight = next(c for c in CONFIGS if c["name"] == "weight_mid_100_70_40_10")
    params = {**BASE, **dict(mid_weight["params"])}
    params.update(
        {
            "panic_dates_file": str(panic_file),
            "panic_signal_column": "panic_day_trained",
            "panic_effective_lag_days": 1,
            "panic_option_value_weight_scope": "triggered_revalue",
            "panic_opportunity_protect_enabled": 1.0,
            "panic_opportunity_protect_days": 20,
            "panic_opportunity_bad_days": 5,
            "panic_opportunity_exit_on_recovery_enabled": 1.0,
            "panic_opportunity_recovery_days": int(recovery_days),
            "panic_opportunity_switch_hurdle_pct": float(switch_hurdle_pct),
            "panic_opportunity_trigger_mode": "medium",
        }
    )
    return params


def _candidate_grid() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lookback, threshold, recovery, hurdle in itertools.product(
        [10, 20, 60],
        [-0.02, -0.01, 0.0, 0.01],
        [1, 2, 3],
        [0.05, 0.08, 0.10],
    ):
        rows.append(
            {
                "name": (
                    f"selfpnl_lb{lookback}_thr{_fmt_float(threshold)}_"
                    f"rec{recovery}_h{_fmt_float(hurdle)}"
                ),
                "regime_lookback_days": int(lookback),
                "regime_risk_threshold": float(threshold),
                "regime_risk_recovery_days": int(recovery),
                "regime_risk_switch_hurdle_pct": float(hurdle),
            }
        )
    return rows


def _load_benchmark_daily(path: Path) -> pd.Series:
    frame = pd.read_csv(path, dtype={"date": str})
    required = {"date", "benchmark_return"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"benchmark file missing columns: {sorted(missing)}")
    frame = frame.sort_values("date").reset_index(drop=True)
    cumulative = frame["benchmark_return"].astype(float)
    daily = (1.0 + cumulative) / (1.0 + cumulative.shift(1)) - 1.0
    daily.iloc[0] = 0.0
    return pd.Series(daily.to_numpy(), index=frame["date"].astype(str)).fillna(0.0)


def _daily_frame(
    equity_curve: list[tuple[str, float]],
    benchmark_daily: pd.Series,
    lookback_days: int,
) -> pd.DataFrame:
    frame = pd.DataFrame(equity_curve, columns=["date", "equity"])
    frame["date"] = frame["date"].astype(str)
    frame["strategy_return"] = frame["equity"].astype(float).pct_change().fillna(0.0)
    frame["benchmark_return"] = (
        frame["date"].map(benchmark_daily).astype(float).fillna(0.0)
    )
    frame["daily_excess"] = frame["strategy_return"] - frame["benchmark_return"]
    frame["lookback_excess"] = (
        frame["daily_excess"].shift(1).rolling(lookback_days, min_periods=lookback_days).sum()
    )
    return frame


def _risk_dates(frame: pd.DataFrame, threshold: float) -> set[str]:
    risk = frame["lookback_excess"].notna() & (frame["lookback_excess"].astype(float) < threshold)
    return set(frame.loc[risk, "date"].astype(str).tolist())


def _annual_window(year: int) -> tuple[str, str]:
    return f"{year}0101", f"{year}1231"


def _run_year(
    ranks: pd.DataFrame,
    data_root: Path,
    fixed_source: int,
    rule: str,
    year: int,
    params: dict[str, Any],
    opportunity_dates: set[str] | None = None,
) -> dict[str, Any]:
    start, end = _annual_window(year)
    return _run_value_gap_backtest(
        ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
        start,
        end,
        data_root,
        fixed_source,
        rule,
        params,
        opportunity_dates_override=opportunity_dates,
    )


def _init_worker(ranks: pd.DataFrame, data_root: Path, fixed_source: int, rule: str) -> None:
    global _WORKER_RANKS, _WORKER_DATA_ROOT, _WORKER_FIXED_SOURCE, _WORKER_RULE
    _WORKER_RANKS = ranks
    _WORKER_DATA_ROOT = data_root
    _WORKER_FIXED_SOURCE = fixed_source
    _WORKER_RULE = rule


def _evaluate_candidate_year(
    idx: int,
    candidate: dict[str, Any],
    year: int,
    params: dict[str, Any],
    risk_dates: set[str],
) -> tuple[int, int, dict[str, Any]]:
    if (
        _WORKER_RANKS is None
        or _WORKER_DATA_ROOT is None
        or _WORKER_FIXED_SOURCE is None
        or _WORKER_RULE is None
    ):
        raise RuntimeError("worker was not initialized")
    result = _run_year(
        _WORKER_RANKS,
        _WORKER_DATA_ROOT,
        _WORKER_FIXED_SOURCE,
        _WORKER_RULE,
        year,
        params,
        risk_dates,
    )
    metrics = dict(result["metrics"])
    metrics["score"] = _score(metrics)
    metrics["risk_days"] = len(risk_dates)
    return idx, year, metrics


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
    selection_years = [year for year in years if year != leave_year]
    if not selection_years:
        selection_years = list(years)
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


def _baseline_rows(
    baseline_metrics: dict[int, dict[str, Any]],
    years: list[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leave_year in years:
        selection_years = [year for year in years if year != leave_year]
        if not selection_years:
            selection_years = list(years)
        row: dict[str, Any] = {"name": "medium_baseline", "kind": "baseline", "leave_year": leave_year}
        for year in years:
            metrics = baseline_metrics[year]
            row[f"y{year}_excess"] = metrics["excess_return"]
            row[f"y{year}_total"] = metrics["total_return"]
            row[f"y{year}_dd"] = metrics["max_drawdown"]
            row[f"y{year}_trades"] = metrics["total_trades"]
        row["selection_avg_excess"] = round(
            sum(float(row[f"y{year}_excess"]) for year in selection_years) / len(selection_years),
            6,
        )
        row[f"replay_{leave_year}_excess"] = row[f"y{leave_year}_excess"]
        rows.append(row)
    return rows


def _detail_rows_for_selected(
    ranks: pd.DataFrame,
    data_root: Path,
    fixed_source: int,
    rule: str,
    panic_file: Path,
    benchmark_daily: pd.Series,
    selected: list[dict[str, Any]],
    risk_frames: dict[tuple[int, int], pd.DataFrame],
    years: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trade_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    trigger_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in selected:
        candidate_key = str(row["name"])
        candidate = {
            "name": candidate_key,
            "regime_lookback_days": int(row["regime_lookback_days"]),
            "regime_risk_threshold": float(row["regime_risk_threshold"]),
            "regime_risk_recovery_days": int(row["regime_risk_recovery_days"]),
            "regime_risk_switch_hurdle_pct": float(row["regime_risk_switch_hurdle_pct"]),
        }
        params = _medium_params(
            panic_file,
            int(candidate["regime_risk_recovery_days"]),
            float(candidate["regime_risk_switch_hurdle_pct"]),
        )
        leave_year = int(row["leave_year"])
        for year in years:
            key = (candidate_key, year)
            if key in seen:
                continue
            seen.add(key)
            frame = risk_frames[(int(candidate["regime_lookback_days"]), year)].copy()
            dates = _risk_dates(frame, float(candidate["regime_risk_threshold"]))
            result = _run_year(ranks, data_root, fixed_source, rule, year, params, dates)
            for trade in result["trades"]:
                trade_rows.append(
                    {
                        "candidate": candidate_key,
                        "leave_year": leave_year,
                        "year": year,
                        **trade,
                    }
                )
            detail_frame = _daily_frame(result["equity_curve"], benchmark_daily, int(candidate["regime_lookback_days"]))
            detail_frame["risk_state"] = detail_frame["date"].isin(dates).astype(int)
            for drow in detail_frame.itertuples(index=False):
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
                        "lookback_excess": (
                            ""
                            if pd.isna(drow.lookback_excess)
                            else round(float(drow.lookback_excess), 10)
                        ),
                        "risk_state": int(drow.risk_state),
                    }
                )
            prev_risk = False
            for brow in frame.itertuples(index=False):
                risk = str(brow.date) in dates
                if risk and not prev_risk:
                    trigger_rows.append(
                        {
                            "candidate": candidate_key,
                            "leave_year": leave_year,
                            "year": year,
                            "trigger_date": str(brow.date),
                            "lookback_excess": round(float(brow.lookback_excess), 10),
                            "regime_lookback_days": candidate["regime_lookback_days"],
                            "regime_risk_threshold": candidate["regime_risk_threshold"],
                        }
                    )
                prev_risk = risk
    return trade_rows, equity_rows, trigger_rows


def _parse_int_list(raw: str) -> list[int]:
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


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
        default=Path("data/cb_arb_regime_switch_2026-05-15"),
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


def _default_ranks_path(data_root: Path) -> Path:
    candidates = [
        data_root
        / "value_gap_panic_opportunity_d20_b5_h012_triggered_signal_lag1_2019_2024_train_2025_2026_test"
        / "daily_value_gap_amounts.parquet",
        data_root / "value_gap_switch_eval_2019_2024_train_2025_2026_test" / "daily_value_gap_amounts.parquet",
        data_root / "repair_time_analysis" / "daily_value_gap_amounts.parquet",
        data_root
        / "value_gap_panic_option_weight_2019_2024_train_2025_2026_test"
        / "daily_value_gap_amounts.parquet",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    panic_file = args.panic_file or args.data_root / "panic_detector_training/panic_detector_trained_daily.csv"
    ranks_path = args.ranks_path or _default_ranks_path(args.data_root)
    years = _parse_int_list(args.years)
    if not years:
        raise SystemExit("--years cannot be empty")
    benchmark_daily = _load_benchmark_daily(args.benchmark_csv)
    ranks = _load_or_build_value_ranks(
        args.data_root,
        f"{min(years)}0101",
        f"{max(years)}1231",
        args.fixed_source,
        args.rule,
        ranks_path,
        True,
    )

    baseline_params = _medium_params(panic_file, 4, 0.15)
    baseline_metrics: dict[int, dict[str, Any]] = {}
    baseline_equity: dict[int, list[tuple[str, float]]] = {}
    print(f"[selfpnl-regime] deriving baseline years={years}", flush=True)
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
        baseline_metrics[year] = dict(result["metrics"])
        baseline_equity[year] = result["equity_curve"]
        print(
            f"[selfpnl-regime] baseline year={year} "
            f"excess={baseline_metrics[year]['excess_return']}",
            flush=True,
        )

    risk_frames: dict[tuple[int, int], pd.DataFrame] = {}
    for lookback in [10, 20, 60]:
        for year in years:
            risk_frames[(lookback, year)] = _daily_frame(
                baseline_equity[year],
                benchmark_daily,
                lookback,
            )

    candidates = _candidate_grid()
    if args.limit_candidates > 0:
        candidates = candidates[: args.limit_candidates]

    candidate_params = [
        _medium_params(
            panic_file,
            int(candidate["regime_risk_recovery_days"]),
            float(candidate["regime_risk_switch_hurdle_pct"]),
        )
        for candidate in candidates
    ]
    candidate_metrics: dict[int, dict[int, dict[str, Any]]] = {
        idx: {} for idx in range(len(candidates))
    }

    total_tasks = len(candidates) * len(years)
    print(
        f"[selfpnl-regime] candidates={len(candidates)} years={len(years)} "
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
                frame = risk_frames[(int(candidate["regime_lookback_days"]), year)]
                dates = _risk_dates(frame, float(candidate["regime_risk_threshold"]))
                futures.append(
                    executor.submit(
                        _evaluate_candidate_year,
                        idx,
                        candidate,
                        year,
                        candidate_params[idx],
                        dates,
                    )
                )
        for done, fut in enumerate(as_completed(futures), 1):
            idx, year, metrics = fut.result()
            candidate_metrics[idx][year] = metrics
            if done % max(1, len(years) * max(1, args.max_workers)) == 0 or done == total_tasks:
                print(f"[selfpnl-regime] tasks_done={done}/{total_tasks}", flush=True)

    summary_rows = _baseline_rows(baseline_metrics, years)
    candidate_summary_rows: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates):
        candidate_summary_rows.append(_candidate_summary_row(candidate, candidate_metrics[idx], years))
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
            f"[selfpnl-regime] leave={leave_year} top={rows[0]['name']} "
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
        trades, daily_equity, triggers = _detail_rows_for_selected(
            ranks,
            args.data_root,
            args.fixed_source,
            args.rule,
            panic_file,
            benchmark_daily,
            selected_rows,
            risk_frames,
            years,
        )
        _write_rows(args.output_dir / "trades.csv", trades)
        _write_rows(args.output_dir / "daily_equity.csv", daily_equity)
        _write_rows(args.output_dir / "trigger_dates.csv", triggers)

    run_summary = {
        "years": years,
        "candidate_count": len(candidates),
        "task_count": total_tasks,
        "main_floors": MAIN_FLOORS,
        "selected_passes": selection_passes,
        "selected_total": len(selected_rows),
        "adoption_pass": selection_passes >= 5,
    }
    (args.output_dir / "run_summary.json").write_text(
        json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[selfpnl-regime] summary", json.dumps(run_summary, ensure_ascii=False), flush=True)
    print(f"[selfpnl-regime] wrote {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
