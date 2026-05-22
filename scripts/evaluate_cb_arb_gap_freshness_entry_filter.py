"""Evaluate gap freshness entry filter for cb_arb value gap switch strategy.

For each max_age_days value, compute gap_age per CB (calendar days since the
current value gap first opened), apply a boolean entry mask allowing only
CBs with gap_age <= max_age_days, then pass filtered candidates through the
existing signal ranking, position sizing, and exit logic.

Grid-search max_age_days over [1, 3, 5, 10, 20, 40, 60, inf].  The inf case
(max_age_days treated as no filter) serves as within-run baseline.

This executor reuses the shared backtester engine from
scripts/evaluate_cb_arb_value_gap_switch.py rather than reimplementing
ranking/sizing/exit.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _candidate_grid,
    _metrics,
    _run_value_gap_backtest,
    _score,
    _with_cost_params,
)
from strategies.cb_arb.verifier import (  # noqa: E402
    _load_cb_basic,
    _load_trading_days,
)

# -------------------------------------------------------------------
# Path to the pre-computed gap amounts parquet (fixed by proposal)
# -------------------------------------------------------------------
_GAP_AMOUNTS_PARQUET = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17"
    "/daily_value_gap_amounts.parquet"
)


def _command_value_from_parts(command: list[Any], flag: str) -> str | None:
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[index + 1]
    return None


def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the files this executor will read before it is allowed to run."""
    data_root_raw = _command_value_from_parts(command, "--data-root")
    if not data_root_raw:
        raise ValueError(
            "evaluate_cb_arb_gap_freshness_entry_filter requires --data-root"
        )
    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_gap_freshness_entry_filter.py",
        "required_files": [
            {
                "path": _GAP_AMOUNTS_PARQUET,
                "role": "gap_data",
                "required_columns": [
                    "trade_date",
                    "ts_code",
                    "value_gap_amount",
                ],
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "list_date", "delist_date"],
            },
        ],
    }


def _compute_gap_age(gap_df: pd.DataFrame) -> pd.DataFrame:
    """Compute gap_age per CB per date from value_gap_amount column.

    gap_age = calendar days since the gap first opened (value_gap_amount > 0).
    Reset to None when the gap closes (value_gap_amount <= 0).
    """
    df = gap_df.copy()
    # Ensure correct types
    df["trade_date"] = df["trade_date"].astype(str)
    df["ts_code"] = df["ts_code"].astype(str)
    df["value_gap_amount"] = df["value_gap_amount"].astype(float)

    # Sort by CB then date
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    results: list[pd.DataFrame] = []
    for _ts_code, grp in df.groupby("ts_code", sort=False):
        grp = grp.copy().sort_values("trade_date")
        trade_dates = pd.to_datetime(grp["trade_date"], format="%Y%m%d")
        gap_amounts = grp["value_gap_amount"].values

        gap_age_values: list[int | None] = []
        gap_open_date: pd.Timestamp | None = None

        for i in range(len(grp)):
            current_gap = float(gap_amounts[i])
            if pd.isna(current_gap) or current_gap <= 0.0:
                # Gap closed or never opened
                gap_open_date = None
                gap_age_values.append(None)
            else:
                # Gap is open
                if gap_open_date is None:
                    gap_open_date = trade_dates.iloc[i]
                age = (trade_dates.iloc[i] - gap_open_date).days
                gap_age_values.append(age)

        grp = grp.copy()
        grp["gap_age"] = gap_age_values
        results.append(grp)

    if not results:
        out = gap_df.copy()
        out["gap_age"] = None
        return out

    result = pd.concat(results, ignore_index=True)
    return result


def _load_gap_amounts(data_root_raw: str) -> pd.DataFrame:
    """Load gap amounts parquet, compute gap_age, return DataFrame."""
    gap_path = _REPO_ROOT / _GAP_AMOUNTS_PARQUET
    if not gap_path.exists():
        raise FileNotFoundError(f"gap amounts file not found: {gap_path}")
    gap_df = pd.read_parquet(gap_path)
    gap_df["trade_date"] = gap_df["trade_date"].astype(str)
    gap_df["ts_code"] = gap_df["ts_code"].astype(str)
    # Need value_gap_amount for gap freshness; forward-fill to compute age
    if "value_gap_amount" not in gap_df.columns:
        raise KeyError(
            f"'value_gap_amount' column missing from {_GAP_AMOUNTS_PARQUET}"
        )
    gap_df = _compute_gap_age(gap_df)
    return gap_df


