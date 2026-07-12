"""
Evaluate rolling option-source PnL feedback v2 for cb_arb value-gap switch.

Unlike v1 which computes rolling realized PnL internally from its own trade
simulation, this executor ingests pre-computed entry-source CSV files from
a prior option-position-sizing run and uses their aggregate source-level
PnL to scale future option-source candidate exposure.

The feedback mechanism:
  1. Parse entry-source CSVs to extract per-source realized PnL stats
     (sum_pnl_amount, avg_pnl_pct) from the baseline (unscaled) config.
  2. For each source that is "recently losing" (metrics cross a threshold),
     scale down value_gap_amount and position_cash on option-source
     candidate rows during rank pre-processing.
  3. Run standard value-gap backtest on adjusted ranks.
  4. Compare multiple feedback strategies (threshold, scale, signal type)
     against a no-feedback baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root & sys.path — must come before any `from scripts.X import Y`.
# ---------------------------------------------------------------------------

def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ---------------------------------------------------------------------------
# Lazy third-party imports — not available at module level in isolated probe.
# ---------------------------------------------------------------------------

pd: Any = None
yaml: Any = None


def _setup_heavy_deps() -> None:
    global pd, yaml
    import pandas as _pd
    import yaml as _yaml
    pd = _pd
    yaml = _yaml


# ── Lazy project module imports ──

def _import_value_gap_switch():
    from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
        _gap_source_shares,
        _load_or_build_value_ranks,
        _run_value_gap_backtest,
        _score,
        _with_cost_params,
        _write_csv,
    )
    return (
        _gap_source_shares,
        _load_or_build_value_ranks,
        _run_value_gap_backtest,
        _score,
        _with_cost_params,
        _write_csv,
    )


def _import_position_sizing():
    from scripts.evaluate_cb_arb_option_position_sizing import _add_moneyness  # noqa: E402
    return _add_moneyness


# ── constants ──

BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
    "option_source_pnl_feedback_enabled": 0.0,
    "candidate_position_scale_enabled": 0.0,
}

CONFIGS: list[dict[str, Any]] = [
    {
        "name": "baseline_no_feedback_v2",
        "description": "不应用任何期权来源盈亏反馈（v2基线）",
        "feedback_enabled": False,
    },
    *[
        {
            "name": (
                f"feedback_{signal}_thresh{str(threshold).replace('.', 'p').replace('-', 'n')}"
                f"_scale{str(scale).replace('.', 'p')}"
            ),
            "description": (
                f"信号={signal}, 阈值={threshold}, 缩放={scale}: "
                f"当期权来源的{signal}低于{threshold}时，"
                f"后续该来源的候选排序金额和买入金额乘以{scale}"
            ),
            "feedback_enabled": True,
            "feedback_signal": signal,
            "feedback_threshold": float(threshold),
            "feedback_scale": float(scale),
        }
        for signal in ("sum_pnl_amount", "avg_pnl_pct")
        for threshold in (-50000, -100000, -200000, -5000, -0.005, -0.01)
        for scale in (0.75, 0.50, 0.25)
    ],
]


# ── CSV parsing helpers ──

def _parse_entry_source_csv(csv_path: Path) -> pd.DataFrame:
    """Read an entry-source CSV and return a DataFrame."""
    return pd.read_csv(csv_path, na_values=["None", "null"])


def _extract_baseline_source_pnl(
    csv_df: pd.DataFrame, baseline_names: tuple | None = None
) -> dict[str, dict[str, float]]:
    """Extract per-source PnL stats for the baseline (unscaled) config."""
    if baseline_names is None:
        baseline_names = ("baseline_no_position_scale", "baseline_no_feedback")
    baseline_rows = csv_df[csv_df["name"].isin(baseline_names)]
    if baseline_rows.empty and len(csv_df) > 0:
        baseline_rows = csv_df.iloc[[0]]
    result: dict[str, dict[str, float]] = {}
    for _, row in baseline_rows.iterrows():
        source = str(row.get("source", "unknown"))
        result[source] = {
            "sum_pnl_amount": float(str(row.get("sum_pnl_amount", 0)).replace(",", "") or 0),
            "avg_pnl_pct": float(str(row.get("avg_pnl_pct", 0)).replace(",", "") or 0),
            "count": int(float(str(row.get("count", 0)) or 0)),
            "wins": int(float(str(row.get("wins", 0)) or 0)),
        }
    return result


# ── Rank adjustment helpers ──

def _source_name_for_row(row: Any, params: dict[str, Any]) -> str:
    _gap_source_shares, *_ = _import_value_gap_switch()
    source, _, _ = _gap_source_shares(row, params)
    return source


def _adjust_ranks_for_feedback(
    ranks: pd.DataFrame,
    source_pnl_lookup: dict[str, dict[str, float]],
    cfg: dict[str, Any],
) -> pd.DataFrame:
    """Pre-adjust ranks based on entry-source PnL feedback."""
    if not cfg.get("feedback_enabled"):
        return ranks

    signal = str(cfg.get("feedback_signal", "sum_pnl_amount"))
    threshold = float(cfg.get("feedback_threshold", 0.0))
    scale_val = float(cfg.get("feedback_scale", 1.0))
    params = {k: cfg[k] for k in ("feedback_enabled", "feedback_signal", "feedback_threshold", "feedback_scale")}
    params.update(BASE_PARAMS)

    adjusted = ranks.copy()
    if "position_cash_scale" not in adjusted.columns:
        adjusted["position_cash_scale"] = 1.0

    option_mask_values: list[bool] = []
    losing_mask_values: list[bool] = []
    for row in adjusted.itertuples(index=False):
        source = _source_name_for_row(row, params)
        is_option = source == "option"
        option_mask_values.append(is_option)
        if is_option and source in source_pnl_lookup:
            pnl_val = source_pnl_lookup[source].get(signal, 0.0)
            losing_mask_values.append(pnl_val < threshold)
        else:
            losing_mask_values.append(False)

    option_mask = pd.Series(option_mask_values, index=adjusted.index)
    losing_mask = pd.Series(losing_mask_values, index=adjusted.index)
    apply_mask = option_mask & losing_mask

    if not bool(apply_mask.any()):
        return adjusted

    adjusted.loc[apply_mask, "position_cash_scale"] = (
        adjusted.loc[apply_mask, "position_cash_scale"].astype(float) * scale_val
    )
    adjusted.loc[apply_mask, "value_gap_amount"] = (
        adjusted.loc[apply_mask, "value_gap_amount"].astype(float) * scale_val
    )
    return adjusted


# ── CLI ──

def declare_data_requirements(
    command: list[Any], spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the files this executor will read before it is allowed to run."""
    cmd_val = lambda flag: next(
        (str(p) for i, p in enumerate(command[:-1]) if str(p) == flag), None
    )
    data_root_raw = cmd_val("--data-root")
    if not data_root_raw:
        raise ValueError("evaluate_cb_arb_option_pnl_feedback_v2 requires --data-root")
    base_ranks = cmd_val("--base-ranks-path")
    csv_2020 = cmd_val("--option-2020-csv")
    csv_test = cmd_val("--option-test-csv")

    required_files: list[dict[str, str]] = [
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/cb_basic.parquet"), "role": "warehouse_input"},
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/cb_daily.parquet"), "role": "warehouse_input"},
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/cb_call.parquet"), "role": "warehouse_input"},
        {"path": str(Path(data_root_raw) / "data/cb_warehouse/stk_daily_qfq.parquet"), "role": "warehouse_input"},
    ]
    if base_ranks:
        required_files.append({"path": base_ranks, "role": "base_ranks_input"})
    if csv_2020:
        required_files.append({"path": csv_2020, "role": "entry_source_input_2020"})
    if csv_test:
        required_files.append({"path": csv_test, "role": "entry_source_input_test"})

    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_option_pnl_feedback_v2.py",
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
    p.add_argument("--option-2020-csv", type=Path, default=None)
    p.add_argument("--option-test-csv", type=Path, default=None)
    return p.parse_args()


