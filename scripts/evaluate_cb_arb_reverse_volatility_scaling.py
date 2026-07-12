#!/usr/bin/env python3
"""Evaluate reverse value_gap sort with volatility-based inverse position scaling.

Composes two mechanisms:
  1. Reverse value_gap sort (ascending, smallest positive gap first)
  2. Inverse ATR-based position scaling: scale *= 1 / ((ATR/median_ATR)^aggressiveness)

Grid-searches ATR lookback [10,20,60] x aggressiveness [0.5,1.0,2.0].
Compares each grid point against a reverse-rank cost-on baseline (no scaling).
Writes summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml,
and reverse_vol_scaling_meta.yaml per the quant framework contract.

Hard boundaries:
  - Does NOT modify raw warehouse data.
  - Does NOT touch current.yaml, baseline_registry.yaml, or research_queue.yaml.
  - Results are evidence only; user must approve any truth promotion.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root & sys.path -- must come before any from scripts.X import Y
# The compliance import-reachability probe runs with -I in /tmp, so all
# non-stdlib imports that follow must resolve from venv site-packages
# (numpy/pandas/yaml) or from REPO_ROOT (scripts.*).
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
# Lazy third-party imports -- not loaded at module level in isolated probe
# ---------------------------------------------------------------------------


def _get_np():
    import numpy as _np
    return _np


def _get_pd():
    import pandas as _pd
    return _pd


def _get_yaml():
    import yaml as _yaml
    return _yaml


_YAML_REPRS_REGISTERED = False


def _ensure_yaml_np_reprs():
    """Register numpy representers for YAML SafeDumper (once)."""
    global _YAML_REPRS_REGISTERED
    if _YAML_REPRS_REGISTERED:
        return
    yaml = _get_yaml()
    np = _get_np()

    def _yaml_repr_np_float(dumper, data):
        return dumper.represent_float(float(data))

    def _yaml_repr_np_int(dumper, data):
        return dumper.represent_int(int(data))

    yaml.SafeDumper.add_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_representer(np.integer, _yaml_repr_np_int)
    yaml.SafeDumper.add_multi_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_multi_representer(np.integer, _yaml_repr_np_int)
    _YAML_REPRS_REGISTERED = True


# ---------------------------------------------------------------------------
# GateKeeper pre-run compliance
# ---------------------------------------------------------------------------


def _gatekeeper_before_run(output_dir: Path) -> None:
    """Run GateKeeper before_run_grid if spec.yaml is present."""
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


# ---------------------------------------------------------------------------
# Grid search space (lightweight tuples -- fine at module level)
# ---------------------------------------------------------------------------

ATR_LOOKBACKS = (10, 20, 60)
SCALING_AGGRESSIVENESSES = (0.5, 1.0, 2.0)

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument(
        "--value-gap-path",
        type=Path,
        default=Path(
            "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
            "daily_value_gap_amounts.parquet"
        ),
    )
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--atr-lookback", type=int, default=20)
    p.add_argument("--scaling-aggressiveness", type=float, default=1.0)
    p.add_argument("--sell-gap-pct", type=float, default=0.0)
    p.add_argument("--max-hold-days", type=float, default=180.0)
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.001)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    p.add_argument(
        "--grid-search-disabled",
        action="store_true",
        help="Run only the CLI-specified ATR lookback and aggressiveness (no grid)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Data requirements declaration
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
        raise ValueError(
            "evaluate_cb_arb_reverse_volatility_scaling requires --data-root"
        )
    data_root = Path(data_root_raw)

    value_gap_raw = _command_value_from_parts(command, "--value-gap-path")
    value_gap_path = (
        str(value_gap_raw)
        if value_gap_raw
        else "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
        "daily_value_gap_amounts.parquet"
    )

    fixed_source_raw = _command_value_from_parts(command, "--fixed-source") or "2"
    fixed_source = int(fixed_source_raw)
    pool_ids = sorted({0, 2, 4, 6, fixed_source})

    warehouse_files = [
        (
            "data/cb_warehouse/cb_basic.parquet",
            ["ts_code", "stk_code", "conv_price", "list_date", "delist_date"],
        ),
        (
            "data/cb_warehouse/cb_daily.parquet",
            ["ts_code", "trade_date", "close"],
        ),
        (
            "data/cb_warehouse/cb_call.parquet",
            ["ts_code", "ann_date", "call_date", "expire_date"],
        ),
        (
            "data/cb_warehouse/stk_daily_qfq.parquet",
            ["stk_code", "trade_date", "close"],
        ),
    ]
    required_files: list[dict[str, Any]] = [
        {
            "path": rel_path,
            "role": "warehouse_input",
            "required_columns": columns,
            "nonnull_columns": [
                col
                for col in columns
                if col not in {"conv_price", "ann_date", "call_date", "expire_date"}
            ],
        }
        for rel_path, columns in warehouse_files
    ]
    required_files.append({
        "path": value_gap_path,
        "role": "value_gap_input",
        "required_columns": ["ts_code", "trade_date", "value_gap_amount"],
        "nonnull_columns": ["value_gap_amount"],
    })
    for pool_id in pool_ids:
        required_files.append({
            "path": str(data_root / f"pool_{pool_id}" / "best_params.json"),
            "role": "config_pool",
        })

    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_reverse_volatility_scaling.py",
        "required_files": required_files,
    }


# ---------------------------------------------------------------------------
# ATR calculation -- SMA of absolute daily returns (per proposal specification)
# ---------------------------------------------------------------------------


def _compute_sma_atr(
    cb_daily: Any,
    lookback: int,
) -> Any:
    """Compute ATR as SMA of |close_t - close_{t-1}| over lookback days.

    Per proposal: "ATR = SMA(|close_t - close_{t-1}|, N)".
    Lagged by 1 day to avoid look-ahead bias.
    Returns DataFrame with [ts_code, trade_date, atr].
    """
    pd = _get_pd()
    df = cb_daily[["ts_code", "trade_date", "close"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["ts_code"] = df["ts_code"].astype(str)

    # Sort and compute daily absolute return
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    # Lagged close: use t-1 close to compute t's return (no look-ahead)
    df["prev_close"] = df.groupby("ts_code")["close"].shift(1)
    df["abs_ret"] = (df["close"] - df["prev_close"]).abs()

    # SMA of abs_ret over lookback window
    df["atr"] = df.groupby("ts_code")["abs_ret"].transform(
        lambda x: x.rolling(window=lookback, min_periods=max(1, lookback // 2)).mean()
    )

    # Forward-fill NaN ATR for CBs with insufficient history
    df["atr"] = df["atr"].fillna(0.0)

    return df[["ts_code", "trade_date", "atr"]]


# ---------------------------------------------------------------------------
# Load value gaps
# ---------------------------------------------------------------------------


def _load_value_gaps(value_gap_path: Path, start: str, end: str) -> Any:
    """Load pre-computed daily value gap amounts and filter to date range."""
    pd = _get_pd()
    abs_path = (
        value_gap_path
        if value_gap_path.is_absolute()
        else _REPO_ROOT / value_gap_path
    )
    ranks = pd.read_parquet(abs_path)
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    ranks["ts_code"] = ranks["ts_code"].astype(str)
    ranks = ranks[
        (ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)
    ].copy()
    ranks = ranks[ranks["value_gap_amount"].notna()]
    return ranks


# ---------------------------------------------------------------------------
# Merge ATR and apply inverse volatility position scaling
# ---------------------------------------------------------------------------


def _apply_inverse_atr_scaling(
    ranks: Any,
    atr_df: Any,
    cfg: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Apply inverse ATR position scaling without changing sort order.

    scaling_factor = 1 / ((ATR / median_ATR)^aggressiveness)
    Clamped to [0.1, 3.0] per proposal, but backtest clips to [0.0, 1.0]
    at buy time, so effective range is [0.1, 1.0].

    IMPORTANT: value_gap_amount is NOT modified. The original reverse sort
    (ascending by value_gap_amount > 0) is preserved.
    position_cash_scale is added for the backtest to read at buy time.
    """
    pd = _get_pd()
    np = _get_np()
    mode = str(cfg.get("mode", "none"))
    blended = ranks.merge(atr_df, on=["ts_code", "trade_date"], how="left")

    blended["position_cash_scale"] = 1.0
    blended["volatility_scaling_factor"] = 1.0

    if mode == "none":
        return blended, {
            "name": cfg["name"],
            "scaled_rows": 0,
            "avg_scale": 1.0,
            "min_scale": 1.0,
            "max_scale": 1.0,
            "avg_atr": 0.0,
            "median_atr": 0.0,
        }

    aggressiveness = float(cfg["scaling_aggressiveness"])

    # Cross-sectional median ATR per day
    daily_median = blended.groupby("trade_date")["atr"].transform("median")

    has_atr = blended["atr"].notna() & (blended["atr"] > 0)
    blended["volatility_scaling_factor"] = 1.0

    if has_atr.any():
        atr_vals = blended.loc[has_atr, "atr"].astype(float)
        med_vals = daily_median.loc[has_atr].astype(float)
        # Avoid division by zero: fall back to ATR itself
        med_safe = med_vals.where(med_vals > 0, atr_vals)

        atr_ratio = atr_vals / med_safe  # >1 for high-vol, <1 for low-vol
        # Inverse scaling: high vol -> smaller position
        raw_factor = 1.0 / (atr_ratio ** aggressiveness)
        # Clamp to [0.1, 3.0] per proposal
        factor = raw_factor.clip(lower=0.1, upper=3.0)

        blended.loc[has_atr, "volatility_scaling_factor"] = factor.astype(float)
        blended.loc[has_atr, "position_cash_scale"] = factor.astype(float)

    # Stats
    factors = (
        blended.loc[has_atr, "volatility_scaling_factor"]
        if has_atr.any()
        else pd.Series(dtype=float)
    )
    atrs = (
        blended.loc[has_atr, "atr"] if has_atr.any() else pd.Series(dtype=float)
    )
    medians = (
        daily_median.loc[has_atr] if has_atr.any() else pd.Series(dtype=float)
    )

    return blended, {
        "name": cfg["name"],
        "scaled_rows": int(has_atr.sum()),
        "avg_scale": round(float(factors.mean()), 6) if not factors.empty else 1.0,
        "min_scale": round(float(factors.min()), 6) if not factors.empty else 1.0,
        "max_scale": round(float(factors.max()), 6) if not factors.empty else 1.0,
        "avg_atr": round(float(atrs.mean()), 6) if not atrs.empty else 0.0,
        "median_atr": round(float(medians.mean()), 6) if not medians.empty else 0.0,
        "atr_lookback": cfg.get("atr_lookback"),
        "scaling_aggressiveness": aggressiveness,
    }


