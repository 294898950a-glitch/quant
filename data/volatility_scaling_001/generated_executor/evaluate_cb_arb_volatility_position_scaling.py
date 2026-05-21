"""Evaluate volatility-based position scaling for cb_arb value-gap switch.

Computes ATR-based volatility scaling factors per CB per day and applies
them as multipliers to the baseline ranking weight and buy amount, without
altering entry eligibility. Grid-searches over lookback window and scaling
beta to find the combination that best reduces max_drawdown while preserving
excess return.

This is an evaluation harness only; it does not replace the default strategy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _gap_source_shares,
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
    _score,
    _with_cost_params,
    _write_csv,
)

# ---------------------------------------------------------------------------
# Base backtest params — same as the baseline value-gap switch
# ---------------------------------------------------------------------------

BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}

# ---------------------------------------------------------------------------
# Grid: lookback windows x scaling betas + baseline
# ---------------------------------------------------------------------------

VOLATILITY_LOOKBACKS = (10, 20, 30)
SCALING_BETAS = (0.5, 1.0, 2.0)

CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_no_scaling",
        "description": "基准：不调整波动率缩放",
        "mode": "none",
    },
    *[
        {
            "name": f"vol_scale_lb{lb}_beta{str(b).replace('.', 'p')}",
            "description": f"波动率缩放 lookback={lb} 天, beta={b}",
            "mode": "volatility",
            "volatility_lookback": lb,
            "scaling_beta": b,
        }
        for lb in VOLATILITY_LOOKBACKS
        for b in SCALING_BETAS
    ],
]


# ---------------------------------------------------------------------------
# spec binding (same as option_position_sizing)
# ---------------------------------------------------------------------------

def _spec_binding_fields(output_dir: Path) -> dict[str, str]:
    spec_path = output_dir / "spec.yaml"
    if not spec_path.exists():
        return {"spec_run_id": output_dir.name, "spec_binding_hash": ""}
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        return {"spec_run_id": output_dir.name, "spec_binding_hash": ""}
    binding = {
        "run_id": spec.get("run_id") or output_dir.name,
        "hypothesis": spec.get("hypothesis"),
        "source_insight": spec.get("source_insight"),
        "parameter_space": spec.get("parameter_space"),
        "mechanics": spec.get("mechanics"),
        "proposal_id": ((spec.get("automation") or {}).get("proposal_id")),
    }
    payload = json.dumps(binding, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "spec_run_id": str(binding["run_id"]),
        "spec_binding_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


def _attach_spec_binding(rows: list[dict[str, Any]], output_dir: Path) -> None:
    binding = _spec_binding_fields(output_dir)
    for row in rows:
        row.update(binding)


# ---------------------------------------------------------------------------
# ATR calculation
# ---------------------------------------------------------------------------

def _compute_daily_atr(
    price_df: pd.DataFrame,
    ts_col: str,
    date_col: str,
    lookback: int,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
) -> pd.DataFrame:
    """Compute ATR (Average True Range) for each asset over the lookback window.

    Uses Wilder's smoothed ATR: first value is simple mean of TR over lookback,
    subsequent values use EMA-style smoothing: ATR_t = (ATR_{t-1}*(n-1) + TR_t)/n.

    Returns a DataFrame with [ts_col, date_col, atr] columns.
    """
    df = price_df.sort_values([ts_col, date_col]).reset_index(drop=True)
    df[high_col] = df[high_col].astype(float)
    df[low_col] = df[low_col].astype(float)
    df[close_col] = df[close_col].astype(float)

    # True Range
    df["prev_close"] = df.groupby(ts_col)[close_col].shift(1)
    df["tr"] = np.maximum(
        df[high_col] - df[low_col],
        np.maximum(
            (df[high_col] - df["prev_close"]).abs(),
            (df[low_col] - df["prev_close"]).abs(),
        ),
    )
    # Fill leading NaN TR with high-low (first bar has no prev_close)
    df["tr"] = df["tr"].fillna(df[high_col] - df[low_col])

    # Wilder smoothed ATR
    result_rows: list[dict[str, Any]] = []
    for _ts, group in df.groupby(ts_col):
        group = group.reset_index(drop=True)
        tr_vals = group["tr"].values
        atr_vals = np.full(len(tr_vals), np.nan)
        if len(tr_vals) >= lookback:
            atr_vals[lookback - 1] = tr_vals[:lookback].mean()
            for i in range(lookback, len(tr_vals)):
                atr_vals[i] = (atr_vals[i - 1] * (lookback - 1) + tr_vals[i]) / lookback
        # Fallback: if too few rows, use simple mean
        else:
            atr_vals[:] = tr_vals.mean()
        for i, (_, row) in enumerate(group.iterrows()):
            result_rows.append(
                {
                    ts_col: str(row[ts_col]),
                    date_col: str(row[date_col]),
                    "atr": float(atr_vals[i]) if not pd.isna(atr_vals[i]) else 0.0,
                }
            )
    return pd.DataFrame(result_rows)


# ---------------------------------------------------------------------------
# args parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--reuse-ranks", action="store_true")
    p.add_argument("--base-ranks-path", type=Path, default=None)
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    p.add_argument("--volatility-lookback", type=int, default=20)
    p.add_argument("--scaling-beta", type=float, default=1.0)
    return p.parse_args()


# ---------------------------------------------------------------------------
# data requirements declaration
# ---------------------------------------------------------------------------

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
        raise ValueError("evaluate_cb_arb_volatility_position_scaling requires --data-root")
    data_root = Path(data_root_raw)

    base_ranks_raw = _command_value_from_parts(command, "--base-ranks-path")
    warehouse_files = [
        "data/cb_warehouse/cb_basic.parquet",
        "data/cb_warehouse/cb_daily.parquet",
        "data/cb_warehouse/cb_call.parquet",
        "data/cb_warehouse/stk_daily_qfq.parquet",
    ]
    required_files: list[dict[str, str]] = [
        {"path": str(data_root / rel_path), "role": "warehouse_input"}
        for rel_path in warehouse_files
    ]
    if base_ranks_raw:
        base_ranks_path = Path(base_ranks_raw)
        if not base_ranks_path.is_absolute():
            base_ranks_path = _REPO_ROOT / base_ranks_path
        required_files.append(
            {"path": str(base_ranks_path), "role": "base_ranks_input"}
        )

    # pool configs needed by the backtest harness
    fixed_source_raw = _command_value_from_parts(command, "--fixed-source") or "2"
    fixed_source = int(fixed_source_raw)
    pool_ids = sorted({0, 2, 4, 6, fixed_source})
    required_files.extend(
        {
            "path": str(data_root / f"pool_{pool_id}" / "best_params.json"),
            "role": "config_pool",
        }
        for pool_id in pool_ids
    )

    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_volatility_position_scaling.py",
        "required_files": required_files,
    }


# ---------------------------------------------------------------------------
# rank loading + volatility scaling
# ---------------------------------------------------------------------------

def _load_base_ranks(args: argparse.Namespace, output_dir: Path) -> pd.DataFrame:
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)
    if args.base_ranks_path is not None and Path(args.base_ranks_path).exists():
        ranks = pd.read_parquet(args.base_ranks_path)
    else:
        ranks = _load_or_build_value_ranks(
            args.data_root,
            start_all,
            end_all,
            args.fixed_source,
            args.rule,
            output_dir / "daily_value_gap_amounts_base.parquet",
            args.reuse_ranks,
        )
    ranks = ranks.copy()
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    ranks["ts_code"] = ranks["ts_code"].astype(str)
    ranks = ranks[
        (ranks["trade_date"] >= start_all) & (ranks["trade_date"] <= end_all)
    ]
    return ranks


def _load_atr_data(
    lookback: int,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Load cb_daily, compute ATR, return DataFrame with [ts_code, trade_date, atr]."""
    cb = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_daily.parquet")
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb["ts_code"] = cb["ts_code"].astype(str)
    cb = cb[(cb["trade_date"] >= start) & (cb["trade_date"] <= end)]
    atr = _compute_daily_atr(cb, "ts_code", "trade_date", lookback)
    return atr