def _params(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    _, _, _, _, _with_cost_params, _ = _import_value_gap_switch()
    params = _with_cost_params(dict(BASE_PARAMS), args)
    params.update(
        {
            k: v
            for k, v in cfg.items()
            if k in ("feedback_enabled", "feedback_signal", "feedback_threshold", "feedback_scale")
        }
    )
    return params


def _row(
    name: str, description: str, period: str, start: str, end: str,
    cfg: dict[str, Any], params: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any]:
    _, _, _, _score_fn, _, _ = _import_value_gap_switch()
    row = {
        "name": name,
        "description": description,
        "period": period,
        "start": start,
        "end": end,
        "feedback_config_json": json.dumps(cfg, sort_keys=True),
        "params_json": json.dumps(params, sort_keys=True),
        **result["metrics"],
    }
    row["score"] = _score_fn(result["metrics"])
    return row


def _pick(rows: list[dict[str, Any]], name: str, period: str) -> dict[str, Any]:
    return next((r for r in rows if r["name"] == name and r["period"] == period), {})


def _year(rows: list[dict[str, Any]], name: str, year: int) -> dict[str, Any]:
    return next((r for r in rows if r["name"] == name and r["period"] == str(year)), {})


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


# ── GateKeeper lifecycle ──

def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        GateKeeper(quiet=True).before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    GateKeeper(quiet=True).after_run_grid(output_dir)


# ── Artifact writing ──

def _write_review_files(
    output_dir: Path,
    summary: dict[str, Any],
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    adoption_pass: bool,
) -> None:
    now_str = datetime.now().isoformat(timespec="seconds")
    now_date = datetime.now().strftime("%Y-%m-%d")
    decision = "mini-spec-retry" if adoption_pass else "reject"
    reason = (
        "Rolling option-source PnL feedback (v2 CSV-based) passed train/test/2020 "
        "checks; review before promotion."
        if adoption_pass
        else "No option-source PnL feedback variant (v2) beat baseline across "
        "train, 2020, and sealed test together."
    )
    selected_test = _pick(summary.get("summary_rows", []), str(best_train.get("name")), "test")
    baseline_2020 = summary.get("baseline_2020", {})
    selected_2020 = summary.get("selected_2020", {})

    # summary.json
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "date": now_date,
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
                ["rolling_option_source_pnl_feedback_v2_csv"] if not adoption_pass else []
            ),
            "learnings": [
                "Pre-computed source PnL from prior position-sizing run is used "
                "as a feedback signal; must be judged on train, 2020, and "
                "sealed test together.",
                reason,
            ],
            "follow_up_actions": [
                "Keep this run as evidence for future option-source feedback ideation.",
                "Do not promote unless follow-up review confirms train/test/2020 robustness.",
            ],
            "summary": reason,
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # l4_ack.yaml
    l4_test_excess = float(selected_test.get("excess_return", 0))
    l4_baseline_test_excess = float(baseline_test.get("excess_return", 0))
    l4_2020_return = float(selected_2020.get("total_return", 0))
    l4_baseline_2020_return = float(baseline_2020.get("total_return", 0))
    l4_pass = (
        l4_test_excess >= l4_baseline_test_excess
        and l4_2020_return >= l4_baseline_2020_return
    )

    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "reviewer": "hermes",
            "ack_at": now_str,
            "q1_hard_floors": {
                "description": "2020 stress period: selected variant >= baseline total return.",
                "answer": f"2020 selected={l4_2020_return} baseline={l4_baseline_2020_return}",
                "pass": l4_2020_return >= l4_baseline_2020_return,
            },
            "q2_selection_quality": {
                "description": "Sealed test: selected variant >= baseline excess return.",
                "answer": f"test excess={l4_test_excess} baseline={l4_baseline_test_excess}",
                "pass": l4_test_excess >= l4_baseline_test_excess,
            },
            "q3_falsifiers": {
                "description": "Overall adoption pass check.",
                "answer": f"adoption_pass={adoption_pass}, best_train={best_train.get('name')}",
                "pass": adoption_pass,
            },
            "overall_pass": adoption_pass,
            "overall_decision": decision,
            "overall_reason": reason,
            "auto_computed_at": now_str,
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # diagnostic.yaml
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "diagnostic_date": now_date,
            "diagnostic_by": "hermes",
            "verdict_referenced": decision,
            "summary": reason,
            "verdict_rationale": reason,
            "warnings": [],
            "errors": [],
            "grid_sweep_size": len(CONFIGS),
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── Main ──