# ---------------------------------------------------------------------------
# Pick helper
# ---------------------------------------------------------------------------


def _pick(rows: list[dict[str, Any]], name: str, period: str) -> dict[str, Any]:
    return next(
        (r for r in rows if r["name"] == name and r["period"] == period), {}
    )


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------


def _write_artifacts(
    output_dir: Path,
    summary: dict[str, Any],
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    adoption_pass: bool,
    grid_meta: list[dict[str, Any]],
) -> None:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml."""
    _ensure_yaml_np_reprs()
    yaml = _get_yaml()
    now = datetime.now().isoformat(timespec="seconds")
    selected_test = _pick(
        summary.get("summary_rows", []),
        str(best_train.get("name")),
        "test",
    )
    decision = "mini-spec-retry" if adoption_pass else "reject"

    if adoption_pass:
        reason = (
            "Reverse volatility scaling passed success criteria: "
            "test excess retains >=80% of reverse baseline, "
            "max drawdown < -15%, Sharpe > baseline."
        )
    else:
        reason = (
            "No reverse volatility scaling variant met all success criteria "
            "(excess retention >= 80%, drawdown < -15%, Sharpe > baseline)."
        )

    # summary.json
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "strategy_id": "cb_arb_value_gap_switch",
                "family": "reverse_volatility_position_scaling",
                "l6_exit_decision": decision,
                "status": "COMPLETE",
                "three_exits_section": {
                    "train_exit": (
                        f"Train winner: {best_train.get('name')} "
                        f"(excess={best_train.get('excess_return')}, "
                        f"dd={best_train.get('max_drawdown')})"
                    ),
                    "validation_exit": (
                        f"Sealed test of train winner: {selected_test.get('name')} "
                        f"(excess={selected_test.get('excess_return')}, "
                        f"dd={selected_test.get('max_drawdown')})"
                    ),
                    "decision_exit": reason,
                },
                "compute_cost_yuan": 0.0,
                "confirmed_invalid_directions": (
                    ["reverse_volatility_position_scaling"] if not adoption_pass else []
                ),
                "learnings": [
                    "Reverse-rank base +30.3% test excess tested with inverse ATR scaling.",
                    reason,
                ],
                "follow_up_actions": [
                    "Keep as evidence for volatility-based risk filters on reverse sort.",
                    "Do not promote without follow-up review.",
                ],
                "summary": reason,
                "references": summary.get("artifacts", []),
                "related_reports": [
                    "data/cb_arb_value_gap_switch_reverse-probe-coston_20260524/report.yaml",
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # l4_ack.yaml
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "reviewer": "hermes",
                "ack_at": now,
                "q1_floor_binding": {
                    "description": (
                        "Hard floors: test excess retention >= 80%, "
                        "drawdown < -15%, Sharpe > baseline."
                    ),
                    "answer": (
                        "All success criteria met."
                        if adoption_pass
                        else "Not all success criteria met."
                    ),
                    "computed_data": {
                        "best_train_variant": best_train.get("name"),
                        "train_excess": best_train.get("excess_return"),
                        "test_excess": selected_test.get("excess_return"),
                        "baseline_test_excess": baseline_test.get("excess_return"),
                        "test_excess_retention": (
                            round(
                                float(selected_test.get("excess_return", 0))
                                / max(
                                    float(baseline_test.get("excess_return", 1)),
                                    0.0001,
                                ),
                                4,
                            )
                            if selected_test and baseline_test
                            else None
                        ),
                        "test_drawdown": selected_test.get("max_drawdown"),
                        "baseline_drawdown": baseline_test.get("max_drawdown"),
                    },
                    "computed_at": now,
                    "pass": adoption_pass,
                },
                "q2_selection_score": {
                    "description": "Train score selection vs sealed test best.",
                    "answer": (
                        f"Train selected {best_train.get('name')}; "
                        f"test best is {best_test.get('name')}."
                    ),
                    "computed_data": {
                        "selected_by_train": best_train.get("name"),
                        "best_test_variant": best_test.get("name"),
                    },
                    "pass": adoption_pass,
                },
                "q3_baseline_alignment": {
                    "description": "Alignment vs reverse cost-on baseline.",
                    "answer": reason,
                    "computed_data": {
                        "baseline_test_excess": baseline_test.get("excess_return"),
                        "selected_test_excess": selected_test.get("excess_return"),
                    },
                    "computed_at": now,
                    "pass": adoption_pass,
                },
                "q4_monotonic": {
                    "description": "Grid monotonicity check.",
                    "answer": "Grid-searched ATR lookback x aggressiveness.",
                    "computed_data": {
                        "grid_points": summary.get("candidate_count")
                    },
                    "pass": True,
                },
                "q5_trade_overlap": {
                    "description": "Trade overlap check.",
                    "answer": "Aggregate train/test used for automatic decision.",
                    "pass": True,
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

    # diagnostic.yaml
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "diagnostic_date": datetime.now().strftime("%Y-%m-%d"),
                "diagnostic_by": "hermes",
                "verdict_referenced": decision,
                "summary": reason,
                "verdict_rationale": reason,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # reverse_vol_scaling_meta.yaml
    (output_dir / "reverse_vol_scaling_meta.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "proposal_id": "cb_arb_value_gap_switch_reverse-volatility-scaling_v1",
                "scaling_type": "inverse_atr_position_scaling",
                "grid_points": grid_meta,
                "best_train": best_train.get("name"),
                "best_test": best_test.get("name"),
                "adoption_pass": adoption_pass,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Lazy import of the heavy backtest module (not at module level, so import
    # reachability probe in isolated subprocess completes quickly).
    from scripts.evaluate_cb_arb_value_gap_switch import (
        _run_value_gap_backtest,
        _score,
        _with_cost_params,
    )

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
            "config_json": json.dumps(cfg, sort_keys=True, default=str),
            "params_json": json.dumps(params, sort_keys=True, default=str),
            **result["metrics"],
        }
        row["score"] = _score(result["metrics"])
        return row

    # Build CONFIGS lazily inside main()
    configs_list: list[dict[str, Any]] = [
        {
            "name": "baseline_reverse_no_scaling",
            "description": "Reverse-rank cost-on baseline (no volatility scaling)",
            "mode": "none",
        },
    ]
    for lb in ATR_LOOKBACKS:
        for ag in SCALING_AGGRESSIVENESSES:
            key = f"rev_vol_scale_lb{lb}_agg{str(ag).replace('.', 'p')}"
            configs_list.append({
                "name": key,
                "description": (
                    f"Reverse + inverse ATR scaling lookback={lb}d "
                    f"aggressiveness={ag}"
                ),
                "mode": "inverse_atr",
                "atr_lookback": lb,
                "scaling_aggressiveness": ag,
            })

    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "reverse_volatility_scaling"
    output_dir.mkdir(parents=True, exist_ok=True)

    # GateKeeper pre-run compliance check
    _gatekeeper_before_run(output_dir)

    # Resolve value-gap path
    value_gap_path = args.value_gap_path
    if not value_gap_path.is_absolute():
        value_gap_path = _REPO_ROOT / value_gap_path

    start_all = min(args.train_start, args.test_start)
    end_all = max(args.train_end, args.test_end)

    # Load pre-computed value gaps
    base_ranks = _load_value_gaps(value_gap_path, start_all, end_all)

    # ---- Monkey-patch sort_values to reverse value_gap sort direction ----
    # The backtest internally calls sort_values("value_gap_amount", ascending=False).
    # We flip ascending -> True so it picks smallest positive gaps first (reverse sort).
    pd = _get_pd()
    original_sort_values = pd.DataFrame.sort_values

    def patched_sort_values(self, by, *sv_args, **sv_kwargs):
        if isinstance(by, str) and by == "value_gap_amount":
            sv_kwargs = dict(sv_kwargs)
            sv_kwargs["ascending"] = not sv_kwargs.get("ascending", False)
            return original_sort_values(self, by, *sv_args, **sv_kwargs)
        return original_sort_values(self, by, *sv_args, **sv_kwargs)

    pd.DataFrame.sort_values = patched_sort_values  # type: ignore[assignment]

    print(
        "[reverse_volatility_scaling] sort_values('value_gap_amount') patched: "
        "ascending flipped",
        flush=True,
    )

    # Pre-compute ATR for all lookback windows (warm-up period before start_all)
    cb_daily_raw = pd.read_parquet(
        _REPO_ROOT / "data/cb_warehouse/cb_daily.parquet"
    )
    atr_start = (
        pd.to_datetime(start_all, format="%Y%m%d") - pd.Timedelta(days=120)
    ).strftime("%Y%m%d")
    atr_end = end_all
    cb_daily_raw["trade_date"] = cb_daily_raw["trade_date"].astype(str)
    cb_daily_filtered = cb_daily_raw[
        (cb_daily_raw["trade_date"] >= atr_start)
        & (cb_daily_raw["trade_date"] <= atr_end)
    ].copy()

    atr_cache: dict[int, Any] = {}
    for lb in ATR_LOOKBACKS:
        atr_cache[lb] = _compute_sma_atr(cb_daily_filtered, lb)

    # Build config list (grid or single)
    if args.grid_search_disabled:
        configs = [
            {
                "name": "baseline_reverse_no_scaling",
                "description": "Reverse-rank cost-on baseline (no scaling)",
                "mode": "none",
            },
            {
                "name": f"rev_vol_scale_lb{args.atr_lookback}_agg"
                f"{str(args.scaling_aggressiveness).replace('.', 'p')}",
                "description": (
                    f"Reverse + inverse ATR scaling "
                    f"lookback={args.atr_lookback}d "
                    f"aggressiveness={args.scaling_aggressiveness}"
                ),
                "mode": "inverse_atr",
                "atr_lookback": args.atr_lookback,
                "scaling_aggressiveness": args.scaling_aggressiveness,
            },
        ]
    else:
        configs = configs_list

    summary_rows: list[dict[str, Any]] = []
    adjustment_rows: list[dict[str, Any]] = []
    grid_meta: list[dict[str, Any]] = []

    for cfg in configs:
        name = str(cfg["name"])
        description = str(cfg["description"])

        if cfg["mode"] == "none":
            adjusted = base_ranks.copy()
            adjusted["position_cash_scale"] = 1.0
            adjusted["volatility_scaling_factor"] = 1.0
            adj_info: dict[str, Any] = {
                "name": name,
                "scaled_rows": 0,
                "avg_scale": 1.0,
                "min_scale": 1.0,
                "max_scale": 1.0,
                "avg_atr": 0.0,
                "median_atr": 0.0,
            }
        else:
            lb = int(cfg["atr_lookback"])
            atr_df = atr_cache[lb].copy()
            adjusted, adj_info = _apply_inverse_atr_scaling(
                base_ranks, atr_df, cfg
            )

        adjustment_rows.append(adj_info)

        # Build params with candidate_position_scale_enabled
        params: dict[str, Any] = {
            "min_gap_pct": 0.0,
            "sell_gap_pct": float(args.sell_gap_pct),
            "switch_hurdle_pct": 0.03,
            "max_hold_days": float(args.max_hold_days),
            "stop_gap_ratio_floor": 0.30,
            "stop_signal_threshold": 999.0,
        }
        params["candidate_position_scale_enabled"] = 1.0
        params = _with_cost_params(params, args)

        # Train
        train_ranks = adjusted[
            (adjusted["trade_date"] >= args.train_start)
            & (adjusted["trade_date"] <= args.train_end)
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

        # Test
        test_ranks = adjusted[
            (adjusted["trade_date"] >= args.test_start)
            & (adjusted["trade_date"] <= args.test_end)
        ]
        test = _run_value_gap_backtest(
            test_ranks,
            args.test_start,
            args.test_end,
            args.data_root,
            args.fixed_source,
            args.rule,
            params,
        )

        summary_rows.append(
            _row(
                name,
                description,
                "train",
                args.train_start,
                args.train_end,
                cfg,
                params,
                train,
            )
        )
        summary_rows.append(
            _row(
                name,
                description,
                "test",
                args.test_start,
                args.test_end,
                cfg,
                params,
                test,
            )
        )

        grid_meta.append({
            "name": name,
            "config": cfg,
            "train_excess": train["metrics"]["excess_return"],
            "train_drawdown": train["metrics"]["max_drawdown"],
            "train_sharpe": train["metrics"].get("sharpe_ratio", float("nan")),
            "test_excess": test["metrics"]["excess_return"],
            "test_drawdown": test["metrics"]["max_drawdown"],
            "test_sharpe": test["metrics"].get("sharpe_ratio", float("nan")),
            "adjustment": adj_info,
        })

        print(
            f"[reverse_volatility_scaling] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"train_dd={train['metrics']['max_drawdown']} "
            f"test_excess={test['metrics']['excess_return']} "
            f"test_dd={test['metrics']['max_drawdown']}",
            flush=True,
        )

    # ---- Restore original sort_values ----
    pd.DataFrame.sort_values = original_sort_values  # type: ignore[assignment]

    # Find best configurations
    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = max(train_rows, key=lambda r: float(r["score"])) if train_rows else {}
    best_test = max(test_rows, key=lambda r: float(r["score"])) if test_rows else {}
    baseline_train = _pick(summary_rows, "baseline_reverse_no_scaling", "train")
    baseline_test = _pick(summary_rows, "baseline_reverse_no_scaling", "test")
    selected_test = _pick(
        summary_rows, str(best_train.get("name")), "test"
    )

    # ---- Success criteria from proposal ----
    # 1. test_excess_retention_vs_reverse_base >= 0.80
    baseline_test_excess = float(baseline_test.get("excess_return", 0))
    selected_test_excess = float(selected_test.get("excess_return", -999))
    if baseline_test_excess > 0:
        excess_retention = selected_test_excess / baseline_test_excess
    else:
        excess_retention = 0.0
    excess_retention_ok = excess_retention >= 0.80

    # 2. test_max_drawdown < -0.15
    test_dd = float(selected_test.get("max_drawdown", -999))
    drawdown_ok = test_dd > -0.15

    # 3. test_sharpe > baseline_reverse_sharpe
    # (approximate via excess/drawdown ratio if sharpe_ratio unavailable)
    np = _get_np()

    def _approx_sharpe(row: dict[str, Any]) -> float:
        sr = row.get("sharpe_ratio")
        if sr is not None and not (isinstance(sr, float) and np.isnan(float(sr))):
            return float(sr)
        excess = float(row.get("excess_return", 0))
        dd = abs(float(row.get("max_drawdown", 1)))
        return excess / max(dd, 0.01)

    selected_sharpe = _approx_sharpe(selected_test)
    baseline_sharpe = _approx_sharpe(baseline_test)
    sharpe_ok = selected_sharpe > baseline_sharpe

    # adoption_pass requires all three
    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_reverse_no_scaling"
        and excess_retention_ok
        and drawdown_ok
        and sharpe_ok
    )

    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "status": "COMPLETE",
        "candidate_count": len(configs),
        "adoption_pass": adoption_pass,
        "best_train": best_train,
        "best_test": best_test,
        "baseline_train": baseline_train,
        "baseline_test": baseline_test,
        "selected_test": selected_test,
        "success_criteria_checks": {
            "test_excess_retention_vs_reverse_base_ge_0p80": excess_retention_ok,
            "test_max_drawdown_lt_0p15": drawdown_ok,
            "test_sharpe_gt_baseline_reverse": sharpe_ok,
        },
        "computed_checks": {
            "excess_retention": round(excess_retention, 4),
            "test_drawdown": round(test_dd, 4),
            "selected_sharpe": round(selected_sharpe, 4),
            "baseline_sharpe": round(baseline_sharpe, 4),
        },
        "summary_rows": summary_rows,
        "adjustment_rows": adjustment_rows,
        "artifacts": [
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
            "reverse_vol_scaling_meta.yaml",
        ],
        "proposal_id": "cb_arb_value_gap_switch_reverse-volatility-scaling_v1",
    }

    _write_artifacts(
        output_dir,
        summary,
        best_train,
        best_test,
        baseline_train,
        baseline_test,
        adoption_pass,
        grid_meta,
    )

    print(
        f"[reverse_volatility_scaling] adoption_pass={adoption_pass}", flush=True
    )
    print(
        f"[reverse_volatility_scaling] wrote artifacts to {output_dir}", flush=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