def _merge_and_scale(
    ranks: pd.DataFrame,
    atr_df: pd.DataFrame,
    cfg: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply volatility scaling to value_gap_amount and position_cash_scale.

    scaling_factor = min(1, 1 / (1 + beta * (ATR / median_ATR - 1)))
    """
    mode = str(cfg.get("mode", "none"))
    blended = ranks.merge(
        atr_df, on=["ts_code", "trade_date"], how="left"
    )

    # Default: no scaling
    blended["position_cash_scale"] = 1.0
    blended["volatility_scaling_factor"] = 1.0

    if mode == "none":
        return blended, {
            "name": cfg["name"],
            "adjusted_rows": 0,
            "avg_scale": 1.0,
            "min_scale": 1.0,
            "max_scale": 1.0,
            "avg_atr": 0.0,
            "median_atr": 0.0,
        }

    lookback = int(cfg["volatility_lookback"])
    beta = float(cfg["scaling_beta"])

    # Compute cross-sectional median ATR per day
    daily_median = blended.groupby("trade_date")["atr"].transform("median")

    # Only scale rows where ATR is available
    has_atr = blended["atr"].notna() & (blended["atr"] > 0)
    blended["volatility_scaling_factor"] = 1.0

    if has_atr.any():
        atr_vals = blended.loc[has_atr, "atr"].astype(float)
        med_vals = daily_median.loc[has_atr].astype(float)
        # Avoid division by zero
        med_safe = med_vals.where(med_vals > 0, atr_vals)

        atr_ratio = atr_vals / med_safe
        raw_factor = 1.0 / (1.0 + beta * (atr_ratio - 1.0))
        factor = raw_factor.clip(upper=1.0)
        blended.loc[has_atr, "volatility_scaling_factor"] = factor.astype(float)
        blended.loc[has_atr, "position_cash_scale"] = factor.astype(float)

    # Apply to value_gap_amount
    value_col = "value_gap_amount"
    if value_col in blended.columns:
        blended[value_col] = (
            blended[value_col].astype(float) * blended["volatility_scaling_factor"].astype(float)
        )
    # Recompute value_gap_pct_of_cash if both columns exist
    if "value_gap_pct_of_cash" in blended.columns and "position_cash" in blended.columns:
        pos_cash = blended["position_cash"].astype(float)
        blended["value_gap_pct_of_cash"] = blended[value_col].astype(float) / pos_cash.where(
            pos_cash > 0, np.nan
        )

    # Re-sort by date and scaled gap amount
    blended = blended.sort_values(
        ["trade_date", value_col], ascending=[True, False]
    ).reset_index(drop=True)
    blended["rank"] = blended.groupby("trade_date").cumcount()

    # Stats
    scaled_rows = has_atr
    factors = blended.loc[scaled_rows, "volatility_scaling_factor"]
    atrs = blended.loc[scaled_rows, "atr"]
    medians = daily_median.loc[scaled_rows]

    return blended, {
        "name": cfg["name"],
        "adjusted_rows": int(scaled_rows.sum()),
        "avg_scale": round(float(factors.mean()), 6) if not factors.empty else 1.0,
        "min_scale": round(float(factors.min()), 6) if not factors.empty else 1.0,
        "max_scale": round(float(factors.max()), 6) if not factors.empty else 1.0,
        "avg_atr": round(float(atrs.mean()), 6) if not atrs.empty else 0.0,
        "median_atr": round(float(medians.mean()), 6) if not medians.empty else 0.0,
        "volatility_lookback": lookback,
        "scaling_beta": beta,
    }


# ---------------------------------------------------------------------------
# report rows and file writing
# ---------------------------------------------------------------------------

def _row(
    name: str,
    description: str,
    period: str,
    start: str,
    end: str,
    cfg: dict[str, Any],
    params: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "description": description,
        "period": period,
        "start": start,
        "end": end,
        "scaling_config_json": json.dumps(cfg, sort_keys=True),
        "params_json": json.dumps(params, sort_keys=True),
        **result["metrics"],
    }
    row["score"] = _score(result["metrics"])
    return row


def _pick(rows: list[dict[str, Any]], name: str, period: str) -> dict[str, Any]:
    return next((r for r in rows if r["name"] == name and r["period"] == period), {})


def _year(rows: list[dict[str, Any]], name: str, year: int) -> dict[str, Any]:
    return next(
        (r for r in rows if r["name"] == name and r["period"] == str(year)), {}
    )


def _write_review_files(
    output_dir: Path,
    summary: dict[str, Any],
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    adoption_pass: bool,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    decision = "mini-spec-retry" if adoption_pass else "reject"
    reason = (
        "Volatility scaling variant passed the automatic train/test/2020 checks; review before promotion."
        if adoption_pass
        else "No volatility scaling variant beat baseline across train, 2020 repair, and sealed test together."
    )
    selected_test = _pick(
        summary.get("summary_rows", []), str(best_train.get("name")), "test"
    )
    baseline_2020 = summary.get("baseline_2020", {})
    selected_2020 = summary.get("selected_2020", {})

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "strategy_id": "cb_arb_value_gap_switch",
                "l6_exit_decision": decision,
                "status": "COMPLETE",
                "three_exits_section": {
                    "train_exit": f"Train winner selected {best_train.get('name')}.",
                    "validation_exit": f"Sealed test winner selected {best_test.get('name')}.",
                    "decision_exit": reason,
                },
                "compute_cost_yuan": 0.0,
                "confirmed_invalid_directions": [
                    "volatility_based_position_scaling"
                ]
                if not adoption_pass
                else [],
                "learnings": [
                    "Volatility-based position scaling must pass train, 2020 repair, and sealed test together.",
                    reason,
                ],
                "follow_up_actions": [
                    "Keep this run as diagnostic evidence for future volatility-based risk filters.",
                    "Do not promote unless follow-up review confirms train/test/2020 robustness.",
                ],
                "summary": reason,
                "notes": "Result reviewed by code-generated summary.json, l4_ack.yaml, and diagnostic.yaml.",
                "references": summary["artifacts"],
                "related_reports": [
                    "data/cb_arb_value_gap_switch_option-position-sizing_2026-05-17_151411/report.yaml",
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "reviewer": "codex",
                "ack_at": now,
                "q1_floor_binding": {
                    "description": "Hard floors and train/test consistency.",
                    "answer": (
                        "Selected train winner also meets sealed test and 2020 checks."
                        if adoption_pass
                        else "Selected train winner does not pass train/test/2020 robustness checks together."
                    ),
                    "computed_data": {
                        "best_train_variant": best_train.get("name"),
                        "train_excess": best_train.get("excess_return"),
                        "test_excess": selected_test.get("excess_return"),
                        "baseline_test_excess": baseline_test.get("excess_return"),
                        "baseline_2020_total_return": baseline_2020.get("total_return"),
                        "selected_2020_total_return": selected_2020.get("total_return"),
                    },
                    "computed_at": now,
                    "pass": adoption_pass,
                },
                "q2_selection_score": {
                    "description": "Candidate selection quality.",
                    "answer": (
                        f"Train score selects {best_train.get('name')}; sealed test best is {best_test.get('name')}."
                    ),
                    "computed_data": {
                        "selected_by_train_score": best_train.get("name"),
                        "selected_score": best_train.get("score"),
                        "best_test_variant": best_test.get("name"),
                        "best_test_score": best_test.get("score"),
                    },
                    "pass": adoption_pass,
                },
                "q3_baseline_alignment": {
                    "description": "Alignment against current cb_arb_value_gap_switch baseline.",
                    "answer": (
                        "Candidate is aligned with baseline thresholds."
                        if adoption_pass
                        else "Candidate does not justify replacing the current baseline."
                    ),
                    "computed_data": {
                        "baseline_train_excess": baseline_train.get("excess_return"),
                        "baseline_test_excess": baseline_test.get("excess_return"),
                        "selected_test_excess": selected_test.get("excess_return"),
                    },
                    "computed_at": now,
                    "pass": adoption_pass,
                },
                "q4_monotonic": {
                    "description": "Edge-of-grid or monotonic concern.",
                    "answer": "Grid-searched volatility scaling factors; no monotonic promotion without manual review.",
                    "computed_data": {
                        "grid_type": "volatility_scaling_grid",
                        "candidates_count": summary.get("candidate_count"),
                    },
                    "computed_at": now,
                    "pass": True,
                },
                "q5_trade_overlap": {
                    "description": "Trade overlap baseline vs selected.",
                    "answer": "Aggregate train/test/2020 checks are used for automatic decision.",
                    "computed_data": {
                        "selected_total_trades_test": selected_test.get("total_trades"),
                        "baseline_total_trades_test": baseline_test.get("total_trades"),
                        "selected_total_trades_2020": selected_2020.get("total_trades"),
                        "baseline_total_trades_2020": baseline_2020.get("total_trades"),
                    },
                    "computed_at": now,
                    "pass": True,
                },
                "q6_trigger_timing": {"description": "Trigger timing leakage.", "applicable": False},
                "q7_path_contamination": {"description": "Path/data contamination.", "applicable": False},
                "overall_pass": adoption_pass,
                "overall_decision": decision,
                "overall_reason": reason,
                "auto_computed_at": now,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
                "diagnostic_by": "codex",
                "verdict_referenced": decision,
                "summary": reason,
                "verdict_rationale": reason,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "volatility_position_scaling"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load base ranks (without any scaling)
    base_ranks = _load_base_ranks(args, output_dir)

    # Determine the full date range for ATR pre-computation per config
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    adjustment_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])

        if cfg["mode"] == "none":
            adjusted = base_ranks.copy()
            adjusted["position_cash_scale"] = 1.0
            adjustment: dict[str, Any] = {
                "name": name,
                "adjusted_rows": 0,
                "avg_scale": 1.0,
                "min_scale": 1.0,
                "max_scale": 1.0,
                "avg_atr": 0.0,
                "median_atr": 0.0,
            }
        else:
            lb = int(cfg["volatility_lookback"])
            # Need ATR starting lb days before start_all for proper Wilder smoothing
            atr_start = pd.to_datetime(start_all, format="%Y%m%d") - pd.Timedelta(days=lb * 3)
            atr_start_str = atr_start.strftime("%Y%m%d")
            atr_df = _load_atr_data(lb, atr_start_str, end_all)
            adjusted, adjustment = _merge_and_scale(base_ranks, atr_df, cfg)

        adjustment_rows.append(adjustment)

        # Save adjusted ranks
        adjusted.to_parquet(
            output_dir / f"daily_value_gap_amounts_{name}.parquet", index=False
        )

        # Backtest params
        params = _with_cost_params(dict(BASE_PARAMS), args)

        # Train
        train = _run_value_gap_backtest(
            adjusted[
                (adjusted["trade_date"] >= args.train_start)
                & (adjusted["trade_date"] <= args.train_end)
            ],
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        # Test
        test = _run_value_gap_backtest(
            adjusted[
                (adjusted["trade_date"] >= args.test_start)
                & (adjusted["trade_date"] <= args.test_end)
            ],
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )

        summary_rows.append(
            _row(name, description, "train", args.train_start, args.train_end, cfg, params, train)
        )
        summary_rows.append(
            _row(name, description, "test", args.test_start, args.test_end, cfg, params, test)
        )

        print(
            f"[volatility_scaling] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"train_dd={train['metrics']['max_drawdown']} "
            f"test_excess={test['metrics']['excess_return']} "
            f"test_dd={test['metrics']['max_drawdown']}",
            flush=True,
        )

        # Yearly breakdown (for 2020 check)
        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            y = _run_value_gap_backtest(
                adjusted[
                    (adjusted["trade_date"] >= start) & (adjusted["trade_date"] <= end)
                ],
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            yearly_rows.append(
                _row(name, description, str(year), start, end, cfg, params, y)
            )

    # Attach spec binding
    _attach_spec_binding(summary_rows, output_dir)

    # Write CSVs
    _write_csv(output_dir / "summary_volatility_position_scaling.csv", summary_rows)
    _write_csv(output_dir / "yearly_volatility_position_scaling.csv", yearly_rows)
    _write_csv(
        output_dir / "adjustment_volatility_position_scaling.csv", adjustment_rows
    )

    # Find best train and test configurations
    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = _pick(summary_rows, "baseline_no_scaling", "train")
    baseline_test = _pick(summary_rows, "baseline_no_scaling", "test")
    selected_test = _pick(summary_rows, str(best_train.get("name")), "test")
    baseline_2020 = _year(yearly_rows, "baseline_no_scaling", 2020)
    selected_2020 = _year(yearly_rows, str(best_train.get("name")), 2020)

    # Success criteria from proposal:
    #   train_max_drawdown: <= -0.25
    #   test_excess_return: >= 0.2
    #   yr2020_total_return: >= -0.05
    train_dd_ok = (
        float(best_train.get("max_drawdown", -999)) >= -0.25
    )
    test_excess_ok = (
        float(selected_test.get("excess_return", -999)) >= 0.2
    )
    yr2020_ok = (
        float(selected_2020.get("total_return", -999)) >= -0.05
    )

    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_no_scaling"
        and train_dd_ok
        and test_excess_ok
        and yr2020_ok
    )

    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "candidate_count": len(CONFIGS),
        "adoption_pass": adoption_pass,
        "best_train": best_train,
        "best_test": best_test,
        "baseline_train": baseline_train,
        "baseline_test": baseline_test,
        "selected_test": selected_test,
        "baseline_2020": baseline_2020,
        "selected_2020": selected_2020,
        "success_criteria_checks": {
            "train_max_drawdown_le_neg0p25": train_dd_ok,
            "test_excess_return_ge_0p2": test_excess_ok,
            "yr2020_total_return_ge_neg0p05": yr2020_ok,
        },
        "summary_rows": summary_rows,
        "artifacts": [
            "summary_volatility_position_scaling.csv",
            "yearly_volatility_position_scaling.csv",
            "adjustment_volatility_position_scaling.csv",
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
    }
    _write_review_files(
        output_dir,
        summary,
        best_train,
        best_test,
        baseline_train,
        baseline_test,
        adoption_pass,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
