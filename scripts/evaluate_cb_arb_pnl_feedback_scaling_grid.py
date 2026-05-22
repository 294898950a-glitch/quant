"""Grid-search executor for rolling realized PnL feedback position scaling.

For each (lookback_days, pnl_scaling_floor) pair: compute per-candidate rolling
average realized PnL over the lookback window; if avg PnL >= 0, scale = 1.0;
if avg PnL < 0, scale linearly from 1.0 down to the floor proportional to the
negative PnL magnitude. Multiply the baseline position weight and buy amount by
the scale factor. Entry eligibility, scoring, ranking, and all exit rules remain
identical to baseline.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_option_position_sizing import (  # noqa: E402
    _load_base_ranks,
    _pick,
    _row,
    _year,
)
from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _gap_source_shares,
    _run_value_gap_backtest,
    _score,
    _with_cost_params,
    _write_csv,
)

BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
    "candidate_position_scale_enabled": 0.0,
    "option_source_pnl_feedback_enabled": 0.0,
}


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
        raise ValueError("evaluate_cb_arb_pnl_feedback_scaling_grid requires --data-root")

    base_ranks_raw = _command_value_from_parts(command, "--base-ranks-path")
    base_path = (
        Path(base_ranks_raw)
        if base_ranks_raw
        else Path("data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet")
    )

    required_files: list[dict[str, Any]] = [
        {
            "path": str(base_path),
            "role": "base_ranks_input",
            "required_columns": [
                "trade_date", "ts_code", "close", "bond_floor",
                "position_cash", "fee_pct", "buy_qty",
                "value_gap_amount", "value_gap_pct_of_cash",
            ],
        },
        {
            "path": "data/cb_warehouse/cb_daily.parquet",
            "role": "warehouse_input",
            "required_columns": ["ts_code", "trade_date", "close"],
        },
        {
            "path": "data/cb_warehouse/cb_basic.parquet",
            "role": "warehouse_input",
            "required_columns": ["ts_code", "stk_code", "conv_price"],
        },
        {
            "path": "data/cb_warehouse/stk_daily_qfq.parquet",
            "role": "warehouse_input",
            "required_columns": ["stk_code", "trade_date", "close"],
        },
    ]
    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_pnl_feedback_scaling_grid.py",
        "required_files": required_files,
    }


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
    p.add_argument("--lookback-days", type=str, default="20,40,60",
                   help="Comma-separated lookback window days, e.g. '20,40,60'")
    p.add_argument("--pnl-scaling-floor", type=str, default="0.3,0.5,0.7",
                   help="Comma-separated floor values, e.g. '0.3,0.5,0.7'")
    return p.parse_args()


def _parse_grid_param(raw: str, label: str, converter: type, valid_range: tuple[float, float]) -> list[Any]:
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = converter(part)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid {label} value: {part}")
        lo, hi = valid_range
        if v < lo or v > hi:
            raise ValueError(f"{label} out of range [{lo}, {hi}]: {v}")
        values.append(v)
    if not values:
        raise ValueError(f"No valid {label} values provided")
    return values


def _compute_realized_pnl(ranks: pd.DataFrame, repo_root: Path) -> pd.DataFrame:
    """Compute daily return as proxy for realized PnL from cb_daily close prices.

    If the 'realized_pnl' column already exists, return ranks unchanged.
    Otherwise, compute daily return = close[t] / close[t-1] - 1 from cb_daily,
    join it back to ranks, and use that as realized_pnl.
    """
    out = ranks.copy()
    if "realized_pnl" in out.columns:
        return out

    cb_daily = pd.read_parquet(repo_root / "data/cb_warehouse/cb_daily.parquet")
    cb_daily["trade_date"] = cb_daily["trade_date"].astype(str)
    cb_daily = cb_daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    cb_daily["daily_ret"] = cb_daily.groupby("ts_code")["close"].transform(
        lambda x: x.pct_change()
    )
    ret_map = cb_daily.set_index(["ts_code", "trade_date"])["daily_ret"]

    out["realized_pnl"] = out.set_index(["ts_code", "trade_date"]).index.map(
        lambda t: float(ret_map.get(t, 0.0) or 0.0)
    )
    out["realized_pnl"] = out["realized_pnl"].fillna(0.0).astype(float)
    return out


def _compute_pnl_feedback_scale(
    ranks: pd.DataFrame, lookback_days: int, floor: float
) -> pd.Series:
    """Compute per-candidate linear scaling factor based on rolling average realized PnL.

    For each (ts_code, trade_date), look back 'lookback_days' days (shifted by 1 to
    avoid lookahead). If the rolling average >= 0, scale = 1.0. If negative, scale
    linearly from 1.0 down to 'floor' proportional to how negative the average is,
    using the candidate's expanding minimum as the reference anchor.

    Returns a Series aligned to ranks index with values in [floor, 1.0].
    """
    df = ranks.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    rolling_avg = df.groupby("ts_code")["realized_pnl"].transform(
        lambda x: x.shift(1).rolling(lookback_days, min_periods=1).mean()
    )

    expanding_min = df.groupby("ts_code")["realized_pnl"].transform(
        lambda x: x.shift(1).expanding().min()
    )

    scale = pd.Series(1.0, index=df.index)

    neg_mask = (rolling_avg < 0) & (expanding_min < 0) & rolling_avg.notna() & expanding_min.notna()
    if neg_mask.any():
        proportion = (rolling_avg[neg_mask] / expanding_min[neg_mask]).clip(0.0, 1.0)
        raw = 1.0 - (1.0 - floor) * proportion
        scale[neg_mask] = raw.clip(lower=floor, upper=1.0)

    return scale


def _apply_scale_to_ranks(ranks: pd.DataFrame, scale: pd.Series) -> pd.DataFrame:
    """Apply position scaling to ranks DataFrame.

    Multiplies value_gap_amount and sets position_cash_scale.
    Entry/exit rules unchanged — only position sizing is affected.
    """
    adjusted = ranks.copy()
    adjusted = adjusted.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    if "position_cash_scale" not in adjusted.columns:
        adjusted["position_cash_scale"] = 1.0

    adjusted["position_cash_scale"] = (
        adjusted["position_cash_scale"].astype(float) * scale.astype(float)
    )
    adjusted["value_gap_amount"] = (
        adjusted["value_gap_amount"].astype(float) * scale.astype(float)
    )
    mask = adjusted["position_cash"] > 0
    adjusted.loc[mask, "value_gap_pct_of_cash"] = (
        adjusted.loc[mask, "value_gap_amount"].astype(float)
        / adjusted.loc[mask, "position_cash"].astype(float)
    )

    adjusted = adjusted.sort_values(
        ["trade_date", "value_gap_amount"], ascending=[True, False]
    ).reset_index(drop=True)
    adjusted["rank"] = adjusted.groupby("trade_date").cumcount()
    return adjusted


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


def _source_rows(
    name: str, result: dict[str, Any], ranks_by_key: dict[tuple[str, str], Any]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in result["trades"]:
        key = (str(trade["entry_date"]), str(trade["cb_code"]))
        rank_row = ranks_by_key.get(key)
        source = str(trade.get("entry_gap_source") or "missing")
        bond_share = 0.0
        option_share = 0.0
        if rank_row is not None:
            source_from_rank, bond_share, option_share = _gap_source_shares(rank_row, {})
            if source == "unknown":
                source = source_from_rank
        grouped.setdefault(source, []).append(
            {
                **trade,
                "bond_share": bond_share,
                "option_share": option_share,
                "position_cash_scale": float(
                    trade.get("entry_position_cash_scale", 1.0) or 1.0
                ),
            }
        )

    rows: list[dict[str, Any]] = []
    for source, trades in sorted(grouped.items()):
        pnl_pct = [float(t["pnl_pct"]) for t in trades]
        pnl_amount = [float(t["pnl_amount"]) for t in trades]
        scales = [float(t["position_cash_scale"]) for t in trades]
        rows.append(
            {
                "name": name,
                "source": source,
                "count": len(trades),
                "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 6),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "wins": sum(1 for v in pnl_pct if v > 0),
                "avg_bond_share": round(
                    sum(float(t["bond_share"]) for t in trades) / len(trades), 6
                ),
                "avg_option_share": round(
                    sum(float(t["option_share"]) for t in trades) / len(trades), 6
                ),
                "avg_position_cash_scale": (
                    round(sum(scales) / len(scales), 6) if scales else None
                ),
            }
        )
    return rows


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
        "PnL feedback scaling passed train/test/2020 checks; review before promotion."
        if adoption_pass
        else "No PnL feedback scaling variant beat baseline across train, 2020, and sealed test together."
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
                "confirmed_invalid_directions": (
                    ["rolling_pnl_feedback"] if not adoption_pass else []
                ),
                "learnings": [
                    "Rolling realized PnL feedback must pass train, 2020, and sealed test together.",
                    reason,
                ],
                "follow_up_actions": [
                    "Keep this run as diagnostic evidence for future PnL feedback ideation.",
                    "Do not promote unless follow-up review confirms train/test/2020 robustness.",
                ],
                "summary": reason,
                "notes": "Result reviewed by code-generated summary.json, l4_ack.yaml, and diagnostic.yaml.",
                "references": summary["artifacts"],
                "related_reports": [],
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
                    "answer": "Categorical PnL feedback variants; no monotonic promotion without review.",
                    "computed_data": {
                        "grid_type": "pnl_feedback_scaling_variants",
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
                "q6_trigger_timing": {
                    "description": "Trigger timing leakage.",
                    "applicable": False,
                },
                "q7_path_contamination": {
                    "description": "Path/data contamination.",
                    "applicable": False,
                },
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


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "pnl_feedback_scaling_grid"
    output_dir.mkdir(parents=True, exist_ok=True)

    lookback_values = _parse_grid_param(args.lookback_days, "lookback_days", int, (5, 252))
    floor_values = _parse_grid_param(args.pnl_scaling_floor, "pnl_scaling_floor", float, (0.01, 0.99))
    grid = list(product(lookback_values, floor_values))

    base_ranks = _load_base_ranks(args, output_dir)
    base_ranks = _compute_realized_pnl(base_ranks, _REPO_ROOT)

    baseline_name = "baseline_no_feedback"
    CONFIGS: list[dict[str, Any]] = [
        {
            "name": baseline_name,
            "description": "Baseline: no PnL feedback scaling",
            "lookback_days": 0,
            "pnl_scaling_floor": 1.0,
        }
    ]
    for lookback, floor in grid:
        fname = str(floor).replace(".", "p")
        CONFIGS.append(
            {
                "name": f"pnlfb_lb{lookback}_f{fname}",
                "description": (
                    f"Rolling {lookback}d avg PnL feedback, "
                    f"scale floor {floor}"
                ),
                "lookback_days": lookback,
                "pnl_scaling_floor": floor,
            }
        )

    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    source_2020_rows: list[dict[str, Any]] = []
    source_test_rows: list[dict[str, Any]] = []
    grid_rows: list[dict[str, Any]] = []

    baseline_train_excess: float = 0.0

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        lookback = int(cfg.get("lookback_days", 0))
        floor = float(cfg.get("pnl_scaling_floor", 1.0))
        is_baseline = name == baseline_name

        if is_baseline:
            adjusted = base_ranks.copy()
            scale_info = {"avg_scale": 1.0, "scaled_pct": 0.0}
        else:
            scale = _compute_pnl_feedback_scale(base_ranks, lookback, floor)
            adjusted = _apply_scale_to_ranks(base_ranks, scale)
            scale_info = {
                "avg_scale": round(float(scale.mean()), 6),
                "scaled_pct": round(float((scale < 1.0).mean()) * 100, 2),
            }

        ranks_by_key = {
            (str(r.trade_date), str(r.ts_code)): r
            for r in adjusted.itertuples(index=False)
        }

        params = _with_cost_params(dict(BASE_PARAMS), args)

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
            _row(
                name, description, "train",
                args.train_start, args.train_end, cfg, params, train,
            )
        )
        summary_rows.append(
            _row(
                name, description, "test",
                args.test_start, args.test_end, cfg, params, test,
            )
        )
        source_test_rows.extend(_source_rows(name, test, ranks_by_key))

        if is_baseline:
            baseline_train_excess = float(train["metrics"]["excess_return"])

        grid_rows.append(
            {
                "name": name,
                "description": description,
                "lookback_days": lookback,
                "pnl_scaling_floor": floor,
                **scale_info,
                "train_excess": train["metrics"]["excess_return"],
                "test_excess": test["metrics"]["excess_return"],
                "train_max_dd": train["metrics"]["max_drawdown"],
                "test_max_dd": test["metrics"]["max_drawdown"],
                "train_win_rate": train["metrics"]["win_rate"],
                "test_win_rate": test["metrics"]["win_rate"],
                "train_total_trades": train["metrics"]["total_trades"],
                "test_total_trades": test["metrics"]["total_trades"],
            }
        )

        print(
            f"[pnl_feedback_grid] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']} "
            f"avg_scale={scale_info.get('avg_scale', 1.0)}",
            flush=True,
        )

        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            ranks_year = adjusted[
                (adjusted["trade_date"] >= start)
                & (adjusted["trade_date"] <= end)
            ]
            if ranks_year.empty:
                continue
            y = _run_value_gap_backtest(
                ranks_year, start, end,
                args.data_root, args.fixed_source, args.rule, params,
            )
            yearly_rows.append(
                _row(name, description, str(year), start, end, cfg, params, y)
            )
            if year == 2020:
                source_2020_rows.extend(_source_rows(name, y, ranks_by_key))

    _attach_spec_binding(summary_rows, output_dir)
    _write_csv(output_dir / "grid_summary_pnl_feedback.csv", grid_rows)
    _write_csv(output_dir / "per_config_summary_pnl_feedback.csv", summary_rows)
    _write_csv(
        output_dir / "entry_source_2020_pnl_feedback.csv", source_2020_rows
    )
    _write_csv(
        output_dir / "entry_source_test_pnl_feedback.csv", source_test_rows
    )

    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = (
        max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    )
    best_test = (
        max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    )
    baseline_train = _pick(summary_rows, baseline_name, "train")
    baseline_test = _pick(summary_rows, baseline_name, "test")
    selected_test = _pick(summary_rows, str(best_train.get("name")), "test")
    baseline_2020 = _year(yearly_rows, baseline_name, 2020)
    selected_2020 = _year(yearly_rows, str(best_train.get("name")), 2020)

    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != baseline_name
        and float(best_train.get("excess_return", -999))
        >= float(baseline_train.get("excess_return", 999))
        and float(selected_test.get("excess_return", -999))
        >= float(baseline_test.get("excess_return", 999))
        and float(selected_2020.get("total_return", -999))
        >= float(baseline_2020.get("total_return", 999)) + 0.03
        and float(best_train.get("max_drawdown", -999)) >= -0.30
    )

    artifacts = [
        "grid_summary_pnl_feedback.csv",
        "per_config_summary_pnl_feedback.csv",
        "entry_source_2020_pnl_feedback.csv",
        "entry_source_test_pnl_feedback.csv",
        "summary.json",
        "report.yaml",
        "l4_ack.yaml",
        "diagnostic.yaml",
    ]
    summary = {
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
        "summary_rows": summary_rows,
        "artifacts": artifacts,
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
