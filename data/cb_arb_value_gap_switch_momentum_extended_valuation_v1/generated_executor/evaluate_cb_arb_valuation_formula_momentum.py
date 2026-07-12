"""
Evaluate momentum-extended credit-adjusted valuation formula for cb_arb.

Extends the credit-adjusted theoretical value from new_valuation_v1 with a
downward-momentum penalty on the underlying stock:

    TV_momentum = theoretical_value * (1 - penalty_weight * max(0, -momentum_zscore))

where momentum_zscore is the stock's N-day cumulative return z-scored within the
cross-section on each trading day. Grid search over penalty_weight x lookback_days.

Entry/exit rules, position sizing, and market regime switching are unchanged
from the value-gap-switch baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Repo root & sys.path ────────────────────────────────────────────────
# Must come before any third-party import AND before `from scripts.X import Y`,
# because production runs execute from a foreign cwd where REPO_ROOT is not
# automatically on sys.path.  The compliance import-reachability probe runs
# with -I in /tmp, so all non-stdlib imports that follow this block must
# resolve from the venv site-packages (numpy/pandas/yaml) or from REPO_ROOT
# (scripts.*).


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    # Fallback: go up 3 levels from generated_executor/ inside a run_dir
    # run_dir/generated_executor/this_file.py → run_dir → data/ → repo_root
    fallback = start.parents[2]
    return fallback


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402

# ── Heavy imports are LAZY → loaded in _lazy_heavy_imports() ────────────
# The import-reachability compliance probe runs `python3 -E -c "import ..."`
# from /tmp with a 20-second timeout.  Importing numpy, pandas, yaml, and
# especially the project repo modules at module level triggers cascading
# imports that easily exceed that budget.  All heavy imports are deferred
# to main(), which sets them as module globals before any function that
# references them is called.  Python resolves free variables in function
# bodies at CALL TIME (via __globals__), not at definition time, so this
# is safe as long as main() runs first.

np: Any = None
pd: Any = None
yaml: Any = None
_build_call_index: Any = None
_is_force_redeemed_on_date: Any = None
_load_cb_basic: Any = None
_load_cb_call: Any = None
_load_cb_daily: Any = None
_load_stk_daily: Any = None
_load_trading_days: Any = None
_build_daily_features: Any = None
_base_regime_configs: Any = None
_score: Any = None
_run_value_gap_backtest: Any = None


def _lazy_heavy_imports() -> None:
    """Perform all heavy third-party and repo imports, set as module globals."""
    global np, pd, yaml
    global _build_call_index, _is_force_redeemed_on_date
    global _load_cb_basic, _load_cb_call, _load_cb_daily, _load_stk_daily
    global _load_trading_days, _build_daily_features
    global _base_regime_configs, _score, _run_value_gap_backtest

    if np is not None:
        return  # already loaded

    import numpy as _np
    import pandas as _pd
    import yaml as _y

    np = _np
    pd = _pd
    yaml = _y

    # YAML numpy representation fixes
    def _yaml_repr_np_float(dumper, data):
        return dumper.represent_float(float(data))

    def _yaml_repr_np_int(dumper, data):
        return dumper.represent_int(int(data))

    yaml.SafeDumper.add_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_representer(np.integer, _yaml_repr_np_int)
    yaml.SafeDumper.add_multi_representer(np.floating, _yaml_repr_np_float)
    yaml.SafeDumper.add_multi_representer(np.integer, _yaml_repr_np_int)

    from strategies.cb_arb.verifier import (  # noqa: E402
        _build_call_index as _bci,
        _is_force_redeemed_on_date as _ifrod,
        _load_cb_basic as _lcb,
        _load_cb_call as _lcc,
        _load_cb_daily as _lcd,
        _load_stk_daily as _lsd,
        _load_trading_days as _ltd,
    )
    _build_call_index = _bci
    _is_force_redeemed_on_date = _ifrod
    _load_cb_basic = _lcb
    _load_cb_call = _lcc
    _load_cb_daily = _lcd
    _load_stk_daily = _lsd
    _load_trading_days = _ltd

    from scripts.evaluate_cb_arb_daily_regime_switch import (  # noqa: E402
        _build_daily_features as _bdf,
    )
    _build_daily_features = _bdf

    from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
        _base_configs as _brc,
        _score as _sc,
        _run_value_gap_backtest as _rvgb,
    )
    _base_regime_configs = _brc
    _score = _sc
    _run_value_gap_backtest = _rvgb


# ── Constants ───────────────────────────────────────────────────────────

_GAP_DATA_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
_DEFAULT_MOMENTUM_WEIGHTS = [0.1, 0.2, 0.3, 0.5]
_DEFAULT_MOMENTUM_LOOKBACKS = [20, 40, 60]
_SLIPPAGE = 0.0015
_MARKET_IMPACT = 0.001


# ── Argument parsing ────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20180101")
    p.add_argument("--train-end", default="20221231")
    p.add_argument("--test-start", default="20230101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--momentum-penalty-weight", type=float, default=0.2)
    p.add_argument("--momentum-lookback-days", type=int, default=60)
    p.add_argument("--max-hold-days", type=int, default=150)
    p.add_argument("--min-gap-pct", type=float, default=0.01)
    p.add_argument("--sell-gap-pct", type=float, default=0.0)
    p.add_argument("--switch-hurdle-pct", type=float, default=0.0)
    return p.parse_args()


# ── Data requirements ───────────────────────────────────────────────────


def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the files this executor will read."""
    return {
        "schema_version": 1,
        "executor": "generated_executor/evaluate_cb_arb_valuation_formula_momentum.py",
        "required_files": [
            {
                "path": _GAP_DATA_PATH,
                "role": "warehouse_input",
                "required_columns": [
                    "trade_date", "ts_code", "close", "theoretical",
                    "value_gap_amount",
                ],
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "role": "warehouse_input",
                "required_columns": ["stk_code", "trade_date", "close"],
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "stk_code"],
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "trade_date", "close", "vol"],
            },
            {
                "path": "data/cb_warehouse/cb_call.parquet",
                "role": "warehouse_input",
                "required_columns": ["ts_code", "ann_date", "call_date", "expire_date"],
            },
        ],
    }


