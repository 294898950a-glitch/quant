"""Search panic opportunity triggers while leaving one year out of selection."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_panic_option_weight import BASE, CONFIGS  # noqa: E402
from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _build_panic_opportunity_dates,
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
    _score,
)

_WORKER_RANKS = None
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


def _params(panic_file: Path, mode: str = "strong", overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    mid_weight = next(c for c in CONFIGS if c["name"] == "weight_mid_100_70_40_10")
    params = {**BASE, **dict(mid_weight["params"])}
    params.update(
        {
            "panic_dates_file": str(panic_file),
            "panic_signal_column": "panic_day_trained",
            "panic_effective_lag_days": 1,
            "panic_option_value_weight_scope": "triggered_revalue",
        }
    )
    if mode:
        params.update(
            {
                "panic_opportunity_protect_enabled": 1.0,
                "panic_opportunity_protect_days": 20,
                "panic_opportunity_bad_days": 5,
                "panic_opportunity_switch_hurdle_pct": 0.12,
                "panic_opportunity_exit_on_recovery_enabled": 1.0,
                "panic_opportunity_recovery_days": 2,
                "panic_opportunity_trigger_mode": mode,
            }
        )
    if overrides:
        params.update(overrides)
    return params


def _run_period(
    ranks,
    data_root: Path,
    fixed_source: int,
    rule: str,
    params: dict[str, Any],
    start: str,
    end: str,
) -> dict[str, Any]:
    result = _run_value_gap_backtest(
        ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
        start,
        end,
        data_root,
        fixed_source,
        rule,
        params,
    )
    metrics = result["metrics"]
    return {
        "total": metrics["total_return"],
        "excess": metrics["excess_return"],
        "dd": metrics["max_drawdown"],
        "trades": metrics["total_trades"],
        "win_rate": metrics["win_rate"],
        "score": _score(metrics),
    }


def _init_worker(ranks, data_root: Path, fixed_source: int, rule: str) -> None:
    global _WORKER_RANKS, _WORKER_DATA_ROOT, _WORKER_FIXED_SOURCE, _WORKER_RULE
    _WORKER_RANKS = ranks
    _WORKER_DATA_ROOT = data_root
    _WORKER_FIXED_SOURCE = fixed_source
    _WORKER_RULE = rule


def _candidate_base_row(candidate: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in candidate.items() if k not in {"params"}}
    row["kind"] = "candidate"
    return row


def _evaluate_candidate_period(
    idx: int,
    candidate: dict[str, Any],
    period_kind: str,
    year: int,
) -> tuple[int, str, int, dict[str, Any]]:
    if (
        _WORKER_RANKS is None
        or _WORKER_DATA_ROOT is None
        or _WORKER_FIXED_SOURCE is None
        or _WORKER_RULE is None
    ):
        raise RuntimeError("worker was not initialized")

    params = dict(candidate["params"])
    end = "20260508" if period_kind == "replay" and year == 2026 else f"{year}1231"
    metrics = _run_period(
        _WORKER_RANKS,
        _WORKER_DATA_ROOT,
        _WORKER_FIXED_SOURCE,
        _WORKER_RULE,
        params,
        f"{year}0101",
        end,
    )
    return idx, period_kind, year, metrics


def _finalize_candidate_row(
    candidate: dict[str, Any],
    row: dict[str, Any],
    selection_years: list[int],
    replay_years: list[int],
    best_current_2025: Any,
) -> dict[str, Any]:
    selection_excess = [float(row[f"y{year}_excess"]) for year in selection_years]
    selection_dd = [abs(float(row[f"y{year}_dd"])) for year in selection_years]
    avg_excess = sum(selection_excess) / len(selection_excess)
    avg_dd = sum(selection_dd) / len(selection_dd)
    penalty = 0.0
    if best_current_2025 is not None and float(row.get("replay_2025_excess", 0.0)) < float(best_current_2025) - 1e-9:
        penalty += 10.0 * (float(best_current_2025) - float(row["replay_2025_excess"]))
    row["selection_avg_excess"] = round(avg_excess, 6)
    row["selection_avg_abs_dd"] = round(avg_dd, 6)
    row["selection_score"] = round(avg_excess - 0.25 * avg_dd - penalty, 6)
    row["params_json"] = json.dumps(candidate["params"], sort_keys=True)
    return row


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--panic-file", type=Path, default=None)
    parser.add_argument("--ranks-path", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--leave-year", type=int, default=2020)
    parser.add_argument("--train-years", default="2019,2020,2021,2022,2023,2024")
    parser.add_argument("--replay-years", default="2020,2025")
    parser.add_argument("--fixed-source", type=int, default=2)
    parser.add_argument("--rule", default="score_4state")
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument(
        "--opportunity-recovery-days",
        default="",
        help="Optional comma-separated panic_opportunity_recovery_days grid.",
    )
    parser.add_argument(
        "--opportunity-switch-hurdle-pcts",
        default="",
        help="Optional comma-separated panic_opportunity_switch_hurdle_pct grid.",
    )
    parser.add_argument(
        "--opportunity-grid-mode",
        default="medium",
        choices=("strong", "medium"),
        help="Trigger mode to use for the opportunity recovery/hurdle grid.",
    )
    parser.add_argument(
        "--vol-windows",
        default="",
        help="Optional comma-separated realized-volatility windows for trigger filtering.",
    )
    parser.add_argument(
        "--vol-thresholds",
        default="",
        help="Optional comma-separated rolling daily-return std thresholds for trigger filtering.",
    )
    parser.add_argument(
        "--market-csi-thresholds",
        default="",
        help="Optional comma-separated absolute CSI500 same-day decline thresholds, e.g. 0.005,0.010.",
    )
    parser.add_argument(
        "--market-csi-path",
        default="data/csi500_grid/raw/510500_daily.parquet",
        help="CSI500 ETF daily parquet used by --market-csi-thresholds.",
    )
    parser.add_argument(
        "--market-csi-dating-mode",
        choices=("raw", "effective"),
        default="raw",
        help="Date used for CSI market filtering: raw signal date or lagged effective execution date.",
    )
    parser.add_argument(
        "--market-spy-thresholds",
        default="",
        help="Optional comma-separated absolute 513500/SPY same-day decline thresholds, e.g. 0.010,0.015.",
    )
    parser.add_argument(
        "--market-spy-path",
        default="data/sp500_grid/raw/513500_daily.parquet",
        help="513500/SPY ETF daily parquet used by --market-spy-thresholds.",
    )
    return parser.parse_args()


def _parse_int_list(raw: str) -> list[int]:
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


def _parse_float_list(raw: str) -> list[float]:
    return [float(x.strip()) for x in str(raw).split(",") if x.strip()]


def main() -> int:
    args = _parse_args()
    data_root = args.data_root
    panic_file = args.panic_file or data_root / "panic_detector_training/panic_detector_trained_daily.csv"
    ranks_path = (
        args.ranks_path
        or data_root
        / "value_gap_panic_option_weight_2019_2024_train_2025_2026_test"
        / "daily_value_gap_amounts.parquet"
    )
    output_dir = args.output_dir or data_root / f"panic_leave_{args.leave_year}_out_search"
    output_dir.mkdir(parents=True, exist_ok=True)

    train_years = [int(y.strip()) for y in str(args.train_years).split(",") if y.strip()]
    selection_years = [y for y in train_years if y != args.leave_year]
    replay_years = [int(y.strip()) for y in str(args.replay_years).split(",") if y.strip()]
    all_years = sorted(set(train_years + replay_years))

    ranks = _load_or_build_value_ranks(
        data_root,
        f"{min(all_years)}0101",
        "20260508" if max(all_years) >= 2026 else f"{max(all_years)}1231",
        args.fixed_source,
        args.rule,
        ranks_path,
        True,
    )

    threshold_grid = itertools.product(
        [-0.030, -0.028, -0.026, -0.024],
        [-0.018, -0.016, -0.014],
        [0.20, 0.25, 0.30],
        [-0.040, -0.035, -0.030],
        [-0.040, -0.035, -0.030],
        [0.25, 0.30, 0.35],
    )

    base = _params(panic_file, "strong")
    seen_dates: set[tuple[str, ...]] = set()
    candidates: list[dict[str, Any]] = []
    vol_windows = _parse_int_list(args.vol_windows)
    vol_thresholds = _parse_float_list(args.vol_thresholds)
    opportunity_recovery_days = _parse_int_list(args.opportunity_recovery_days)
    opportunity_switch_hurdles = _parse_float_list(args.opportunity_switch_hurdle_pcts)
    market_csi_thresholds = _parse_float_list(args.market_csi_thresholds)
    market_spy_thresholds = _parse_float_list(args.market_spy_thresholds)
    csi_spy_combo_only = bool(market_csi_thresholds and market_spy_thresholds)
    csi_vol_combo_only = bool(market_csi_thresholds and vol_windows and vol_thresholds and not market_spy_thresholds)
    base_candidates: list[dict[str, Any]] = []

    def add_candidate(
        name: str,
        candidate_overrides: dict[str, Any],
        params: dict[str, Any],
        dates: set[str],
    ) -> None:
        candidates.append(
            {
                "name": name,
                **candidate_overrides,
                "params": params,
                "count_leave_year": sum(1 for d in dates if d.startswith(str(args.leave_year))),
                "count_2025": sum(1 for d in dates if d.startswith("2025")),
            }
        )

    if opportunity_recovery_days or opportunity_switch_hurdles:
        if not (opportunity_recovery_days and opportunity_switch_hurdles):
            raise ValueError(
                "--opportunity-recovery-days and --opportunity-switch-hurdle-pcts must be supplied together"
            )
        for recovery_days, switch_hurdle in itertools.product(
            opportunity_recovery_days,
            opportunity_switch_hurdles,
        ):
            overrides = {
                "panic_opportunity_recovery_days": int(recovery_days),
                "panic_opportunity_switch_hurdle_pct": float(switch_hurdle),
            }
            params = _params(panic_file, args.opportunity_grid_mode, overrides)
            dates = _build_panic_opportunity_dates("20190101", "20260508", params)
            name = f"{args.opportunity_grid_mode}_recovery{recovery_days}_hurdle{switch_hurdle:.2f}".replace(
                ".", "p"
            )
            add_candidate(name, overrides, params, dates)
    else:
        for hard, shock_ret, shock_breadth, trend5, trend20, trend_breadth in threshold_grid:
            overrides = {
                "panic_opportunity_strong_day_ret": hard,
                "panic_opportunity_shock_day_ret": shock_ret,
                "panic_opportunity_shock_breadth1": shock_breadth,
                "panic_opportunity_trend_ret5": trend5,
                "panic_opportunity_trend_ret20": trend20,
                "panic_opportunity_trend_breadth20": trend_breadth,
            }
            params = {**base, **overrides}
            dates = _build_panic_opportunity_dates("20190101", "20260508", params)
            key = tuple(sorted(dates))
            if key not in seen_dates:
                seen_dates.add(key)
                name = (
                    f"loo_h{abs(hard):.3f}_s{abs(shock_ret):.3f}_b{shock_breadth:.2f}_"
                    f"t5{abs(trend5):.3f}_t20{abs(trend20):.3f}_tb{trend_breadth:.2f}"
                ).replace(".", "p")
                base_candidate = {
                    "name": name,
                    "overrides": overrides,
                    "params": params,
                    "dates": dates,
                }
                base_candidates.append(base_candidate)
                if not (csi_spy_combo_only or csi_vol_combo_only):
                    add_candidate(name, overrides, params, dates)

            if vol_windows and vol_thresholds and not csi_vol_combo_only:
                for vol_window, vol_threshold in itertools.product(vol_windows, vol_thresholds):
                    filter_overrides = {
                        "panic_opportunity_realized_vol_filter_enabled": 1.0,
                        "panic_opportunity_realized_vol_window": float(vol_window),
                        "panic_opportunity_realized_vol_threshold": vol_threshold,
                    }
                    candidate_overrides = {**overrides, **filter_overrides}
                    params = {**base, **candidate_overrides}
                    dates = _build_panic_opportunity_dates("20190101", "20260508", params)
                    key = tuple(sorted(dates))
                    if key in seen_dates:
                        continue
                    seen_dates.add(key)
                    name_suffix = f"_v{vol_window}_{vol_threshold:.3f}".replace(".", "p")
                    name = (
                        f"loo_h{abs(hard):.3f}_s{abs(shock_ret):.3f}_b{shock_breadth:.2f}_"
                        f"t5{abs(trend5):.3f}_t20{abs(trend20):.3f}_tb{trend_breadth:.2f}"
                        f"{name_suffix}"
                    ).replace(".", "p")
                    add_candidate(name, candidate_overrides, params, dates)

    for base_candidate in base_candidates:
        if market_csi_thresholds and not (csi_spy_combo_only or csi_vol_combo_only):
            for threshold in market_csi_thresholds:
                csi_threshold = -abs(float(threshold))
                filter_overrides = {
                    "panic_market_filter_enabled": 1.0,
                    "panic_market_filter_csi_path": str(args.market_csi_path),
                    "panic_market_filter_csi_threshold": csi_threshold,
                    "panic_market_filter_csi_dating_mode": str(args.market_csi_dating_mode),
                }
                candidate_overrides = {**base_candidate["overrides"], **filter_overrides}
                params = {**base, **candidate_overrides}
                dates = _build_panic_opportunity_dates("20190101", "20260508", params)
                name_suffix = f"_mcsi{abs(csi_threshold):.3f}".replace(".", "p")
                add_candidate(
                    f"{base_candidate['name']}{name_suffix}",
                    candidate_overrides,
                    params,
                    dates,
                )
        if market_spy_thresholds and not csi_spy_combo_only:
            for threshold in market_spy_thresholds:
                spy_threshold = -abs(float(threshold))
                filter_overrides = {
                    "panic_market_filter_spy_enabled": 1.0,
                    "panic_market_filter_spy_path": str(args.market_spy_path),
                    "panic_market_filter_spy_threshold": spy_threshold,
                }
                candidate_overrides = {**base_candidate["overrides"], **filter_overrides}
                params = {**base, **candidate_overrides}
                dates = _build_panic_opportunity_dates("20190101", "20260508", params)
                name_suffix = f"_mspy{abs(spy_threshold):.3f}".replace(".", "p")
                add_candidate(
                    f"{base_candidate['name']}{name_suffix}",
                    candidate_overrides,
                    params,
                    dates,
                )
        if csi_spy_combo_only:
            for csi_threshold_raw, spy_threshold_raw in itertools.product(
                market_csi_thresholds,
                market_spy_thresholds,
            ):
                csi_threshold = -abs(float(csi_threshold_raw))
                spy_threshold = -abs(float(spy_threshold_raw))
                filter_overrides = {
                    "panic_market_filter_enabled": 1.0,
                    "panic_market_filter_csi_path": str(args.market_csi_path),
                    "panic_market_filter_csi_threshold": csi_threshold,
                    "panic_market_filter_csi_dating_mode": str(args.market_csi_dating_mode),
                    "panic_market_filter_spy_enabled": 1.0,
                    "panic_market_filter_spy_path": str(args.market_spy_path),
                    "panic_market_filter_spy_threshold": spy_threshold,
                }
                candidate_overrides = {**base_candidate["overrides"], **filter_overrides}
                params = {**base, **candidate_overrides}
                dates = _build_panic_opportunity_dates("20190101", "20260508", params)
                name_suffix = f"_mcsi{abs(csi_threshold):.3f}_mspy{abs(spy_threshold):.3f}".replace(".", "p")
                add_candidate(
                    f"{base_candidate['name']}{name_suffix}",
                    candidate_overrides,
                    params,
                    dates,
                )
        if csi_vol_combo_only:
            for vol_window, vol_threshold, csi_threshold_raw in itertools.product(
                vol_windows,
                vol_thresholds,
                market_csi_thresholds,
            ):
                csi_threshold = -abs(float(csi_threshold_raw))
                filter_overrides = {
                    "panic_opportunity_realized_vol_filter_enabled": 1.0,
                    "panic_opportunity_realized_vol_window": float(vol_window),
                    "panic_opportunity_realized_vol_threshold": vol_threshold,
                    "panic_market_filter_enabled": 1.0,
                    "panic_market_filter_csi_path": str(args.market_csi_path),
                    "panic_market_filter_csi_threshold": csi_threshold,
                    "panic_market_filter_csi_dating_mode": str(args.market_csi_dating_mode),
                }
                candidate_overrides = {**base_candidate["overrides"], **filter_overrides}
                params = {**base, **candidate_overrides}
                dates = _build_panic_opportunity_dates("20190101", "20260508", params)
                name_suffix = (
                    f"_v{vol_window}_{vol_threshold:.3f}_mcsi{abs(csi_threshold):.3f}"
                ).replace(".", "p")
                add_candidate(
                    f"{base_candidate['name']}{name_suffix}",
                    candidate_overrides,
                    params,
                    dates,
                )

    rows: list[dict[str, Any]] = []
    baselines = [
        ("current_best_no_opportunity", _params(panic_file, "")),
        ("strong_opportunity", _params(panic_file, "strong")),
        ("medium_opportunity", _params(panic_file, "medium")),
    ]
    for name, params in baselines:
        row: dict[str, Any] = {"name": name, "kind": "baseline"}
        selection_excess = []
        for year in selection_years:
            metrics = _run_period(
                ranks, data_root, args.fixed_source, args.rule, params, f"{year}0101", f"{year}1231"
            )
            row[f"y{year}_excess"] = metrics["excess"]
            selection_excess.append(float(metrics["excess"]))
        for year in replay_years:
            end = "20260508" if year == 2026 else f"{year}1231"
            metrics = _run_period(
                ranks, data_root, args.fixed_source, args.rule, params, f"{year}0101", end
            )
            row[f"replay_{year}_excess"] = metrics["excess"]
        row["selection_avg_excess"] = round(sum(selection_excess) / len(selection_excess), 6)
        row["selection_score"] = ""
        rows.append(row)
        print(f"[leave-year] baseline {name} selection_avg={row['selection_avg_excess']}", flush=True)

    best_current_2025 = next(
        r for r in rows if r["name"] == "current_best_no_opportunity"
    ).get("replay_2025_excess")
    periods = [("selection", year) for year in selection_years] + [("replay", year) for year in replay_years]
    total_tasks = len(candidates) * len(periods)
    print(
        f"[leave-year] candidates={len(candidates)} periods={len(periods)} "
        f"tasks={total_tasks} workers={max(1, args.max_workers)}",
        flush=True,
    )
    candidate_rows = {idx: _candidate_base_row(candidate) for idx, candidate in enumerate(candidates, 1)}
    completed_by_candidate = {idx: 0 for idx in candidate_rows}
    with ProcessPoolExecutor(
        max_workers=max(1, args.max_workers),
        initializer=_init_worker,
        initargs=(ranks, data_root, args.fixed_source, args.rule),
    ) as executor:
        futures = [
            executor.submit(
                _evaluate_candidate_period,
                idx,
                candidate,
                period_kind,
                year,
            )
            for idx, candidate in enumerate(candidates, 1)
            for period_kind, year in periods
        ]
        for done, fut in enumerate(as_completed(futures), 1):
            idx, period_kind, year, metrics = fut.result()
            row = candidate_rows[idx]
            if period_kind == "selection":
                row[f"y{year}_excess"] = metrics["excess"]
                row[f"y{year}_dd"] = metrics["dd"]
            else:
                row[f"replay_{year}_excess"] = metrics["excess"]
                row[f"replay_{year}_total"] = metrics["total"]
                row[f"replay_{year}_trades"] = metrics["trades"]
                row[f"replay_{year}_win_rate"] = metrics["win_rate"]
            completed_by_candidate[idx] += 1
            if completed_by_candidate[idx] == len(periods):
                candidate = candidates[idx - 1]
                row = _finalize_candidate_row(candidate, row, selection_years, replay_years, best_current_2025)
                rows.append(row)
                print(
                    f"[leave-year] done={done}/{total_tasks} candidate={idx} {row['name']} "
                    f"score={row['selection_score']} avg={row['selection_avg_excess']} "
                    f"replay{args.leave_year}={row.get(f'replay_{args.leave_year}_excess')}",
                    flush=True,
                )
            elif done % max(1, len(periods) * max(1, args.max_workers)) == 0:
                print(f"[leave-year] period_tasks_done={done}/{total_tasks}", flush=True)

    _write_rows(output_dir / "leave_year_out_summary.csv", rows)
    ranked = [r for r in rows if r["kind"] == "candidate"]
    ranked.sort(key=lambda r: float(r["selection_score"]), reverse=True)
    _write_rows(output_dir / "leave_year_out_ranked.csv", ranked)
    print("TOP", flush=True)
    for row in ranked[:10]:
        print(
            json.dumps(
                {
                    "name": row["name"],
                    "selection_score": row["selection_score"],
                    "selection_avg_excess": row["selection_avg_excess"],
                    f"replay_{args.leave_year}_excess": row.get(f"replay_{args.leave_year}_excess"),
                    "replay_2025_excess": row.get("replay_2025_excess"),
                    "count_leave_year": row.get("count_leave_year"),
                    "count_2025": row.get("count_2025"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(f"[leave-year] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