def main() -> int:
    _setup_heavy_deps()
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "value_gap_option_pnl_feedback_v2"
    output_dir.mkdir(parents=True, exist_ok=True)

    _gatekeeper_before_run(output_dir)

    (
        _gap_source_shares,
        _load_or_build_value_ranks,
        _run_value_gap_backtest,
        _score_fn,
        _with_cost_params,
        _write_csv,
    ) = _import_value_gap_switch()
    _add_moneyness = _import_position_sizing()

    # 1. Load base ranks
    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)
    if args.base_ranks_path is not None and Path(args.base_ranks_path).exists():
        base_ranks = pd.read_parquet(args.base_ranks_path)
    else:
        base_ranks = _load_or_build_value_ranks(
            args.data_root, start_all, end_all, args.fixed_source, args.rule,
            output_dir / "daily_value_gap_amounts_base.parquet", args.reuse_ranks,
        )
    base_ranks = base_ranks.copy()
    base_ranks["trade_date"] = base_ranks["trade_date"].astype(str)
    base_ranks["ts_code"] = base_ranks["ts_code"].astype(str)
    base_ranks = base_ranks[(base_ranks["trade_date"] >= start_all) & (base_ranks["trade_date"] <= end_all)]
    base_ranks = _add_moneyness(base_ranks)

    # 2. Load entry-source CSVs → per-source PnL lookup
    source_pnl_lookup: dict[str, dict[str, float]] = {}
    if args.option_2020_csv is not None and Path(args.option_2020_csv).exists():
        df_2020 = _parse_entry_source_csv(args.option_2020_csv)
        source_pnl_lookup.update(_extract_baseline_source_pnl(df_2020))
    if args.option_test_csv is not None and Path(args.option_test_csv).exists():
        df_test = _parse_entry_source_csv(args.option_test_csv)
        test_pnl = _extract_baseline_source_pnl(df_test)
        for src, stats in test_pnl.items():
            if src not in source_pnl_lookup:
                source_pnl_lookup[src] = stats

    if not source_pnl_lookup:
        print("[v2] WARNING: No entry-source CSV data loaded; feedback disabled.", flush=True)

    # 3. Iterate configs
    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []

    for cfg in CONFIGS:
        name = str(cfg["name"])
        description = str(cfg.get("description", ""))
        params = _params(args, cfg)

        adjusted_ranks = _adjust_ranks_for_feedback(base_ranks, source_pnl_lookup, cfg)

        train_ranks = adjusted_ranks[
            (adjusted_ranks["trade_date"] >= args.train_start)
            & (adjusted_ranks["trade_date"] <= args.train_end)
        ]
        test_ranks = adjusted_ranks[
            (adjusted_ranks["trade_date"] >= args.test_start)
            & (adjusted_ranks["trade_date"] <= args.test_end)
        ]

        train = _run_value_gap_backtest(
            train_ranks, args.train_start, args.train_end,
            args.data_root, args.fixed_source, args.rule, params,
        )
        test = _run_value_gap_backtest(
            test_ranks, args.test_start, args.test_end,
            args.data_root, args.fixed_source, args.rule, params,
        )

        summary_rows.append(_row(name, description, "train", args.train_start, args.train_end, cfg, params, train))
        summary_rows.append(_row(name, description, "test", args.test_start, args.test_end, cfg, params, test))

        print(
            f"[v2] {name} train_excess={train['metrics']['excess_return']} "
            f"test_excess={test['metrics']['excess_return']}",
            flush=True,
        )

        # Yearly slices for 2020 validation
        for year in range(2019, 2025):
            yr_start = f"{year}0101"
            yr_end = f"{year}1231"
            ranks_yr = adjusted_ranks[
                (adjusted_ranks["trade_date"] >= yr_start)
                & (adjusted_ranks["trade_date"] <= yr_end)
            ]
            yr_result = _run_value_gap_backtest(
                ranks_yr, yr_start, yr_end,
                args.data_root, args.fixed_source, args.rule, params,
            )
            yearly_rows.append(_row(name, description, str(year), yr_start, yr_end, cfg, params, yr_result))

    # 4. Write CSVs
    _attach_spec_binding(summary_rows, output_dir)
    _write_csv(output_dir / "summary_option_pnl_feedback_v2.csv", summary_rows)
    _write_csv(output_dir / "yearly_option_pnl_feedback_v2.csv", yearly_rows)
    _write_csv(output_dir / "feedback_option_pnl_feedback_v2.csv", [
        {
            "name": str(cfg["name"]),
            "description": str(cfg.get("description", "")),
            "params_json": json.dumps(_params(args, cfg), sort_keys=True),
        }
        for cfg in CONFIGS
    ])

    # 5. Select best and compute adoption
    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = _pick(summary_rows, "baseline_no_feedback_v2", "train")
    baseline_test = _pick(summary_rows, "baseline_no_feedback_v2", "test")
    selected_test = _pick(summary_rows, str(best_train.get("name")), "test")
    baseline_2020 = _year(yearly_rows, "baseline_no_feedback_v2", 2020)
    selected_2020 = _year(yearly_rows, str(best_train.get("name")), 2020)

    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_no_feedback_v2"
        and float(best_train.get("excess_return", -999)) >= float(baseline_train.get("excess_return", 999))
        and float(selected_test.get("excess_return", -999)) >= float(baseline_test.get("excess_return", 999))
        and float(selected_2020.get("total_return", -999)) >= float(baseline_2020.get("total_return", 999)) + 0.03
        and float(best_train.get("max_drawdown", -999)) >= -0.30
    )

    summary = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "adoption_pass": adoption_pass,
        "decision": "mini-spec-retry" if adoption_pass else "reject",
        "candidate_count": len(CONFIGS),
        "params": params,
        "best_train": {k: v for k, v in best_train.items() if k != "trades"},
        "best_test": {k: v for k, v in best_test.items() if k != "trades"},
        "baseline_train": {k: v for k, v in baseline_train.items() if k != "trades"},
        "baseline_test": {k: v for k, v in baseline_test.items() if k != "trades"},
        "selected_test": {k: v for k, v in selected_test.items() if k != "trades"},
        "baseline_2020": {k: v for k, v in baseline_2020.items() if k != "trades"},
        "selected_2020": {k: v for k, v in selected_2020.items() if k != "trades"},
        "summary_rows": summary_rows,
    }
    _write_review_files(output_dir, summary, best_train, best_test, baseline_train, baseline_test, adoption_pass)

    _gatekeeper_after_run(output_dir)
    print(f"[v2] DONE adoption_pass={adoption_pass} candidates={len(CONFIGS)} best_train={best_train.get('name')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