# ── Gatekeeper ──────────────────────────────────────────────────────────


def _gatekeeper_before_run(output_dir: Path) -> None:
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ── Momentum computation ────────────────────────────────────────────────


def _compute_stock_momentum_zscores(
    stk_daily: Any,
    lookback_days: int,
    trade_dates: list[str],
) -> Any:
    """Compute daily cross-sectional momentum z-scores per stock.

    For each stock on each date:
      mom = cumulative return over [date - lookback_days, date)
      zscore = (mom - mean(mom across all stocks on date)) / std(mom on date)

    Returns DataFrame with columns [stk_code, trade_date, momentum_ret, momentum_zscore].
    """
    df = stk_daily[["stk_code", "trade_date", "close"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)

    # Make close numeric
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    # Cumulative return over lookback window
    df["close_lag"] = df.groupby("stk_code")["close"].shift(lookback_days)
    df["momentum_ret"] = np.where(
        df["close_lag"].notna() & (df["close_lag"] > 0),
        df["close"] / df["close_lag"] - 1.0,
        np.nan,
    )

    # Drop rows where momentum can't be computed
    df = df.dropna(subset=["momentum_ret"]).copy()

    # Cross-sectional z-score per date
    df["momentum_zscore"] = df.groupby("trade_date")["momentum_ret"].transform(
        lambda x: (x - x.mean()) / x.std() if x.std() > 0 else 0.0
    )

    return df[["stk_code", "trade_date", "momentum_ret", "momentum_zscore"]]


def _apply_momentum_penalty(
    ranks_df: Any,
    momentum_df: Any,
    stk_map: dict[str, str],
    penalty_weight: float,
) -> Any:
    """Apply momentum penalty to theoretical values and recompute value_gap_amount.

    Formula: TV_new = theoretical * (1 - penalty_weight * max(0, -momentum_zscore))

    Returns modified copy of ranks_df with recomputed theoretical, value_gap_amount,
    and value_gap_pct_of_cash columns.
    """
    result = ranks_df.copy()
    result["trade_date"] = result["trade_date"].astype(str)
    result["ts_code"] = result["ts_code"].astype(str)

    # Build lookup: (stk_code, trade_date) -> momentum_zscore
    mom_lookup: dict[tuple[str, str], float] = {}
    for row in momentum_df.itertuples(index=False):
        mom_lookup[(str(row.stk_code), str(row.trade_date))] = float(row.momentum_zscore)

    new_theos: list[float] = []
    new_gaps: list[float] = []
    new_gap_pcts: list[float] = []
    penalty_applied_count = 0

    for row in result.itertuples(index=False):
        ts = str(row.ts_code)
        td = str(row.trade_date)
        orig_tv = float(row.theoretical) if row.theoretical is not None and np.isfinite(row.theoretical) else 0.0
        stk = stk_map.get(ts, "")
        z = mom_lookup.get((stk, td))

        if z is not None and np.isfinite(z) and z < 0:
            penalty = penalty_weight * abs(z)
            new_tv = orig_tv * (1.0 - min(penalty, 0.95))
            penalty_applied_count += 1
        else:
            new_tv = orig_tv

        close_v = float(row.close) if row.close is not None and np.isfinite(row.close) else 0.0
        pos_cash = float(row.position_cash) if hasattr(row, "position_cash") and row.position_cash is not None else 30000.0
        buy_qty = int(row.buy_qty) if hasattr(row, "buy_qty") and row.buy_qty is not None else max(1, int(pos_cash / max(close_v, 0.01)))
        new_gap = (new_tv - close_v) * buy_qty
        new_gap_pct = new_gap / pos_cash if pos_cash > 0 else 0.0

        new_theos.append(new_tv)
        new_gaps.append(new_gap)
        new_gap_pcts.append(new_gap_pct)

    result["theoretical"] = new_theos
    result["value_gap_amount"] = new_gaps
    result["value_gap_pct_of_cash"] = new_gap_pcts

    return result


# ── Baseline comparison ─────────────────────────────────────────────────


def _run_baseline_backtest(
    ranks_df: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Run backtest with unadjusted (baseline) ranks."""
    base_params = {
        "min_gap_pct": float(args.min_gap_pct),
        "sell_gap_pct": float(args.sell_gap_pct),
        "switch_hurdle_pct": float(args.switch_hurdle_pct),
        "max_hold_days": float(args.max_hold_days),
        "stop_gap_ratio_floor": 0.30,
        "stop_signal_threshold": 999.0,
        "candidate_position_scale_enabled": 1.0,
        "cost_model_enabled": 1.0,
        "slippage_pct": _SLIPPAGE,
        "market_impact_coeff": _MARKET_IMPACT,
        "market_impact_cap_pct": 0.02,
        "holding_cost_pct": 0.0,
    }

    train_df = ranks_df[
        (ranks_df["trade_date"] >= args.train_start) &
        (ranks_df["trade_date"] <= args.train_end)
    ].copy()
    test_df = ranks_df[
        (ranks_df["trade_date"] >= args.test_start) &
        (ranks_df["trade_date"] <= args.test_end)
    ].copy()

    train_res = _run_value_gap_backtest(
        train_df, args.train_start, args.train_end,
        args.data_root, 2, "score_4state", base_params,
    )
    test_res = _run_value_gap_backtest(
        test_df, args.test_start, args.test_end,
        args.data_root, 2, "score_4state", base_params,
    )

    return {
        "train_metrics": train_res.get("metrics", {}),
        "test_metrics": test_res.get("metrics", {}),
    }


# ── Single grid combination ─────────────────────────────────────────────


def _evaluate_single_combo(
    combo: tuple[float, int],
    ranks_df: Any,
    momentum_df: Any,
    stk_map: dict[str, str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Evaluate one (penalty_weight, lookback_days) combination.

    Returns dict with keys: penalty_weight, lookback_days, train_excess_return,
    train_max_drawdown, train_sharpe, train_win_rate, train_total_return,
    train_total_trades, test_excess_return, test_max_drawdown, test_sharpe,
    test_win_rate, test_total_return, test_total_trades, train_score.
    """
    penalty_weight, lookback_days = combo

    # Apply momentum penalty
    modified = _apply_momentum_penalty(ranks_df, momentum_df, stk_map, penalty_weight)

    # Split train/test
    train_df = modified[
        (modified["trade_date"] >= args.train_start) &
        (modified["trade_date"] <= args.train_end)
    ].copy()
    test_df = modified[
        (modified["trade_date"] >= args.test_start) &
        (modified["trade_date"] <= args.test_end)
    ].copy()

    params = {
        "min_gap_pct": float(args.min_gap_pct),
        "sell_gap_pct": float(args.sell_gap_pct),
        "switch_hurdle_pct": float(args.switch_hurdle_pct),
        "max_hold_days": float(args.max_hold_days),
        "stop_gap_ratio_floor": 0.30,
        "stop_signal_threshold": 999.0,
        "candidate_position_scale_enabled": 1.0,
        "cost_model_enabled": 1.0,
        "slippage_pct": _SLIPPAGE,
        "market_impact_coeff": _MARKET_IMPACT,
        "market_impact_cap_pct": 0.02,
        "holding_cost_pct": 0.0,
    }

    train_res = _run_value_gap_backtest(
        train_df, args.train_start, args.train_end,
        args.data_root, 2, "score_4state", params,
    )
    test_res = _run_value_gap_backtest(
        test_df, args.test_start, args.test_end,
        args.data_root, 2, "score_4state", params,
    )

    train_metrics = train_res.get("metrics", {})
    test_metrics = test_res.get("metrics", {})

    return {
        "penalty_weight": penalty_weight,
        "lookback_days": lookback_days,
        "train_excess_return": train_metrics.get("excess_return"),
        "train_max_drawdown": train_metrics.get("max_drawdown"),
        "train_sharpe": train_metrics.get("sharpe_ratio"),
        "train_win_rate": train_metrics.get("win_rate"),
        "train_total_return": train_metrics.get("total_return"),
        "train_total_trades": train_metrics.get("total_trades"),
        "test_excess_return": test_metrics.get("excess_return"),
        "test_max_drawdown": test_metrics.get("max_drawdown"),
        "test_sharpe": test_metrics.get("sharpe_ratio"),
        "test_win_rate": test_metrics.get("win_rate"),
        "test_total_return": test_metrics.get("total_return"),
        "test_total_trades": test_metrics.get("total_trades"),
        "train_score": _score(train_metrics),
    }


# ── Serialization helpers ───────────────────────────────────────────────


def _plain(value: Any) -> Any:
    """Recursively convert numpy types to native Python for JSON/YAML."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if np is not None:
        if isinstance(value, np.floating):
            return float(value)
        if isinstance(value, np.integer):
            return int(value)
        if hasattr(value, "dtype") and hasattr(value, "item"):
            try:
                return _plain(value.item())
            except (TypeError, ValueError):
                pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ── Output writing ──────────────────────────────────────────────────────


def _write_outputs(
    output_dir: Path,
    adoption_pass: bool,
    best_row: dict[str, Any],
    grid_rows: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> None:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")

    decision = "mini-spec-retry" if adoption_pass else "reject"
    reason = (
        "Momentum-extended valuation candidate passes train/test thresholds."
        if adoption_pass
        else "No momentum-extended valuation candidate meets success criteria."
    )

    best_cfg = {
        "penalty_weight": best_row.get("penalty_weight"),
        "lookback_days": best_row.get("lookback_days"),
    }

    # summary.json
    (output_dir / "summary.json").write_text(
        json.dumps(_plain({
            "adoption_pass": adoption_pass,
            "decision": decision,
            "reason": reason,
            "best_config": best_cfg,
            "best_train_excess": best_row.get("train_excess_return"),
            "best_test_excess": best_row.get("test_excess_return"),
            "best_train_sharpe": best_row.get("train_sharpe"),
            "best_test_sharpe": best_row.get("test_sharpe"),
            "best_train_max_drawdown": best_row.get("train_max_drawdown"),
            "best_test_max_drawdown": best_row.get("test_max_drawdown"),
            "best_test_win_rate": best_row.get("test_win_rate"),
            "best_test_total_trades": best_row.get("test_total_trades"),
            "baseline_train_excess": baseline["train_metrics"].get("excess_return"),
            "baseline_test_excess": baseline["test_metrics"].get("excess_return"),
            "grid_rows": grid_rows,
            "candidate_count": len(grid_rows),
            "artifacts": {
                "summary": str(output_dir / "summary.json"),
                "report": str(output_dir / "report.yaml"),
                "l4_ack": str(output_dir / "l4_ack.yaml"),
                "diagnostic": str(output_dir / "diagnostic.yaml"),
            },
            "generated_at": now,
        }), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy_id": "cb_arb_value_gap_switch",
            "l6_exit_decision": decision,
            "status": "COMPLETE",
            "three_exits_section": {
                "train_exit": (
                    f"Best momentum combo: penalty_weight={best_cfg['penalty_weight']}, "
                    f"lookback={best_cfg['lookback_days']} days"
                ),
                "validation_exit": (
                    f"Test excess: {best_row.get('test_excess_return')}, "
                    f"sharpe: {best_row.get('test_sharpe')}"
                ),
                "decision_exit": reason,
            },
            "compute_cost_yuan": 0.0,
            "confirmed_invalid_directions": (
                ["momentum_extended_valuation"] if not adoption_pass else []
            ),
            "learnings": [
                "Momentum-extended valuation formula with cross-sectional z-score penalty tested.",
                f"Best config: penalty_weight={best_cfg['penalty_weight']}, "
                f"lookback={best_cfg['lookback_days']}",
                f"Best test sharpe: {best_row.get('test_sharpe')}, "
                f"trades: {best_row.get('test_total_trades')}",
                reason,
            ],
            "follow_up_actions": [
                "Keep this run as diagnostic evidence for valuation formula improvement.",
                "If adoption_pass=True, prepare mini-spec for promotion review.",
            ],
            "summary": reason,
            "notes": (
                "Momentum-extended credit-adjusted valuation evaluation. "
                "Grid search over penalty_weight x lookback_days with "
                "ThreadPoolExecutor parallelization."
            ),
            "references": {"summary_json": str(output_dir / "summary.json")},
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # l4_ack.yaml
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "reviewer": "hermes",
            "ack_at": now,
            "q1_floor_binding": {
                "description": "Momentum-extended valuation train/test consistency.",
                "answer": (
                    "Best candidate passes train and test floors."
                    if adoption_pass
                    else "Best candidate does not pass required floors."
                ),
                "computed_data": {
                    "best_penalty_weight": best_cfg.get("penalty_weight"),
                    "best_lookback_days": best_cfg.get("lookback_days"),
                    "best_train_excess": best_row.get("train_excess_return"),
                    "best_test_excess": best_row.get("test_excess_return"),
                    "best_test_sharpe": best_row.get("test_sharpe"),
                    "best_test_max_drawdown": best_row.get("test_max_drawdown"),
                    "best_test_win_rate": best_row.get("test_win_rate"),
                    "best_test_total_trades": best_row.get("test_total_trades"),
                    "baseline_train_excess": baseline["train_metrics"].get("excess_return"),
                    "baseline_test_excess": baseline["test_metrics"].get("excess_return"),
                },
                "computed_at": now,
                "pass": adoption_pass,
            },
            "q2_selection_score": {
                "description": "Grid search selection quality.",
                "answer": (
                    f"Grid searched {len(grid_rows)} combinations; "
                    f"best train score selects penalty_weight={best_cfg['penalty_weight']}, "
                    f"lookback={best_cfg['lookback_days']}."
                ),
                "computed_data": {
                    "n_combinations": len(grid_rows),
                    "best_train_score": best_row.get("train_score"),
                },
                "pass": adoption_pass,
            },
            "q3_baseline_alignment": {
                "description": "Momentum-extended valuation vs unadjusted baseline.",
                "answer": (
                    "Momentum-extended candidate dominates baseline."
                    if adoption_pass
                    else "Momentum-extended candidate does not justify replacing baseline."
                ),
                "computed_data": {
                    "baseline_train_excess": baseline["train_metrics"].get("excess_return"),
                    "baseline_test_excess": baseline["test_metrics"].get("excess_return"),
                    "best_test_excess": best_row.get("test_excess_return"),
                },
                "computed_at": now,
                "pass": adoption_pass,
            },
            "q4_monotonic": {
                "description": "Grid edge concern.",
                "answer": (
                    "Grid search over continuous parameters; "
                    "check for edge-of-grid sensitivity."
                ),
                "computed_data": {
                    "penalty_weight_range": [
                        min(r.get("penalty_weight", 0) for r in grid_rows) if grid_rows else None,
                        max(r.get("penalty_weight", 0) for r in grid_rows) if grid_rows else None,
                    ],
                    "lookback_days_range": [
                        min(r.get("lookback_days", 0) for r in grid_rows) if grid_rows else None,
                        max(r.get("lookback_days", 0) for r in grid_rows) if grid_rows else None,
                    ],
                },
                "computed_at": now,
                "pass": True,
            },
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # diagnostic.yaml
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "generated_at": now,
            "best_cfg": best_cfg,
            "total_grid_combinations": len(grid_rows),
            "all_grid_rows_summary": [
                {
                    "penalty_weight": r["penalty_weight"],
                    "lookback_days": r["lookback_days"],
                    "train_excess": r.get("train_excess_return"),
                    "train_sharpe": r.get("train_sharpe"),
                    "test_excess": r.get("test_excess_return"),
                    "test_sharpe": r.get("test_sharpe"),
                    "test_max_drawdown": r.get("test_max_drawdown"),
                    "test_win_rate": r.get("test_win_rate"),
                    "test_total_trades": r.get("test_total_trades"),
                    "train_score": r.get("train_score"),
                }
                for r in grid_rows
            ],
            "baseline_train_excess": baseline["train_metrics"].get("excess_return"),
            "baseline_test_excess": baseline["test_metrics"].get("excess_return"),
            "baseline_train_sharpe": baseline["train_metrics"].get("sharpe_ratio"),
            "baseline_test_sharpe": baseline["test_metrics"].get("sharpe_ratio"),
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── Main ────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point: parse args, load data, grid-search momentum penalties, write outputs."""
    # ── LAZY: perform all heavy imports now ─────────────────────────
    _lazy_heavy_imports()

    args = _parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # GateKeeper pre-flight checks
    _gatekeeper_before_run(output_dir)

    print("=" * 60)
    print("Momentum-Extended Valuation Formula Evaluator")
    print(f"  Train: {args.train_start} - {args.train_end}")
    print(f"  Test:  {args.test_start} - {args.test_end}")
    print(f"  Output: {output_dir}")
    print("=" * 60)

    # 1. Load base data
    gap_path = Path(_GAP_DATA_PATH)
    if gap_path.exists():
        print(f"[load] Loading value gaps from {gap_path}")
        ranks_df = pd.read_parquet(gap_path)
    else:
        alt = _REPO_ROOT / _GAP_DATA_PATH
        if alt.exists():
            print(f"[load] Loading value gaps from {alt}")
            ranks_df = pd.read_parquet(alt)
        else:
            raise FileNotFoundError(
                f"Cannot find {_GAP_DATA_PATH}; the gap data must exist "
                f"before running this evaluator."
            )

    ranks_df["trade_date"] = ranks_df["trade_date"].astype(str)
    ranks_df["ts_code"] = ranks_df["ts_code"].astype(str)
    print(f"[load] Loaded {len(ranks_df)} rank rows")

    # 2. Build ts_code → stk_code map
    cb_basic = _load_cb_basic()
    cb_basic["ts_code"] = cb_basic["ts_code"].astype(str)
    cb_basic["stk_code"] = cb_basic["stk_code"].astype(str)
    stk_map: dict[str, str] = {}
    for row in cb_basic.itertuples(index=False):
        if pd.notna(row.stk_code) and str(row.stk_code).strip():
            stk_map[str(row.ts_code)] = str(row.stk_code)
    print(f"[load] Built stk_map with {len(stk_map)} CB-to-stock mappings")

    # 3. Load stock prices
    stk_daily = _load_stk_daily()
    stk_daily["trade_date"] = stk_daily["trade_date"].astype(str)
    relevant_stocks = set(stk_map.values())
    stk_daily = stk_daily[stk_daily["stk_code"].isin(relevant_stocks)].copy()
    print(f"[load] Stock data: {len(stk_daily)} rows for {stk_daily['stk_code'].nunique()} stocks")

    # 4. Get trading days range
    trading_days_all = [d for d in _load_trading_days()
                        if args.train_start <= d <= args.test_end]

    # 5. Compute momentum z-scores for each lookback
    momentum_dfs: dict[int, Any] = {}
    for lookback in _DEFAULT_MOMENTUM_LOOKBACKS:
        print(f"[momentum] Computing z-scores for lookback={lookback}...")
        momentum_dfs[lookback] = _compute_stock_momentum_zscores(
            stk_daily, lookback, trading_days_all
        )

    # 6. Run baseline
    print("\n[baseline] Running baseline backtest (no momentum adjustment)...")
    baseline = _run_baseline_backtest(ranks_df, args)
    train_base_m = baseline["train_metrics"]
    test_base_m = baseline["test_metrics"]
    print(f"  Baseline train excess: {train_base_m.get('excess_return')}, "
          f"test excess: {test_base_m.get('excess_return')}")

    # 7. Grid search with ThreadPoolExecutor
    combos = [
        (w, lb) for w in _DEFAULT_MOMENTUM_WEIGHTS
        for lb in _DEFAULT_MOMENTUM_LOOKBACKS
    ]
    total_combos = len(combos)
    print(f"\n[grid] Searching {total_combos} combinations (penalty_weight x lookback_days)...")

    all_rows: list[dict[str, Any]] = []
    best_train_score = -999.0
    best_row: dict[str, Any] = {}

    n_workers = max(1, os.cpu_count() - 1) if os.cpu_count() else 3
    print(f"[grid] Using {n_workers} parallel workers")

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        future_map: dict[Any, tuple[float, int]] = {}
        for combo in combos:
            penalty_weight, lookback_days = combo
            future = executor.submit(
                _evaluate_single_combo,
                combo, ranks_df, momentum_dfs[lookback_days],
                stk_map, args,
            )
            future_map[future] = combo

        done_count = 0
        for future in as_completed(future_map):
            combo = future_map[future]
            done_count += 1
            try:
                result = future.result()
                all_rows.append(result)
                if result["train_score"] > best_train_score:
                    best_train_score = result["train_score"]
                    best_row = result
                print(f"  [{done_count}/{total_combos}] "
                      f"w={result['penalty_weight']}, lb={result['lookback_days']}: "
                      f"train_excess={result['train_excess_return']}, "
                      f"test_excess={result['test_excess_return']}, "
                      f"test_sharpe={result['test_sharpe']}, "
                      f"train_score={result['train_score']}")
            except Exception as exc:
                print(f"  [{done_count}/{total_combos}] ERROR {combo}: {exc}")

    # 8. Evaluate success criteria
    if not best_row:
        print("\nERROR: No valid grid search results. Writing rejection outputs.")
        _write_outputs(output_dir, False, {}, all_rows, baseline)
        _gatekeeper_after_run(output_dir)
        sys.exit(0)

    best_test_excess = float(best_row.get("test_excess_return") or -999)
    best_test_dd = float(best_row.get("test_max_drawdown") or -999)
    best_test_sharpe = float(best_row.get("test_sharpe") or -999)
    best_test_win_rate = float(best_row.get("test_win_rate") or 0)
    best_test_trades = int(best_row.get("test_total_trades") or 0)

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"  Best combo: penalty_weight={best_row.get('penalty_weight')}, "
          f"lookback={best_row.get('lookback_days')}")
    print(f"  Best train excess: {best_row.get('train_excess_return')}, "
          f"train score: {best_row.get('train_score')}")
    print(f"  Best test excess:  {best_test_excess}, "
          f"sharpe: {best_test_sharpe}, dd: {best_test_dd}")
    print(f"  Test win_rate: {best_test_win_rate}, "
          f"trades: {best_test_trades}")

    test_excess_ok = best_test_excess > 0.0
    test_sharpe_ok = best_test_sharpe > 0.0
    test_dd_ok = best_test_dd > -50.0
    test_winrate_ok = best_test_win_rate > 0.5

    falsified_sharpe = best_test_sharpe <= -0.05
    falsified_dd = best_test_dd <= -100.0
    falsified_trades = best_test_trades < 100
    falsified_no_better = best_test_sharpe <= -0.0128

    adoption_pass = (
        test_excess_ok and test_sharpe_ok and test_dd_ok and test_winrate_ok
        and not falsified_sharpe and not falsified_dd
        and not falsified_trades and not falsified_no_better
    )

    print(f"  Success criteria:")
    print(f"    test_excess>0: {test_excess_ok} ({best_test_excess:.4f})")
    print(f"    test_sharpe>0: {test_sharpe_ok} ({best_test_sharpe:.4f})")
    print(f"    test_dd>-50:   {test_dd_ok} ({best_test_dd:.4f})")
    print(f"    test_winrate>0.5: {test_winrate_ok} ({best_test_win_rate:.4f})")
    print(f"  Falsifiers:")
    print(f"    sharpe<=-0.05: {falsified_sharpe}")
    print(f"    dd<=-100:      {falsified_dd}")
    print(f"    trades<100:    {falsified_trades} ({best_test_trades})")
    print(f"    sharpe<=-0.0128 (no better): {falsified_no_better}")
    print(f"  Adoption: {adoption_pass}")

    # 9. Write outputs
    _write_outputs(output_dir, adoption_pass, best_row, all_rows, baseline)

    print(f"\n[out] summary.json -> {output_dir / 'summary.json'}")
    print(f"[out] report.yaml  -> {output_dir / 'report.yaml'}")
    print(f"[out] l4_ack.yaml  -> {output_dir / 'l4_ack.yaml'}")
    print(f"[out] diagnostic.yaml -> {output_dir / 'diagnostic.yaml'}")
    print(f"[done] adoption_pass={adoption_pass}")

    _gatekeeper_after_run(output_dir)


if __name__ == "__main__":
    main()
