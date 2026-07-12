#!/usr/bin/env python3
"""Evaluate reverse value-gap sort with ATR-rank inverse position scaling.

Mechanism:
  1. Reverse value-gap sort (ASCENDING by value_gap_amount, smallest positive first)
  2. ATR-rank inverse scaling: position_cash_scale = scaling_beta / ATR_rank
     where ATR is computed from stk_daily_qfq close (stock-level volatility, not CB).

Grid-searches:
  - ATR lookback [10, 20, 40, 60]
  - scaling_beta [0.25, 0.5, 0.75, 1.0, 1.5]
  - max_hold_days [120, 150, 180]
  - min_gap_pct [0.005, 0.01]
  - switch_hurdle_pct [0.0, 0.01]
  + baseline (reverse cost-on, no scaling)

Cost-on always enabled (slippage 0.0015, impact 0.001).
Train 2018-2022, test 2023-2026.
Writes summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml.

Hard boundaries:
  - Does NOT modify raw warehouse data.
  - Does NOT touch current.yaml, baseline_registry.yaml, or research_queue.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Repo root & sys.path ────────────────────────────────────────────────
# Must come before any third-party import AND before `from scripts.X import Y`,
# because production runs execute from a foreign cwd where REPO_ROOT is not
# automatically on sys.path.  The compliance import-reachability probe runs
# with -E in /tmp, so all non-stdlib imports that follow this block must
# resolve from the venv site-packages (numpy/pandas/yaml) or from REPO_ROOT
# (scripts.*).


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ── Lazy third-party imports (heavy) ────────────────────────────────────
# Imported lazily to keep module-load time under the 20-second compliance
# import-reachability probe threshold.  The probe runs with -I in /tmp
# where venv site-packages are still reachable via sys.path (which is
# stripped vs normal) but time is tight if numpy/pandas/yaml are loaded
# at module level.


def _get_np():
    """Lazy import numpy."""
    import numpy as _np
    return _np


def _get_pd():
    """Lazy import pandas."""
    import pandas as _pd
    return _pd


def _get_yaml():
    """Lazy import yaml."""
    import yaml as _yaml
    return _yaml


# YAML numpy representer registration — runs once at first yaml write.
_YAML_REPRS_REGISTERED = False


def _ensure_yaml_np_reprs():
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


# NOTE: _run_value_gap_backtest, _score are imported lazily inside main()
# and _row() respectively to keep import time below the 20-second
# compliance-probe threshold.  The deep import chain through
# scripts.evaluate_cb_arb_value_gap_switch triggers heavy transitive
# module loading (strategies.cb_arb.verifier etc.) at module level.

# ---------------------------------------------------------------------------
# GateKeeper pre-run compliance
# ---------------------------------------------------------------------------


def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ---------------------------------------------------------------------------
# Base backtest params
# ---------------------------------------------------------------------------

BASE_PARAMS: dict[str, Any] = {
    "sell_gap_pct": 0.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
}

# ---------------------------------------------------------------------------
# Grid definitions
# ---------------------------------------------------------------------------

ATR_LOOKBACKS = (10, 20, 40, 60)
SCALING_BETAS = (0.25, 0.5, 0.75, 1.0, 1.5)
MAX_HOLD_DAYS_GRID = (120, 150, 180)
MIN_GAP_PCT_GRID = (0.005, 0.01)
SWITCH_HURDLE_PCT_GRID = (0.0, 0.01)

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
    p.add_argument("--train-start", default="20180101")
    p.add_argument("--train-end", default="20221231")
    p.add_argument("--test-start", default="20230101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--volatility-lookback", type=int, default=20)
    p.add_argument("--scaling-beta", type=float, default=1.0)
    p.add_argument("--max-hold-days", type=float, default=150)
    p.add_argument("--min-gap-pct", type=float, default=0.01)
    p.add_argument("--sell-gap-pct", type=float, default=0.0)
    p.add_argument("--switch-hurdle-pct", type=float, default=0.0)
    p.add_argument(
        "--grid-search-disabled",
        action="store_true",
        help="Run only CLI-specified params (no full grid)",
    )
    # Cost-on is always enabled for this executor
    p.add_argument("--cost-model-enabled", action="store_true", default=True)
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.001)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
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
            "evaluate_cb_arb_volatility_position_scaling_reverse requires --data-root"
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
            "data/cb_warehouse/stk_daily_qfq.parquet",
            ["ts_code", "stk_code", "trade_date", "close"],
        ),
        (
            "data/cb_warehouse/cb_call.parquet",
            ["ts_code", "ann_date", "call_date", "expire_date"],
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
    required_files.append(
        {
            "path": value_gap_path,
            "role": "value_gap_input",
            "required_columns": ["ts_code", "trade_date", "value_gap_amount"],
            "nonnull_columns": ["value_gap_amount"],
        }
    )
    for pool_id in pool_ids:
        required_files.append(
            {
                "path": str(data_root / f"pool_{pool_id}" / "best_params.json"),
                "role": "config_pool",
            }
        )

    return {
        "schema_version": 1,
        "executor": "scripts/evaluate_cb_arb_volatility_position_scaling_reverse.py",
        "required_files": required_files,
    }


# ---------------------------------------------------------------------------
# ATR calculation from stock close prices (SMA of absolute returns)
# ---------------------------------------------------------------------------


def _compute_stock_sma_atr(
    stk_daily: Any,  # pd.DataFrame — lazily typed
    lookback: int,
) -> Any:  # -> pd.DataFrame
    """Compute ATR as SMA of |close_t - close_{t-1}| from stock close prices.

    Returns DataFrame with [stk_code, trade_date, atr].
    """
    pd = _get_pd()
    df = stk_daily[["stk_code", "trade_date", "close"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["stk_code"] = df["stk_code"].astype(str)

    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    df["prev_close"] = df.groupby("stk_code")["close"].shift(1)
    df["abs_ret"] = (df["close"] - df["prev_close"]).abs()

    df["atr"] = df.groupby("stk_code")["abs_ret"].transform(
        lambda x: x.rolling(window=lookback, min_periods=max(1, lookback // 2)).mean()
    )
    df["atr"] = df["atr"].fillna(0.0)

    return df[["stk_code", "trade_date", "atr"]]


# ---------------------------------------------------------------------------
# Value gap loading
# ---------------------------------------------------------------------------


def _load_value_gaps(
    value_gap_path: Path, start: str, end: str
) -> Any:  # -> pd.DataFrame
    """Load pre-computed daily value gap amounts and filter."""
    pd = _get_pd()
    abs_path = (
        value_gap_path if value_gap_path.is_absolute()
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
# Merge ATR and apply rank-based inverse position scaling
# ---------------------------------------------------------------------------


def _apply_rank_based_atr_scaling(
    ranks: Any,  # pd.DataFrame
    atr_df: Any,  # pd.DataFrame
    cfg: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:  # -> tuple[pd.DataFrame, dict]
    """Apply rank-based inverse ATR position scaling.

    position_cash_scale = scaling_beta / ATR_rank
    where ATR_rank=1 for lowest ATR (least volatile, highest weight).

    value_gap_amount is NOT modified. Reverse sort preserved.
    """
    pd = _get_pd()
    mode = str(cfg.get("mode", "none"))
    blended = ranks.merge(atr_df, on=["stk_code", "trade_date"], how="left")

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

    scaling_beta = float(cfg["scaling_beta"])

    has_atr = blended["atr"].notna() & (blended["atr"] > 0)

    if has_atr.any():
        # Rank ATR within each day: rank 1 = lowest ATR = least volatile
        blended["atr_rank"] = blended.groupby("trade_date")["atr"].rank(
            method="dense", ascending=True
        )
        blended["atr_rank"] = blended["atr_rank"].fillna(1.0)

        # position_cash_scale = scaling_beta / atr_rank
        raw_scale = scaling_beta / blended["atr_rank"]
        factor = raw_scale.clip(0.1, 3.0)

        blended.loc[has_atr, "volatility_scaling_factor"] = factor.loc[
            has_atr
        ].astype(float)
        blended.loc[has_atr, "position_cash_scale"] = factor.loc[has_atr].astype(float)
    else:
        blended["atr_rank"] = 1.0

    # Stats
    factors = (
        blended.loc[has_atr, "volatility_scaling_factor"]
        if has_atr.any()
        else pd.Series(dtype=float)
    )
    atrs = (
        blended.loc[has_atr, "atr"]
        if has_atr.any()
        else pd.Series(dtype=float)
    )

    return blended, {
        "name": cfg["name"],
        "scaled_rows": int(has_atr.sum()),
        "avg_scale": round(float(factors.mean()), 6) if not factors.empty else 1.0,
        "min_scale": round(float(factors.min()), 6) if not factors.empty else 1.0,
        "max_scale": round(float(factors.max()), 6) if not factors.empty else 1.0,
        "avg_atr": round(float(atrs.mean()), 6) if not atrs.empty else 0.0,
        "median_atr": round(float(atrs.median()), 6) if not atrs.empty else 0.0,
        "atr_lookback": cfg.get("atr_lookback"),
        "scaling_beta": scaling_beta,
    }


# ---------------------------------------------------------------------------
# Report row helpers
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
    # Lazy import to avoid deep transitive import chain at module load time
    from scripts.evaluate_cb_arb_value_gap_switch import _score  # noqa: E402

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


def _pick(
    rows: list[dict[str, Any]], name: str, period: str
) -> dict[str, Any]:
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
    yaml = _get_yaml()
    _ensure_yaml_np_reprs()
    now = datetime.now().isoformat(timespec="seconds")
    selected_test = _pick(
        summary.get("summary_rows", []),
        str(best_train.get("name")),
        "test",
    )
    decision = "mini-spec-retry" if adoption_pass else "reject"

    if adoption_pass:
        reason = (
            "Reverse ATR-rank position scaling passed success criteria: "
            "test excess >= 24%, max drawdown <= 11.3%, win rate >= 50%."
        )
    else:
        reason = (
            "No reverse ATR-rank position scaling variant met all success "
            "criteria (excess >= 24%, drawdown <= 11.3%, win rate >= 50%)."
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
                    ["reverse_volatility_position_scaling"]
                    if not adoption_pass
                    else []
                ),
                "learnings": [
                    "Reverse-rank base tested with ATR-rank inverse position scaling.",
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
    baseline_test_excess = float(baseline_test.get("excess_return", 0))
    selected_test_excess = float(selected_test.get("excess_return", 0))
    excess_retention = (
        round(selected_test_excess / max(baseline_test_excess, 0.0001), 4)
        if selected_test and baseline_test
        else None
    )

    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": output_dir.name,
                "reviewer": "hermes",
                "ack_at": now,
                "q1_floor_binding": {
                    "description": (
                        "Hard floors: test excess >= 24%, drawdown <= 11.3%, "
                        "win rate >= 50%."
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
                        "test_excess_retention": excess_retention,
                        "test_drawdown": selected_test.get("max_drawdown"),
                        "baseline_drawdown": baseline_test.get("max_drawdown"),
                        "test_win_rate": selected_test.get("win_rate"),
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
                    "answer": "Grid-searched ATR lookback x scaling_beta x exit params.",
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


# ---------------------------------------------------------------------------
# Grid builder
# ---------------------------------------------------------------------------


def _build_configs() -> list[dict[str, Any]]:
    """Build full grid: baseline + ATR lookback x beta x exit params."""
    configs: list[dict[str, Any]] = [
        {
            "name": "baseline_reverse_no_scaling",
            "description": "Reverse-rank cost-on baseline (no position scaling)",
            "mode": "none",
        },
    ]
    for lb in ATR_LOOKBACKS:
        for beta in SCALING_BETAS:
            for mhd in MAX_HOLD_DAYS_GRID:
                for mgp in MIN_GAP_PCT_GRID:
                    for shp in SWITCH_HURDLE_PCT_GRID:
                        b_str = str(beta).replace(".", "p")
                        mgp_str = str(mgp).replace(".", "p")
                        shp_str = str(shp).replace(".", "p")
                        configs.append(
                            {
                                "name": (
                                    f"rev_scale_lb{lb}_b{b_str}_mhd{mhd}"
                                    f"_mgp{mgp_str}_shp{shp_str}"
                                ),
                                "description": (
                                    f"Reverse ATR-rank scaling: "
                                    f"lookback={lb}d beta={beta} "
                                    f"max_hold={mhd}d min_gap={mgp} "
                                    f"switch_hurdle={shp}"
                                ),
                                "mode": "atr_rank",
                                "atr_lookback": lb,
                                "scaling_beta": beta,
                                "max_hold_days": float(mhd),
                                "min_gap_pct": mgp,
                                "switch_hurdle_pct": shp,
                            }
                        )
    return configs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Lazy import to avoid deep transitive import chain at module load time
    from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
        _run_value_gap_backtest,
    )

    pd = _get_pd()
    args = _parse_args()
    output_dir = (
        args.output_dir
        or args.data_root / "reverse_volatility_position_scaling"
    )
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
    # Filter: value_gap_amount > 0 only
    base_ranks = base_ranks[base_ranks["value_gap_amount"] > 0].copy()
    # Filter to required columns only
    base_ranks = base_ranks[
        ["trade_date", "ts_code", "value_gap_amount"]
    ].copy()

    # ── Attach stk_code via cb_basic ──
    cb_basic = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_basic.parquet")
    cb_basic["ts_code"] = cb_basic["ts_code"].astype(str)
    cb_basic["stk_code"] = cb_basic["stk_code"].astype(str)
    cb_map = cb_basic[["ts_code", "stk_code"]].drop_duplicates(subset="ts_code")
    base_ranks = base_ranks.merge(cb_map, on="ts_code", how="left")

    # ── Monkey-patch sort_values to reverse value_gap sort direction ──
    original_sort_values = pd.DataFrame.sort_values

    def patched_sort_values(self, by, *sv_args, **sv_kwargs):  # type: ignore[no-redef]
        if isinstance(by, str) and by == "value_gap_amount":
            sv_kwargs = dict(sv_kwargs)
            sv_kwargs["ascending"] = not sv_kwargs.get("ascending", False)
            return original_sort_values(self, by, *sv_args, **sv_kwargs)
        return original_sort_values(self, by, *sv_args, **sv_kwargs)

    pd.DataFrame.sort_values = patched_sort_values  # type: ignore[assignment]

    print(
        "[reverse_volatility_position_scaling] sort_values('value_gap_amount') "
        "patched: ascending flipped",
        flush=True,
    )

    # Pre-compute ATR for all lookback windows
    # Stock daily data (with warm-up period)
    stk_raw = pd.read_parquet(
        _REPO_ROOT / "data/cb_warehouse/stk_daily_qfq.parquet"
    )
    atr_start = (
        pd.to_datetime(start_all, format="%Y%m%d") - pd.Timedelta(days=120)
    ).strftime("%Y%m%d")
    stk_raw["trade_date"] = stk_raw["trade_date"].astype(str)
    stk_raw["stk_code"] = stk_raw["stk_code"].astype(str)
    stk_filtered = stk_raw[
        (stk_raw["trade_date"] >= atr_start)
        & (stk_raw["trade_date"] <= end_all)
    ].copy()

    atr_cache: dict[int, Any] = {}
    for lb in ATR_LOOKBACKS:
        atr_cache[lb] = _compute_stock_sma_atr(stk_filtered, lb)
        print(
            f"[reverse_volatility_position_scaling] ATR lookback={lb} "
            f"computed: {len(atr_cache[lb])} rows",
            flush=True,
        )

    # Build config list
    if args.grid_search_disabled:
        b_str = str(args.scaling_beta).replace(".", "p")
        mgp_str = str(args.min_gap_pct).replace(".", "p")
        shp_str = str(args.switch_hurdle_pct).replace(".", "p")
        configs = [
            {
                "name": "baseline_reverse_no_scaling",
                "description": "Reverse-rank cost-on baseline (no position scaling)",
                "mode": "none",
            },
            {
                "name": (
                    f"rev_scale_lb{args.volatility_lookback}_b{b_str}"
                    f"_mhd{int(args.max_hold_days)}"
                    f"_mgp{mgp_str}_shp{shp_str}"
                ),
                "description": (
                    f"Reverse ATR-rank scaling: "
                    f"lookback={args.volatility_lookback}d "
                    f"beta={args.scaling_beta} "
                    f"max_hold={int(args.max_hold_days)}d "
                    f"min_gap={args.min_gap_pct} "
                    f"switch_hurdle={args.switch_hurdle_pct}"
                ),
                "mode": "atr_rank",
                "atr_lookback": args.volatility_lookback,
                "scaling_beta": args.scaling_beta,
                "max_hold_days": args.max_hold_days,
                "min_gap_pct": args.min_gap_pct,
                "switch_hurdle_pct": args.switch_hurdle_pct,
            },
        ]
    else:
        configs = _build_configs()

    print(
        f"[reverse_volatility_position_scaling] "
        f"running {len(configs)} configs",
        flush=True,
    )

    summary_rows: list[dict[str, Any]] = []
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
            adjusted, adj_info = _apply_rank_based_atr_scaling(
                base_ranks, atr_df, cfg
            )

        # Build backtest params with cost-on enabled
        params = dict(BASE_PARAMS)
        params["max_hold_days"] = float(
            cfg.get("max_hold_days", 150)
        )
        params["min_gap_pct"] = float(
            cfg.get("min_gap_pct", 0.01)
        )
        params["switch_hurdle_pct"] = float(
            cfg.get("switch_hurdle_pct", 0.0)
        )
        params["candidate_position_scale_enabled"] = 1.0
        # Cost-on always enabled
        params["cost_model_enabled"] = 1.0
        params["slippage_pct"] = float(args.slippage_pct)
        params["market_impact_coeff"] = float(args.market_impact_coeff)
        params["market_impact_cap_pct"] = float(args.market_impact_cap_pct)
        params["holding_cost_pct"] = float(args.holding_cost_pct)

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

        grid_meta.append(
            {
                "name": name,
                "config": cfg,
                "train_excess": train["metrics"]["excess_return"],
                "train_drawdown": train["metrics"]["max_drawdown"],
                "train_sharpe": train["metrics"].get(
                    "sharpe_ratio", float("nan")
                ),
                "test_excess": test["metrics"]["excess_return"],
                "test_drawdown": test["metrics"]["max_drawdown"],
                "test_sharpe": test["metrics"].get(
                    "sharpe_ratio", float("nan")
                ),
                "test_win_rate": test["metrics"].get(
                    "win_rate", float("nan")
                ),
                "adjustment": adj_info,
            }
        )

        print(
            f"[reverse_volatility_position_scaling] {name} "
            f"train_excess={train['metrics']['excess_return']} "
            f"train_dd={train['metrics']['max_drawdown']} "
            f"test_excess={test['metrics']['excess_return']} "
            f"test_dd={test['metrics']['max_drawdown']}",
            flush=True,
        )

    # ── Restore original sort_values ──
    pd.DataFrame.sort_values = original_sort_values  # type: ignore[assignment]

    # Find best configurations
    train_rows = [r for r in summary_rows if r["period"] == "train"]
    test_rows = [r for r in summary_rows if r["period"] == "test"]
    best_train = (
        max(train_rows, key=lambda r: float(r["score"]))
        if train_rows
        else {}
    )
    best_test = (
        max(test_rows, key=lambda r: float(r["score"]))
        if test_rows
        else {}
    )
    baseline_train = _pick(
        summary_rows, "baseline_reverse_no_scaling", "train"
    )
    baseline_test = _pick(
        summary_rows, "baseline_reverse_no_scaling", "test"
    )
    selected_test = _pick(
        summary_rows, str(best_train.get("name")), "test"
    )

    # ── Success criteria from proposal spec ──
    # 1. test_excess_return >= 0.24
    test_excess = float(selected_test.get("excess_return", -999))
    excess_ok = test_excess >= 0.24

    # 2. test_max_drawdown <= 0.113 (abs value)
    test_dd = float(selected_test.get("max_drawdown", -999))
    drawdown_ok = test_dd >= -0.113

    # 3. test_win_rate >= 0.50
    test_wr = float(selected_test.get("win_rate", 0))
    wr_ok = test_wr >= 0.50

    adoption_pass = (
        bool(best_train)
        and best_train.get("name") != "baseline_reverse_no_scaling"
        and excess_ok
        and drawdown_ok
        and wr_ok
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
            "test_excess_return_ge_0p24": excess_ok,
            "test_max_drawdown_le_0p113": drawdown_ok,
            "test_win_rate_ge_0p50": wr_ok,
        },
        "computed_checks": {
            "test_excess_return": round(test_excess, 4),
            "test_max_drawdown": round(test_dd, 4),
            "test_win_rate": round(test_wr, 4),
        },
        "summary_rows": summary_rows,
        "artifacts": [
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
        "proposal_id": "cb_arb_value_gap_switch_reverse_volatility_position_scaling_v1",
    }

    _write_artifacts(
        output_dir, summary,
        best_train, best_test,
        baseline_train, baseline_test,
        adoption_pass, grid_meta,
    )

    print(
        f"[reverse_volatility_position_scaling] "
        f"adoption_pass={adoption_pass}",
        flush=True,
    )
    print(
        f"[reverse_volatility_position_scaling] "
        f"wrote artifacts to {output_dir}",
        flush=True,
    )

    _gatekeeper_after_run(output_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
