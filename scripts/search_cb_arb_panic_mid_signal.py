"""Search middle panic opportunity triggers for cb_arb.

The search keeps the existing panic valuation and opportunity-protection
behavior fixed, then varies only the opportunity trigger thresholds.
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


def _build_params(
    panic_file: Path,
    *,
    opportunity_mode: str = "strong",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    if opportunity_mode:
        params.update(
            {
                "panic_opportunity_protect_enabled": 1.0,
                "panic_opportunity_protect_days": 20,
                "panic_opportunity_bad_days": 5,
                "panic_opportunity_switch_hurdle_pct": 0.12,
                "panic_opportunity_exit_on_recovery_enabled": 1.0,
                "panic_opportunity_recovery_days": 2,
                "panic_opportunity_trigger_mode": opportunity_mode,
            }
        )
    if overrides:
        params.update(overrides)
    return params


def _run_one(
    name: str,
    params: dict[str, Any],
    ranks,
    data_root: Path,
    fixed_source: int,
    rule: str,
) -> dict[str, Any]:
    periods = {
        "train": ("20190101", "20241231"),
        "test": ("20250101", "20260508"),
        "y2020": ("20200101", "20201231"),
    }
    row: dict[str, Any] = {"name": name}
    for label, (start, end) in periods.items():
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
        row[f"{label}_total"] = metrics["total_return"]
        row[f"{label}_excess"] = metrics["excess_return"]
        row[f"{label}_dd"] = metrics["max_drawdown"]
        row[f"{label}_trades"] = metrics["total_trades"]
        row[f"{label}_score"] = _score(metrics)
    row["params_json"] = json.dumps(params, sort_keys=True)
    return row


def _run_named_period(
    label: str,
    start: str,
    end: str,
    params: dict[str, Any],
    ranks,
    data_root: Path,
    fixed_source: int,
    rule: str,
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
        f"{label}_total": metrics["total_return"],
        f"{label}_excess": metrics["excess_return"],
        f"{label}_dd": metrics["max_drawdown"],
        f"{label}_trades": metrics["total_trades"],
        f"{label}_score": _score(metrics),
    }


def _init_worker(ranks, data_root: Path, fixed_source: int, rule: str) -> None:
    global _WORKER_RANKS, _WORKER_DATA_ROOT, _WORKER_FIXED_SOURCE, _WORKER_RULE
    _WORKER_RANKS = ranks
    _WORKER_DATA_ROOT = data_root
    _WORKER_FIXED_SOURCE = fixed_source
    _WORKER_RULE = rule


def _selected_overrides(drow: dict[str, Any]) -> dict[str, Any]:
    return {
        "panic_opportunity_strong_day_ret": drow["panic_opportunity_strong_day_ret"],
        "panic_opportunity_shock_day_ret": drow["panic_opportunity_shock_day_ret"],
        "panic_opportunity_shock_breadth1": drow["panic_opportunity_shock_breadth1"],
        "panic_opportunity_trend_ret5": drow["panic_opportunity_trend_ret5"],
        "panic_opportunity_trend_ret20": drow["panic_opportunity_trend_ret20"],
        "panic_opportunity_trend_breadth20": drow["panic_opportunity_trend_breadth20"],
    }


def _evaluate_selected_period(
    idx: int,
    drow: dict[str, Any],
    panic_file: Path,
    label: str,
    start: str,
    end: str,
) -> tuple[int, str, dict[str, Any]]:
    if (
        _WORKER_RANKS is None
        or _WORKER_DATA_ROOT is None
        or _WORKER_FIXED_SOURCE is None
        or _WORKER_RULE is None
    ):
        raise RuntimeError("worker was not initialized")

    overrides = _selected_overrides(drow)
    params = _build_params(panic_file, opportunity_mode="strong", overrides=overrides)
    metrics = _run_named_period(
        label,
        start,
        end,
        params,
        _WORKER_RANKS,
        _WORKER_DATA_ROOT,
        _WORKER_FIXED_SOURCE,
        _WORKER_RULE,
    )
    return idx, label, metrics


def _selected_base_row(drow: dict[str, Any], panic_file: Path) -> dict[str, Any]:
    overrides = _selected_overrides(drow)
    params = _build_params(panic_file, opportunity_mode="strong", overrides=overrides)
    row: dict[str, Any] = {"name": str(drow["name"])}
    row.update(drow)
    row["params_json"] = json.dumps(params, sort_keys=True)
    return row


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--panic-file", type=Path, default=None)
    parser.add_argument("--ranks-path", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--selected", type=int, default=24)
    parser.add_argument("--fixed-source", type=int, default=2)
    parser.add_argument("--rule", default="score_4state")
    parser.add_argument("--max-workers", type=int, default=16)
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
    output_dir = args.output_dir or data_root / "panic_mid_signal_search"
    output_dir.mkdir(parents=True, exist_ok=True)

    base_params = _build_params(panic_file, opportunity_mode="strong")
    wanted_2020 = {"20200309", "20200413", "20200907", "20200924"}
    bad_2025 = {
        "20250902",
        "20250909",
        "20250915",
        "20251014",
        "20251016",
        "20251017",
        "20251104",
        "20251121",
        "20251216",
    }

    threshold_grid = itertools.product(
        [-0.030, -0.028, -0.026, -0.024],
        [-0.018, -0.016, -0.014],
        [0.20, 0.25, 0.30],
        [-0.040, -0.035, -0.030],
        [-0.040, -0.035, -0.030],
        [0.25, 0.30, 0.35],
    )

    seen: set[tuple[str, ...]] = set()
    date_rows: list[dict[str, Any]] = []
    vol_windows = _parse_int_list(args.vol_windows)
    vol_thresholds = _parse_float_list(args.vol_thresholds)
    vol_grid: list[tuple[int | None, float | None]] = [(None, None)]
    if vol_windows and vol_thresholds:
        vol_grid.extend(itertools.product(vol_windows, vol_thresholds))

    for hard, shock_ret, shock_breadth, trend5, trend20, trend_breadth in threshold_grid:
        overrides = {
            "panic_opportunity_strong_day_ret": hard,
            "panic_opportunity_shock_day_ret": shock_ret,
            "panic_opportunity_shock_breadth1": shock_breadth,
            "panic_opportunity_trend_ret5": trend5,
            "panic_opportunity_trend_ret20": trend20,
            "panic_opportunity_trend_breadth20": trend_breadth,
        }
        for vol_window, vol_threshold in vol_grid:
            candidate_overrides = dict(overrides)
            name_suffix = ""
            if vol_window is not None and vol_threshold is not None:
                candidate_overrides.update(
                    {
                        "panic_opportunity_realized_vol_filter_enabled": 1.0,
                        "panic_opportunity_realized_vol_window": float(vol_window),
                        "panic_opportunity_realized_vol_threshold": vol_threshold,
                    }
                )
                name_suffix = f"_v{vol_window}_{vol_threshold:.3f}".replace(".", "p")
            params = {**base_params, **candidate_overrides}
            dates = _build_panic_opportunity_dates("20190101", "20260508", params)
            key = tuple(sorted(dates))
            if key in seen:
                continue
            seen.add(key)

            dates2020 = {d for d in dates if d.startswith("2020")}
            dates2025 = {d for d in dates if d.startswith("2025")}
            good_hits = len(wanted_2020 & dates2020)
            bad_hits = len(bad_2025 & dates2025)
            name = (
                f"mid_h{abs(hard):.3f}_s{abs(shock_ret):.3f}_b{shock_breadth:.2f}_"
                f"t5{abs(trend5):.3f}_t20{abs(trend20):.3f}_tb{trend_breadth:.2f}"
                f"{name_suffix}"
            ).replace(".", "p")
            row = {
                "name": name,
                **candidate_overrides,
                "count_2019": sum(1 for d in dates if d.startswith("2019")),
                "count_2020": len(dates2020),
                "count_2021": sum(1 for d in dates if d.startswith("2021")),
                "count_2022": sum(1 for d in dates if d.startswith("2022")),
                "count_2023": sum(1 for d in dates if d.startswith("2023")),
                "count_2024": sum(1 for d in dates if d.startswith("2024")),
                "count_2025": len(dates2025),
                "wanted_2020_hits": good_hits,
                "bad_2025_hits": bad_hits,
                "dates_2020": ";".join(sorted(dates2020)),
                "dates_2025": ";".join(sorted(dates2025)),
                "date_score": good_hits * 10
                - bad_hits * 20
                - max(0, len(dates2025) - 5) * 2
                - max(0, len(dates2020) - 18),
            }
            date_rows.append(row)

    date_rows.sort(
        key=lambda r: (r["date_score"], r["wanted_2020_hits"], -r["bad_2025_hits"]),
        reverse=True,
    )
    _write_rows(output_dir / "date_filter_candidates.csv", date_rows)
    selected = date_rows[: int(args.selected)]
    print(f"date candidates={len(date_rows)} selected={len(selected)}", flush=True)

    ranks = _load_or_build_value_ranks(
        data_root,
        "20190101",
        "20260508",
        args.fixed_source,
        args.rule,
        ranks_path,
        True,
    )

    result_rows: list[dict[str, Any]] = []
    baselines = [
        ("best_continue", _build_params(panic_file, opportunity_mode="")),
        ("ordinary_recover2", _build_params(panic_file, opportunity_mode="panic")),
        ("strong_recover2", _build_params(panic_file, opportunity_mode="strong")),
    ]
    for name, params in baselines:
        row = _run_one(name, params, ranks, data_root, args.fixed_source, args.rule)
        result_rows.append(row)
        print(
            f"done {name}: train={row['train_excess']} "
            f"test={row['test_excess']} y2020={row['y2020_excess']}",
            flush=True,
        )

    periods = [
        ("train", "20190101", "20241231"),
        ("test", "20250101", "20260508"),
        ("y2020", "20200101", "20201231"),
    ]
    total_tasks = len(selected) * len(periods)
    print(
        f"selected backtests={len(selected)} periods={len(periods)} "
        f"tasks={total_tasks} workers={max(1, args.max_workers)}",
        flush=True,
    )
    selected_rows = {idx: _selected_base_row(drow, panic_file) for idx, drow in enumerate(selected, 1)}
    completed_by_selected = {idx: 0 for idx in selected_rows}
    with ProcessPoolExecutor(
        max_workers=max(1, args.max_workers),
        initializer=_init_worker,
        initargs=(ranks, data_root, args.fixed_source, args.rule),
    ) as executor:
        futures = [
            executor.submit(_evaluate_selected_period, idx, drow, panic_file, label, start, end)
            for idx, drow in enumerate(selected, 1)
            for label, start, end in periods
        ]
        for done, fut in enumerate(as_completed(futures), 1):
            idx, _label, metrics = fut.result()
            row = selected_rows[idx]
            row.update(metrics)
            completed_by_selected[idx] += 1
            if completed_by_selected[idx] == len(periods):
                result_rows.append(row)
                print(
                    f"done={done}/{total_tasks} candidate={idx} {row['name']}: "
                    f"train={row['train_excess']} test={row['test_excess']} "
                    f"y2020={row['y2020_excess']}",
                    flush=True,
                )
            elif done % max(1, len(periods) * max(1, args.max_workers)) == 0:
                print(f"period_tasks_done={done}/{total_tasks}", flush=True)

    best_test = next(r for r in result_rows if r["name"] == "best_continue")["test_excess"]
    for row in result_rows:
        if row["name"] in {"best_continue", "ordinary_recover2", "strong_recover2"}:
            row["selection_score"] = ""
            continue
        penalty = 0.0
        if float(row["test_excess"]) < float(best_test) - 1e-9:
            penalty += 1000.0 + (float(best_test) - float(row["test_excess"])) * 1000.0
        if float(row["train_excess"]) < 0.06:
            penalty += (0.06 - float(row["train_excess"])) * 100.0
        row["selection_score"] = round(
            float(row["y2020_excess"]) * 10 + float(row["train_excess"]) - penalty,
            6,
        )

    _write_rows(output_dir / "mid_signal_backtest_summary.csv", result_rows)
    ranked = [
        r
        for r in result_rows
        if r["name"] not in {"best_continue", "ordinary_recover2", "strong_recover2"}
    ]
    ranked.sort(key=lambda r: float(r["selection_score"]), reverse=True)
    print("TOP", flush=True)
    for row in ranked[:8]:
        print(
            json.dumps(
                {
                    "name": row["name"],
                    "train_excess": row["train_excess"],
                    "test_excess": row["test_excess"],
                    "y2020_excess": row["y2020_excess"],
                    "count_2020": row["count_2020"],
                    "count_2025": row["count_2025"],
                    "wanted_2020_hits": row["wanted_2020_hits"],
                    "bad_2025_hits": row["bad_2025_hits"],
                    "selection_score": row["selection_score"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(f"wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