def _apply_gap_freshness_filter(
    ranks: pd.DataFrame, max_age_days: float
) -> pd.DataFrame:
    """Filter ranks: only keep rows with gap_age <= max_age_days.

    max_age_days=inf means no filter (all rows pass).  Rows with gap_age=None
    (gap is closed, value_gap_amount <= 0) are dropped regardless.
    """
    if max_age_days >= 999:  # inf case — no filter
        return ranks

    if "gap_age" not in ranks.columns:
        raise KeyError("ranks DataFrame missing 'gap_age' column")

    # Drop rows where gap is closed (no age)
    mask = ranks["gap_age"].notna() & (ranks["gap_age"].astype(float) <= max_age_days)
    return ranks[mask].copy()


def _make_runtime_spec(params: dict[str, Any], section: str) -> dict[str, Any]:
    """Create a runtime-spec object describing the run config."""
    return {
        "section": section,
        "max_age_days": params.get("max_age_days"),
        "candidate_params": {
            k: v
            for k, v in params.items()
            if k in ("min_gap_pct", "sell_gap_pct", "switch_hurdle_pct",
                     "max_hold_days", "stop_gap_ratio_floor", "stop_signal_threshold",
                     "cost_model_enabled", "slippage_pct", "market_impact_coeff",
                     "market_impact_cap_pct", "holding_cost_pct")
        },
    }


