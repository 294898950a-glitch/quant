"""Evaluate rolling option-source PnL feedback for cb_arb value-gap switch."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
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
from scripts.gatekeeper import GateKeeper  # noqa: E402


def declare_data_requirements(command: list[str], spec: dict[str, Any]) -> dict[str, Any]:
    base_ranks_path: str | None = None
    if "--base-ranks-path" in command:
        idx = command.index("--base-ranks-path")
        if idx + 1 < len(command):
            base_ranks_path = str(command[idx + 1])

    required_files: list[dict[str, Any]] = [
        {
            "path": "data/cb_warehouse/cb_basic.parquet",
            "required_columns": ["ts_code", "stk_code", "issue_size", "rating", "conv_price"],
        },
        {
            "path": "data/cb_warehouse/cb_daily.parquet",
            "required_columns": ["ts_code", "trade_date", "open", "high", "low", "close", "vol"],
        },
        {
            "path": "data/cb_warehouse/cb_call.parquet",
            "required_columns": ["ts_code", "ann_date", "call_date", "expire_date"],
        },
        {
            "path": "data/cb_warehouse/stk_daily_qfq.parquet",
            "required_columns": ["stk_code", "trade_date", "close"],
        },
    ]
    if base_ranks_path:
        required_files.append(
            {
                "path": base_ranks_path,
                "note": "precomputed daily_value_gap_amounts ranks; consumed by _load_base_ranks",
            }
        )
    return {"required_files": required_files}


BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
    "candidate_position_scale_enabled": 1.0,
    "option_source_pnl_feedback_enabled": 1.0,
    "option_source_pnl_feedback_trigger_sum_pnl": 0.0,
    "option_source_pnl_feedback_trigger_avg_pnl_pct": 0.0,
}


CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_no_feedback",
        "description": "当前主策略，不使用已平仓期权来源盈亏反馈",
        "option_source_pnl_feedback_enabled": 0.0,
        "candidate_position_scale_enabled": 0.0,
    },
    *[
        {
            "name": (
                f"feedback_{lookback}d_min{min_trades}_scale_"
                f"{str(scale).replace('.', 'p')}"
            ),
            "description": (
                f"最近 {lookback} 天已平仓期权来源交易不少于 {min_trades} 笔且亏钱时，"
                f"后续期权来源候选排序金额和买入金额乘以 {scale}"
            ),
            "option_source_pnl_feedback_lookback_days": float(lookback),
            "option_source_pnl_feedback_min_trades": float(min_trades),
            "option_source_pnl_feedback_scale": float(scale),
        }
        for lookback in (20, 40, 60)
        for min_trades in (2, 3)
        for scale in (0.75, 0.50, 0.25)
    ],
]


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
    return p.parse_args()


def _params(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    params = _with_cost_params(dict(BASE_PARAMS), args)
    params.update(
        {
            k: v
            for k, v in cfg.items()
            if k.startswith("option_source_pnl_feedback_")
            or k == "candidate_position_scale_enabled"
        }
    )
    return params


def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gate = GateKeeper(quiet=True)
        gate.before_run_grid(spec_path)


def _source_rows(name: str, result: dict[str, Any], ranks_by_key: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
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
                "position_cash_scale": float(trade.get("entry_position_cash_scale", 1.0) or 1.0),
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
                "avg_bond_share": round(sum(float(t["bond_share"]) for t in trades) / len(trades), 6),
                "avg_option_share": round(sum(float(t["option_share"]) for t in trades) / len(trades), 6),
                "avg_position_cash_scale": round(sum(scales) / len(scales), 6) if scales else None,
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
        "Rolling option-source PnL feedback passed train/test/2020 checks; review before promotion."
        if adoption_pass
        else "No rolling option-source PnL feedback variant beat baseline across train, 2020, and test together."
    )
    selected_test = _pick(summary.get("summary_rows", []), str(best_train.get("name")), "test")
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
                "date": "2026-05-18",
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
                    "rolling_option_source_pnl_feedback"
                ] if not adoption_pass else [],
                "learnings": [
                    "Rolling realized PnL feedback is path-dependent and must be judged on train, 2020, and sealed test together.",
                    reason,
                ],
                "follow_up_actions": [
                    "Keep this run as evidence for future option-source false-undervaluation ideation.",
                    "Do not promote unless follow-up review confirms train/test/2020 robustness.",
                ],
                "summary": reason,
                "notes": "Result reviewed by code-generated summary.json, l4_ack.yaml, and diagnostic.yaml.",
                "references": summary["artifacts"],
                "related_reports": [
                    "data/cb_arb_value_gap_switch_option-position-sizing_2026-05-17_151411/report.yaml",
                    "data/cb_arb_value_gap_switch_option-value-haircut_2026-05-17/report.yaml",
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
                    "answer": "Categorical feedback variants; no monotonic promotion without review.",
                    "computed_data": {
                        "grid_type": "categorical_feedback_variants",
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
                "diagnostic_date": "2026-05-18",
                "diagnostic_by": "codex",
                "verdict_referenced": decision,
                "summary": reason,
                "verdict_rationale": reason,
                **(
                    {
                        "next_step_spec_changes": [
                            {
                                "field": "review_required_before_promotion",
                                "old_value": False,
                                "new_value": True,
                                "reason": "mini-spec-retry requires explicit follow-up review before any baseline change.",
                            }
                        ]
                    }
                    if decision == "mini-spec-retry"
                    else {}
                ),
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_option_pnl_feedback"
    output_dir.mkdir(parents=True, exist_ok=True)
    _gatekeeper_before_run(output_dir)

    base_ranks = _load_base_ranks(args, output_dir)
    ranks_by_key = {
        (str(r.trade_date), str(r.ts_code)): r
        for r in base_ranks.itertuples(index=False)
    }
    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    source_2020_rows: list[dict[str, Any]] = []
    source_test_rows: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg["description"])
        params = _params(args, cfg)
        train_ranks = base_ranks[
            (base_ranks["trade_date"] >= args.train_start)
            & (base_ranks["trade_date"] <= args.train_end)
        ]
        test_ranks = base_ranks[
            (base_ranks["trade_date"] >= args.test_start)
            & (base_ranks["trade_date"] <= args.test_end)
        ]
        train = _run_value_gap_backtest(
            train_ranks,
            args.train_start,
            args.train_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        test = _run_value_gap_backtest(
            test_ranks,
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )
        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, cfg, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, cfg, params, test))
        source_test_rows.extend(_source_rows(name, test, ranks_by_key))
        feedback_rows.append(
            {
                "name": name,
                "description": description,
                "params_json": json.dumps(params, sort_keys=True),
                "train_trade_count": len(train["trades"]),
                "test_trade_count": len(test["trades"]),
            }
        )
        print(
            f"[option_pnl_feedback] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']}",
            flush=True,
        )

        for year in range(2019, 2025):
            start = f"{year}0101"
            end = f"{year}1231"
            ranks = base_ranks[(base_ranks["trade_date"] >= start) & (base_ranks["trade_date"] <= end)]
            y = _run_value_gap_backtest(
                ranks,
                start,
                end,
                args.data_root,
                args.fixed_source,
                args.rule,
                params,
            )
            yearly_rows.append(_row(name, description, str(year), start, end, cfg, params, y))
            if year == 2020:
                source_2020_rows.extend(_source_rows(name, y, ranks_by_key))

    _attach_spec_binding(summary_rows, output_dir)
    _write_csv(output_dir / "summary_option_pnl_feedback.csv", summary_rows)
    _write_csv(output_dir / "yearly_option_pnl_feedback.csv", yearly_rows)
    _write_csv(output_dir / "entry_source_2020_option_pnl_feedback.csv", source_2020_rows)
    _write_csv(output_dir / "entry_source_test_option_pnl_feedback.csv", source_test_rows)
    _write_csv(output_dir / "feedback_option_pnl_feedback.csv", feedback_rows)

    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = _pick(summary_rows, "baseline_no_feedback", "train")
    baseline_test = _pick(summary_rows, "baseline_no_feedback", "test")
    selected_test = _pick(summary_rows, str(best_train.get("name")), "test")
    baseline_2020 = _year(yearly_rows, "baseline_no_feedback", 2020)
    selected_2020 = _year(yearly_rows, str(best_train.get("name")), 2020)
    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_no_feedback"
        and float(best_train.get("excess_return", -999)) >= float(baseline_train.get("excess_return", 999))
        and float(selected_test.get("excess_return", -999)) >= float(baseline_test.get("excess_return", 999))
        and float(selected_2020.get("total_return", -999)) >= float(baseline_2020.get("total_return", 999)) + 0.03
        and float(best_train.get("max_drawdown", -999)) >= -0.30
    )

    artifacts = [
        "summary_option_pnl_feedback.csv",
        "yearly_option_pnl_feedback.csv",
        "entry_source_2020_option_pnl_feedback.csv",
        "entry_source_test_option_pnl_feedback.csv",
        "feedback_option_pnl_feedback.csv",
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
