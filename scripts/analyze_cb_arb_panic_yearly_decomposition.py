"""Yearly decomposition for cb_arb panic opportunity variants."""

from __future__ import annotations

import argparse
import csv
import json
import sys
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


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def _base_params(panic_file: Path) -> dict[str, Any]:
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
    return params


def _opportunity_params(panic_file: Path, mode: str) -> dict[str, Any]:
    params = _base_params(panic_file)
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
    return params


def _exit_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for trade in trades:
        reason = str(trade["exit_reason"])
        prefix = f"exit_{reason}"
        summary[f"{prefix}_count"] = int(summary.get(f"{prefix}_count", 0)) + 1
        summary[f"{prefix}_pnl"] = round(
            float(summary.get(f"{prefix}_pnl", 0.0)) + float(trade["pnl_amount"]),
            2,
        )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--panic-file", type=Path, default=None)
    parser.add_argument("--ranks-path", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--fixed-source", type=int, default=2)
    parser.add_argument("--rule", default="score_4state")
    parser.add_argument("--years", default="2019,2020,2021,2022,2023,2024,2025")
    return parser.parse_args()


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
    output_dir = args.output_dir or data_root / "panic_yearly_decomposition"
    output_dir.mkdir(parents=True, exist_ok=True)

    years = [int(y.strip()) for y in str(args.years).split(",") if y.strip()]
    start_all = f"{min(years)}0101"
    end_all = "20260508" if max(years) >= 2026 else f"{max(years)}1231"
    if max(years) == 2025:
        end_all = "20251231"

    ranks = _load_or_build_value_ranks(
        data_root,
        start_all,
        end_all,
        args.fixed_source,
        args.rule,
        ranks_path,
        True,
    )

    variants = [
        ("current_best_no_opportunity", _base_params(panic_file)),
        ("ordinary_opportunity", _opportunity_params(panic_file, "panic")),
        ("strong_opportunity", _opportunity_params(panic_file, "strong")),
        ("medium_opportunity", _opportunity_params(panic_file, "medium")),
    ]

    rows: list[dict[str, Any]] = []
    exit_rows: list[dict[str, Any]] = []
    for year in years:
        start = f"{year}0101"
        end = "20260508" if year == 2026 else f"{year}1231"
        for name, params in variants:
            result = _run_value_gap_backtest(
                ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)],
                start,
                end,
                data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            opportunity_dates = (
                _build_panic_opportunity_dates(start, end, params)
                if float(params.get("panic_opportunity_protect_enabled", 0.0)) > 0
                else set()
            )
            metrics = result["metrics"]
            row = {
                "variant": name,
                "year": year,
                "start": start,
                "end": end,
                "total_return": metrics["total_return"],
                "excess_return": metrics["excess_return"],
                "max_drawdown": metrics["max_drawdown"],
                "win_rate": metrics["win_rate"],
                "total_trades": metrics["total_trades"],
                "score": _score(metrics),
                "opportunity_dates": len(opportunity_dates),
                "params_json": json.dumps(params, sort_keys=True),
            }
            row.update(_exit_summary(result["trades"]))
            rows.append(row)
            for reason_key, value in sorted(row.items()):
                if reason_key.startswith("exit_") and reason_key.endswith("_count"):
                    reason = reason_key[len("exit_") : -len("_count")]
                    exit_rows.append(
                        {
                            "variant": name,
                            "year": year,
                            "exit_reason": reason,
                            "count": value,
                            "pnl_amount": row.get(f"exit_{reason}_pnl", 0.0),
                        }
                    )
            print(
                f"[yearly] {year} {name} excess={metrics['excess_return']} "
                f"total={metrics['total_return']} trades={metrics['total_trades']} "
                f"opp_dates={len(opportunity_dates)}",
                flush=True,
            )

    _write_csv(output_dir / "yearly_decomposition.csv", rows)
    _write_csv(output_dir / "yearly_exit_summary.csv", exit_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "data_root": str(data_root),
                "panic_file": str(panic_file),
                "ranks_path": str(ranks_path),
                "years": years,
                "variants": [name for name, _ in variants],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[yearly] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