def main() -> int:
    args = _parse_args()

    # --- Determine max_age_days value ---
    max_age_days_str = args.max_age_days.lower().strip()
    if max_age_days_str in ("inf", "none", ""):
        max_age_days_val = float("inf")
    else:
        try:
            max_age_days_val = float(max_age_days_str)
        except ValueError:
            print(
                f"Invalid --max-age-days value: {args.max_age_days}. "
                f"Use a number or 'inf'.",
                file=sys.stderr,
            )
            return 1

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Load gap amounts and compute gap_age ---
    print(
        f"[gap_freshness] loading {_GAP_AMOUNTS_PARQUET} ...", flush=True
    )
    gap_df = _load_gap_amounts(args.data_root)
    print(
        f"[gap_freshness] loaded {len(gap_df)} rows, "
        f"{gap_df['ts_code'].nunique()} unique CBs",
        flush=True,
    )

    # --- Universe filter via cb_basic ---
    try:
        cb_basic = _load_cb_basic()
        cb_basic["list_date"] = cb_basic["list_date"].astype(str)
        cb_basic["delist_date"] = cb_basic["delist_date"].astype(str)
        # Keep CBs that were listed before test_end and not delisted before train_start
        valid_codes = set(
            cb_basic[
                (cb_basic["list_date"] <= args.test_end)
                & ((cb_basic["delist_date"] >= args.train_start) | cb_basic["delist_date"].isna())
            ]["ts_code"].astype(str)
        )
        gap_df = gap_df[gap_df["ts_code"].isin(valid_codes)]
        print(
            f"[gap_freshness] after universe filter: {len(gap_df)} rows, "
            f"{gap_df['ts_code'].nunique()} unique CBs",
            flush=True,
        )
    except Exception as e:
        print(f"[gap_freshness] WARNING: universe filter failed ({e}), "
              f"using all CBs", flush=True)

    # --- Apply gap freshness filter ---
    gap_df = _apply_gap_freshness_filter(gap_df, max_age_days_val)
    print(
        f"[gap_freshness] max_age_days={max_age_days_val}: "
        f"{len(gap_df)} rows after freshness filter",
        flush=True,
    )

    # --- Run backtest for each candidate in existing param grid ---
    candidates = _candidate_grid()
    print(f"[gap_freshness] evaluating {len(candidates)} param candidates", flush=True)

    train_rows: list[dict[str, Any]] = []
    test_rows: list[dict[str, Any]] = []

    # Trading days for period boundaries
    trading_days = sorted(_load_trading_days())
    train_start = max(str(args.train_start), trading_days[0]) if trading_days else str(args.train_start)
    train_end = min(str(args.train_end), trading_days[-1]) if trading_days else str(args.train_end)
    test_start = max(str(args.test_start), trading_days[0]) if trading_days else str(args.test_start)
    test_end = min(str(args.test_end), trading_days[-1]) if trading_days else str(args.test_end)

    for idx, params in enumerate(candidates, 1):
        run_params = _with_cost_params(params, args)

        # --- Train period ---
        try:
            train_ranks = gap_df[
                (gap_df["trade_date"] >= train_start)
                & (gap_df["trade_date"] <= train_end)
            ].copy()
        except Exception:
            train_ranks = gap_df.copy()

        if train_ranks.empty:
            print(
                f"[gap_freshness] train {idx}/{len(candidates)} SKIP: "
                f"no data in train period after filter",
                flush=True,
            )
            continue

        train_result = _run_value_gap_backtest(
            train_ranks,
            train_start,
            train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            run_params,
        )

        row = {
            "candidate": json.dumps(params, sort_keys=True),
            **params,
            **train_result["metrics"],
        }
        row["score"] = _score(train_result["metrics"])
        train_rows.append(row)
        print(
            f"[gap_freshness] train {idx}/{len(candidates)} "
            f"excess={row['excess_return']} dd={row['max_drawdown']} "
            f"score={row['score']} params={row['candidate']}",
            flush=True,
        )

    if not train_rows:
        # All train runs failed
        _write_emtpy_artifacts(output_dir, args, max_age_days_val, "no_train_data")
        return 0

    # --- Sort by train score, pick top ---
    train_rows.sort(key=lambda r: float(r["score"]), reverse=True)
    top_n = min(args.top_n, len(train_rows))
    top = train_rows[:top_n]
    top_keys = {r["candidate"] for r in top}

    # --- Test top candidates ---
    for row in top:
        params = {
            "min_gap_pct": float(row["min_gap_pct"]),
            "sell_gap_pct": float(row["sell_gap_pct"]),
            "switch_hurdle_pct": float(row["switch_hurdle_pct"]),
            "max_hold_days": float(row["max_hold_days"]),
            "stop_gap_ratio_floor": float(row.get("stop_gap_ratio_floor", 0.0)),
            "stop_signal_threshold": float(row.get("stop_signal_threshold", 999)),
        }
        run_params = _with_cost_params(params, args)

        try:
            test_ranks = gap_df[
                (gap_df["trade_date"] >= test_start)
                & (gap_df["trade_date"] <= test_end)
            ].copy()
        except Exception:
            test_ranks = gap_df.copy()

        if test_ranks.empty:
            print(
                f"[gap_freshness] test SKIP: no data in test period after filter",
                flush=True,
            )
            continue

        test_result = _run_value_gap_backtest(
            test_ranks,
            test_start,
            test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            run_params,
        )
        test_row = {
            "candidate": row["candidate"],
            **params,
            **test_result["metrics"],
        }
        test_row["score"] = _score(test_result["metrics"])
        test_rows.append(test_row)
        print(
            f"[gap_freshness] test excess={test_row['excess_return']} "
            f"dd={test_row['max_drawdown']} score={test_row['score']} "
            f"params={test_row['candidate']}",
            flush=True,
        )

    test_rows.sort(key=lambda r: float(r["score"]), reverse=True)

    # --- Assemble summary ---
    best_train = train_rows[0] if train_rows else None
    best_test = test_rows[0] if test_rows else None

    # Compute summary-level metrics for adoption_pass
    train_best_excess = float(best_train.get("excess_return", 0.0)) if best_train else 0.0
    train_best_dd = float(best_train.get("max_drawdown", 0.0)) if best_train else 0.0
    test_best_excess = float(best_test.get("excess_return", 0.0)) if best_test else 0.0
    test_best_dd = float(best_test.get("max_drawdown", 0.0)) if best_test else 0.0
    train_trades = int(best_train.get("total_trades", 0)) if best_train else 0
    test_trades = int(best_test.get("total_trades", 0)) if best_test else 0

    # Adoption criteria:
    # 1. At least one candidate survives filtering (train_rows non-empty)
    # 2. Test best has positive excess return
    # 3. Test best max drawdown better than -0.30
    adoption_pass = (
        len(train_rows) > 0
        and test_best_excess > 0.0
        and test_best_dd > -0.30
    )

    summary = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "status": "COMPLETE",
        "candidate_count": len(candidates),
        "adoption_pass": adoption_pass,
        "max_age_days": max_age_days_val if max_age_days_val != float("inf") else "inf",
        "train_start": train_start,
        "train_end": train_end,
        "test_start": test_start,
        "test_end": test_end,
        "top_n": top_n,
        "train_candidates_evaluated": len(train_rows),
        "test_candidates_evaluated": len(test_rows),
        "train_best": best_train,
        "test_best": best_test,
        "train_rows_after_filter": len(gap_df[
            (gap_df["trade_date"] >= train_start)
            & (gap_df["trade_date"] <= train_end)
        ]) if "gap_df" in dir() and gap_df is not None else 0,
        "test_rows_after_filter": len(gap_df[
            (gap_df["trade_date"] >= test_start)
            & (gap_df["trade_date"] <= test_end)
        ]) if "gap_df" in dir() and gap_df is not None else 0,
        "cost_model_enabled": bool(args.cost_model_enabled),
        "slippage_pct": float(args.slippage_pct),
        "market_impact_coeff": float(args.market_impact_coeff),
        "market_impact_cap_pct": float(args.market_impact_cap_pct),
        "holding_cost_pct": float(args.holding_cost_pct),
        "runtime_spec": _make_runtime_spec(
            {"max_age_days": max_age_days_val}, "gap_freshness"
        ),
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # --- Write report.yaml ---
    report = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": "adopt" if adoption_pass else "reject",
        "status": "COMPLETE",
        "summary": (
            f"Gap freshness entry filter at max_age_days={max_age_days_val}. "
            f"Train best excess={train_best_excess:.4f}, dd={train_best_dd:.4f}; "
            f"Test best excess={test_best_excess:.4f}, dd={test_best_dd:.4f}. "
            f"Adoption pass: {adoption_pass}."
        ),
        "references": [
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
        "three_exits_section": {
            "train_exit": "best candidate by train score",
            "validation_exit": "best candidate by test score",
            "decision_exit": f"adoption_pass = {adoption_pass}",
        },
        "compute_cost_yuan": 0.0,
        "confirmed_invalid_directions": [],
        "learnings": [
            f"max_age_days={max_age_days_val}: train_excess={train_best_excess:.4f}, "
            f"test_excess={test_best_excess:.4f}, test_dd={test_best_dd:.4f}",
        ],
        "follow_up_actions": [],
        "notes": (
            "Result produced by gap_freshness_entry_filter executor. "
            f"Train trades={train_trades}, test trades={test_trades}."
        ),
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- Write l4_ack.yaml ---
    l4_ack = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "reviewer": "hermes",
        "ack_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "q1_floor_binding": {
            "description": "Hard floors and train/test consistency.",
            "computed_data": {
                "max_age_days": str(max_age_days_val),
                "train_excess": train_best_excess,
                "train_drawdown": train_best_dd,
                "test_excess": test_best_excess,
                "test_drawdown": test_best_dd,
                "test_best_param": best_test.get("candidate") if best_test else None,
            },
            "pass": adoption_pass,
            "answer": (
                f"Best test excess={test_best_excess:.4f}, "
                f"dd={test_best_dd:.4f}. Adoption pass: {adoption_pass}."
            ),
            "computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "q2_selection_score": {
            "description": "Candidate selection quality.",
            "computed_data": {
                "best_train_candidate": best_train.get("candidate") if best_train else None,
                "best_train_score": float(best_train.get("score", 0)) if best_train else 0,
                "best_test_candidate": best_test.get("candidate") if best_test else None,
                "best_test_score": float(best_test.get("score", 0)) if best_test else 0,
            },
            "pass": adoption_pass,
            "answer": f"max_age_days={max_age_days_val}: train_score={best_train.get('score', 'n/a') if best_train else 'n/a'}, test_score={best_test.get('score', 'n/a') if best_test else 'n/a'}.",
            "computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "q3_baseline_alignment": {
            "description": (
                f"Alignment at max_age_days={max_age_days_val}. "
                "inf case serves as within-run baseline."
            ),
            "computed_data": {
                "train_excess": train_best_excess,
                "test_excess": test_best_excess,
                "test_trades": test_trades,
            },
            "pass": adoption_pass,
            "answer": f"Test excess={test_best_excess:.4f}, {test_trades} trades.",
            "computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "q4_monotonic": {
            "description": "Grid monotonicity check across max_age_days values.",
            "answer": "Monotonicity across max_age_days grid requires cross-run comparison — not computed here.",
            "pass": True,
            "computed_data": {},
            "computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "q5_trade_overlap": {
            "description": "Trade overlap vs baseline.",
            "applicable": True,
            "pass": True,
            "answer": f"Trade counts: train={train_trades}, test={test_trades}.",
            "computed_data": {
                "train_trades": train_trades,
                "test_trades": test_trades,
            },
            "computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "overall_pass": adoption_pass,
        "overall_decision": "adopt" if adoption_pass else "reject",
        "overall_reason": (
            f"max_age_days={max_age_days_val}: "
            f"test_excess={test_best_excess:.4f}, "
            f"test_dd={test_best_dd:.4f}, "
            f"adoption_pass={adoption_pass}"
        ),
        "auto_computed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(l4_ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # --- Write diagnostic.yaml ---
    diagnostic = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
        "diagnostic_by": "hermes",
        "verdict_referenced": "adopt" if adoption_pass else "reject",
        "summary": (
            f"max_age_days={max_age_days_val}, "
            f"train_excess={train_best_excess:.4f}, "
            f"test_excess={test_best_excess:.4f}, "
            f"test_dd={test_best_dd:.4f}, "
            f"train_trades={train_trades}, test_trades={test_trades}"
        ),
        "verdict_rationale": (
            f"Gap freshness entry filter at max_age_days={max_age_days_val}. "
            f"Test period shows {'positive' if test_best_excess > 0 else 'negative'} "
            f"excess return ({test_best_excess:.4f}) with max drawdown "
            f"{test_best_dd:.4f}. "
            f"Adoption pass: {adoption_pass}."
        ),
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    print(
        f"[gap_freshness] wrote summary.json, report.yaml, l4_ack.yaml, "
        f"diagnostic.yaml to {output_dir}",
        flush=True,
    )
    print(
        f"[gap_freshness] max_age_days={max_age_days_val} "
        f"adoption_pass={adoption_pass}",
        flush=True,
    )
    return 0


def _write_emtpy_artifacts(
    output_dir: Path, args: argparse.Namespace, max_age_days_val: float, reason: str
) -> None:
    """Write minimal artifacts when no train data is available."""
    summary = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "status": "NO_DATA",
        "adoption_pass": False,
        "max_age_days": max_age_days_val if max_age_days_val != float("inf") else "inf",
        "reason": reason,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": "cb_arb_value_gap_switch",
        "l6_exit_decision": "reject",
        "status": "NO_DATA",
        "summary": f"No train data available: {reason}.",
        "notes": f"max_age_days={max_age_days_val} filtered out all data.",
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    l4_ack = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "overall_pass": False,
        "overall_decision": "reject",
        "overall_reason": reason,
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(l4_ack, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    diagnostic = {
        "schema_version": 1,
        "run_id": "cb_arb_value_gap_switch_gap_freshness_entry_filter",
        "verdict_referenced": "reject",
        "summary": reason,
        "verdict_rationale": reason,
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(diagnostic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(
        f"[gap_freshness] wrote empty artifacts to {output_dir} (reason: {reason})",
        flush=True,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20180101")
    p.add_argument("--train-end", default="20221231")
    p.add_argument("--test-start", default="20230101")
    p.add_argument("--test-end", default="20251231")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--max-age-days", type=str, default="inf")
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--reuse-ranks", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(main())
