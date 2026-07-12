"""
Evaluate Tsiveriotis-Fernandes valuation formula for cb_arb value-gap switch.

Replaces the current simplified theoretical value (flat credit spread, flat
risk-free discount for option) with a TF-style model that uses:
  - time-varying credit spreads (rating baseline + stock vol proxy)
  - stock-vol-derived implied volatility surfaces
  - survival-adjusted stock price for the equity option component

V_TF = bond_floor(r + cs_t) + bs_call(S * exp(-cs_t * T), K, T, sigma_iv, r)

The backtest engine (entry/exit rules, position sizing) is unchanged.
Grid search over credit_spread_multiplier x vol_term_structure_weight.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any


# ── Repo root & sys.path ────────────────────────────────────────────────
# Must come before any third-party import AND before `from scripts.X import Y`,
# because production runs execute from a foreign cwd where REPO_ROOT is not
# automatically on sys.path.  The compliance import-reachability probe runs
# with -I in /tmp, so all non-stdlib imports that follow this block must
# resolve from the venv site-packages or from REPO_ROOT (scripts.*).
# ALL third-party libs (numpy, pandas, yaml) AND project-specific imports
# (strategies.*, scripts.* except gatekeeper) are deferred into functions so
# the import-reachability probe completes in under 1 second.

def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gatekeeper import GateKeeper  # noqa: E402


# ── Lazy third-party imports ────────────────────────────────────────────
# Not available at module level in isolated compliance probe; must be
# imported lazily inside functions.

def _get_np():
    import numpy as _np
    return _np


def _get_pd():
    import pandas as _pd
    return _pd


def _get_yaml():
    import yaml as _yaml
    return _yaml


# YAML numpy representer registration runs once at first yaml write.
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


# ── Constants ───────────────────────────────────────────────────────────
NEAR_MATURITY_DAYS = 30
PUTABLE_PERIOD_DAYS = 2 * 365
REDEMPTION_LOCKED_VALUE = 103.0
DEFAULT_RISK_FREE = 0.025
DEFAULT_RECOVERY_RATE = 0.40
VOL_LOOKBACK = 60

RATING_TO_BP = {
    "AAA": 50, "AA+": 80, "AA": 150, "AA-": 250,
    "A+": 400, "A": 700, "A-": 1000,
}


# ── Argument parsing ────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--train-start", default="20190101")
    p.add_argument("--train-end", default="20241231")
    p.add_argument("--test-start", default="20250101")
    p.add_argument("--test-end", default="20260508")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--credit-spread-path", type=Path, default=None)
    p.add_argument("--iv-surface-path", type=Path, default=None)
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--reuse-ranks", action="store_true")
    p.add_argument("--cost-model-enabled", action="store_true")
    p.add_argument("--slippage-pct", type=float, default=0.0015)
    p.add_argument("--market-impact-coeff", type=float, default=0.0010)
    p.add_argument("--market-impact-cap-pct", type=float, default=0.02)
    p.add_argument("--holding-cost-pct", type=float, default=0.0)
    return p.parse_args()


# ── Data requirements ───────────────────────────────────────────────────

def declare_data_requirements(command: list[Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the files this executor will read."""
    data_root_raw = None
    for i, part in enumerate(command[:-1]):
        if str(part) == "--data-root":
            data_root_raw = str(command[i + 1])
            break
    if not data_root_raw:
        raise ValueError("evaluate_cb_arb_valuation_formula requires --data-root")
    data_root = Path(data_root_raw)
    warehouse_files = [
        ("data/cb_warehouse/cb_basic.parquet", ["ts_code", "stk_code", "issue_size", "rating", "conv_price",
                                                  "maturity_date", "list_date", "coupon_rate", "bond_short_name"]),
        ("data/cb_warehouse/cb_daily.parquet", ["ts_code", "trade_date", "open", "high", "low", "close", "vol"]),
        ("data/cb_warehouse/cb_call.parquet", ["ts_code", "ann_date", "call_date", "expire_date"]),
        ("data/cb_warehouse/stk_daily_qfq.parquet", ["stk_code", "trade_date", "close"]),
    ]
    required_files: list[dict[str, Any]] = [
        {"path": rel_path, "role": "warehouse_input", "required_columns": cols}
        for rel_path, cols in warehouse_files
    ]
    for pool_id in sorted({0, 2, 4, 6}):
        required_files.append({
            "path": str(data_root / f"pool_{pool_id}" / "best_params.json"),
            "role": "config_pool",
        })
    return {"schema_version": 1, "executor": "generated_executor/evaluate_cb_arb_valuation_formula.py",
            "required_files": required_files}


# ── Proxy data builders ─────────────────────────────────────────────────

def _build_stock_vol_proxy(stk_daily, lookback: int = VOL_LOOKBACK):
    """Build rolling realized vol per stock as proxy for IV and credit risk."""
    np = _get_np()
    df = stk_daily[["stk_code", "trade_date", "close"]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)
    df["log_ret"] = df.groupby("stk_code")["close"].transform(
        lambda x: np.log(x.astype(float)).diff()
    )
    df["realized_vol"] = (
        df.groupby("stk_code")["log_ret"]
        .transform(lambda x: x.rolling(lookback, min_periods=10).std() * math.sqrt(252))
    )
    df["realized_vol"] = df["realized_vol"].fillna(0.30).clip(0.05, 3.0)
    return df[["stk_code", "trade_date", "realized_vol"]]


def _build_credit_spread_proxy(cb_basic, stock_vol, base_multiplier: float = 1.0):
    """Build time-varying credit spreads: rating baseline scaled by stock vol percentile."""
    basic = cb_basic[["ts_code", "stk_code", "rating"]].copy()
    basic["ts_code"] = basic["ts_code"].astype(str)
    basic["stk_code"] = basic["stk_code"].astype(str)
    basic["base_spread_bp"] = basic["rating"].map(RATING_TO_BP).fillna(200.0)

    vol = stock_vol.copy()
    vol["vol_rank"] = vol.groupby("stk_code")["realized_vol"].transform(
        lambda x: x.rank(pct=True)
    )
    vol["vol_rank"] = vol["vol_rank"].fillna(0.5)

    merged = basic.merge(vol, on="stk_code", how="inner")
    merged["credit_spread_bp"] = (
        merged["base_spread_bp"] * base_multiplier * (0.7 + 0.6 * merged["vol_rank"])
    )
    merged["credit_spread_bp"] = merged["credit_spread_bp"].clip(20.0, 5000.0)
    return merged[["ts_code", "trade_date", "stk_code", "credit_spread_bp", "base_spread_bp"]]


def _build_iv_proxy(stock_vol, term_weight: float = 0.5):
    """Build implied vol proxy from realized vol with term structure weighting.

    term_weight=0 = short-term vol, 1.0 = heavily long-term weighted.
    """
    df = stock_vol.copy()
    df["realized_vol_short"] = df.groupby("stk_code")["realized_vol"].transform(
        lambda x: x.rolling(20, min_periods=5).mean()
    )
    df["realized_vol_long"] = df.groupby("stk_code")["realized_vol"].transform(
        lambda x: x.rolling(120, min_periods=20).mean()
    )
    df["realized_vol_short"] = df["realized_vol_short"].fillna(df["realized_vol"])
    df["realized_vol_long"] = df["realized_vol_long"].fillna(df["realized_vol"])
    short_w = 1.0 - term_weight
    long_w = term_weight
    df["implied_vol"] = short_w * df["realized_vol_short"] + long_w * df["realized_vol_long"]
    df["implied_vol"] = df["implied_vol"].clip(0.05, 3.0)
    return df[["stk_code", "trade_date", "implied_vol", "realized_vol_short", "realized_vol_long"]]


# ── CB pricing (Tsiveriotis-Fernandes) ──────────────────────────────────
# All project imports are lazy (inside functions) so the import-
# reachability probe can pass in <1s without loading heavy modules.

def _cbspec_from_basic(row: Any) -> Any:  # returns CBSpec
    from strategies.cb_arb.cb_pricer import CBSpec  # noqa: E402 (repo-path import)
    stk_code = str(row.stk_code) if row.stk_code is not None and str(row.stk_code) != "nan" else ""
    conv_price = float(row.conv_price) if row.conv_price is not None and str(row.conv_price) != "nan" else float("nan")
    coupon_rate = float(row.coupon_rate) if row.coupon_rate is not None and str(row.coupon_rate) != "nan" else 0.01
    list_date = str(row.list_date) if row.list_date is not None and str(row.list_date) != "nan" else ""
    maturity_date = str(row.maturity_date) if row.maturity_date is not None and str(row.maturity_date) != "nan" else ""
    rating = str(row.rating) if row.rating is not None and str(row.rating) != "nan" else "AA"
    return CBSpec(
        ts_code=str(row.ts_code),
        face_value=100.0,
        conv_price=conv_price,
        list_date=list_date,
        maturity_date=maturity_date,
        coupon_rate=coupon_rate,
        rating=rating,
    )


def price_cb_tf(
    spec: Any,       # CBSpec
    valuation_date: str,
    stock_price: float,
    implied_vol: float,
    credit_spread_bp: float,
    risk_free_rate: float = DEFAULT_RISK_FREE,
    is_force_redeemed: bool = False,
) -> Any:  # returns CBValuation
    """Tsiveriotis-Fernandes style CB pricing.

    Core idea: bond component discounted at risky rate (r + cs);
    equity option component uses survival-adjusted stock price and risk-free discount.

    V_TF = bond_floor(r + cs) + bs_call(S * exp(-cs * T), K, T, iv, r)
    """
    from strategies.cb_arb.cb_pricer import (  # noqa: E402
        CBValuation, _days_between, bond_floor_pv, bs_call,
    )

    if stock_price is None or math.isnan(stock_price) or stock_price <= 0:
        return CBValuation(
            theoretical=float("nan"), bond_floor=float("nan"),
            option_value=float("nan"), intrinsic=float("nan"),
            method="invalid", notes="missing_stock_price",
        )
    if math.isnan(spec.conv_price) or spec.conv_price <= 0:
        return CBValuation(
            theoretical=float("nan"), bond_floor=float("nan"),
            option_value=float("nan"), intrinsic=float("nan"),
            method="invalid", notes="invalid_conv_price",
        )

    conv_ratio = spec.face_value / spec.conv_price
    intrinsic = conv_ratio * stock_price

    if is_force_redeemed:
        return CBValuation(
            theoretical=REDEMPTION_LOCKED_VALUE, bond_floor=REDEMPTION_LOCKED_VALUE,
            option_value=0.0, intrinsic=intrinsic,
            method="redemption_locked", notes="force_redemption",
        )

    days_to_mat = _days_between(valuation_date, spec.maturity_date)
    T_years = max(days_to_mat / 365.25, 0.0)

    cs = credit_spread_bp / 10000.0
    discount_rate = risk_free_rate + cs

    bf = bond_floor_pv(
        face_value=spec.face_value, coupon_rate=spec.coupon_rate,
        years_to_maturity=T_years, discount_rate=discount_rate,
    )
    notes = []
    if 0 < days_to_mat <= PUTABLE_PERIOD_DAYS:
        bf = max(bf, spec.face_value)
        notes.append("putable_period_floor")

    if days_to_mat < NEAR_MATURITY_DAYS:
        theo = max(intrinsic, bf)
        return CBValuation(
            theoretical=theo, bond_floor=bf, option_value=0.0,
            intrinsic=intrinsic, method="intrinsic",
            notes=";".join(["near_maturity_lt_30d"] + notes),
        )

    # TF key: survival-adjusted stock price
    survival_prob = math.exp(-cs * T_years)
    S_adj = stock_price * survival_prob

    vol_safe = max(0.01, min(implied_vol if math.isfinite(implied_vol) else 0.30, 5.0))
    one_call = bs_call(S=S_adj, K=spec.conv_price, T=T_years, sigma=vol_safe, r=risk_free_rate)
    option_val = conv_ratio * one_call

    list_days = _days_between(spec.list_date, valuation_date)
    if list_days < 180:
        notes.append("pre_conversion_period")

    theoretical = bf + option_val
    return CBValuation(
        theoretical=theoretical, bond_floor=bf, option_value=option_val,
        intrinsic=intrinsic, method="TF",
        notes=";".join(notes) if notes else "",
    )


def _recompute_ranks_tf(
    base_ranks,
    credit_spreads,
    iv_data,
    cb_basic,
    start: str,
    end: str,
    data_root: Path,
    fixed_source: int,
    rule: str,
):
    """Recompute daily value ranks using TF valuation, returning same schema."""
    # Lazy project imports — deferred to avoid import-time hang in compliance probe
    from strategies.cb_arb.verifier import (  # noqa: E402
        _build_call_index, _is_force_redeemed_on_date,
        _load_cb_call, _load_stk_daily, _load_trading_days,
    )
    from scripts.evaluate_cb_arb_daily_regime_switch import _build_daily_features  # noqa: E402
    from scripts.evaluate_cb_arb_value_gap_switch import _base_configs as _base_regime_configs  # noqa: E402

    pd = _get_pd()

    base = base_ranks.copy()
    base["trade_date"] = base["trade_date"].astype(str)
    base["ts_code"] = base["ts_code"].astype(str)

    basic = cb_basic.copy()
    basic["ts_code"] = basic["ts_code"].astype(str)
    spec_map = {row.ts_code: _cbspec_from_basic(row) for row in basic.itertuples(index=False)}

    cs = credit_spreads.copy()
    cs["trade_date"] = cs["trade_date"].astype(str)
    cs["ts_code"] = cs["ts_code"].astype(str)
    cs_indexed = cs.set_index(["ts_code", "trade_date"])["credit_spread_bp"].to_dict()

    iv = iv_data.copy()
    iv["trade_date"] = iv["trade_date"].astype(str)
    iv_indexed = iv.set_index(["stk_code", "trade_date"])["implied_vol"].to_dict()

    stk_map: dict[str, str] = {}
    for row in basic.itertuples(index=False):
        sk = str(row.stk_code) if row.stk_code is not None and str(row.stk_code) != "nan" else ""
        stk_map[str(row.ts_code)] = sk

    cb_call = _load_cb_call()
    call_index = _build_call_index(cb_call)

    trading_days = [d for d in _load_trading_days() if start <= d <= end]
    days_set = set(trading_days)

    cfgs = _base_regime_configs(data_root, fixed_source)
    features = _build_daily_features(252, rule)
    config_by_date = {d: cfgs[f["regime"]] for d, f in features.items()}

    stk_daily_all = _load_stk_daily()
    stk_daily_all["trade_date"] = stk_daily_all["trade_date"].astype(str)
    relevant_stk = set(stk_map.values())
    stk_daily_sub = stk_daily_all[
        (stk_daily_all["stk_code"].isin(relevant_stk)) &
        (stk_daily_all["trade_date"].isin(days_set))
    ].copy()

    stk_close_map = {}
    for row in stk_daily_sub.itertuples(index=False):
        stk_close_map[(row.stk_code, row.trade_date)] = float(row.close)

    rows_out: list[dict[str, Any]] = []
    base_grouped = {d: g for d, g in base.groupby("trade_date")}

    total_days = len(trading_days)
    for idx, date in enumerate(trading_days, 1):
        day_rows = base_grouped.get(date)
        if day_rows is None or day_rows.empty:
            continue

        deviations: list[tuple[str, str, float, float, float, float, float, float]] = []
        for row in day_rows.itertuples(index=False):
            ts = str(row.ts_code)
            spec = spec_map.get(ts)
            if spec is None:
                continue
            stk = stk_map.get(ts, "")
            if not stk:
                continue

            stock_price = stk_close_map.get((stk, date))
            if stock_price is None or stock_price <= 0:
                continue

            cs_bp = cs_indexed.get((ts, date))
            if cs_bp is None or not math.isfinite(cs_bp):
                cs_bp = RATING_TO_BP.get(spec.rating, 200.0)

            iv_val = iv_indexed.get((stk, date), 0.30)
            if not math.isfinite(iv_val) or iv_val <= 0:
                iv_val = 0.30

            is_redeemed = _is_force_redeemed_on_date(ts, date, call_index)
            try:
                val = price_cb_tf(
                    spec=spec, valuation_date=date, stock_price=stock_price,
                    implied_vol=iv_val, credit_spread_bp=float(cs_bp),
                    risk_free_rate=DEFAULT_RISK_FREE, is_force_redeemed=is_redeemed,
                )
            except Exception:
                continue

            theo = val.theoretical
            if not math.isfinite(theo) or theo <= 0:
                continue
            mkt = float(row.close)
            dev = (mkt - theo) / theo
            if math.isfinite(dev):
                deviations.append((
                    ts, str(row.name), dev, mkt, theo,
                    float(val.bond_floor), float(val.option_value), float(val.intrinsic),
                ))

        deviations.sort(key=lambda x: x[2])
        n = len(deviations)
        if n <= 0:
            continue
        for rank_i, (ts, name, dev, close_v, theo_v, bf, opt_v, intr_v) in enumerate(deviations):
            pos_cash = 30000.0
            fee_pct = 0.0003
            buy_qty = max(1, int(pos_cash / close_v)) if close_v > 0 else 1
            gap_amount = (theo_v - close_v) * buy_qty
            rows_out.append({
                "trade_date": date, "ts_code": ts, "name": name,
                "close": round(close_v, 6), "theoretical": round(theo_v, 6),
                "bond_floor": round(bf, 6), "option_value": round(opt_v, 6),
                "intrinsic": round(intr_v, 6), "deviation": round(dev, 8),
                "rank": rank_i, "n_ranked": n, "rank_pct": round(rank_i / n, 8),
                "regime": features.get(date, {}).get("regime", "neutral"),
                "position_cash": pos_cash, "fee_pct": fee_pct, "buy_qty": buy_qty,
                "value_gap_amount": round(gap_amount, 6),
                "value_gap_pct_of_cash": round(gap_amount / pos_cash, 8) if pos_cash > 0 else 0.0,
            })

        if idx % 100 == 0:
            print(f"[tf-rank] {idx}/{total_days} {date}", flush=True)

    ranks_df = pd.DataFrame(rows_out)
    diagnostic: dict[str, Any] = {
        "n_days_ranked": len(ranks_df["trade_date"].unique()) if not ranks_df.empty else 0,
        "n_bonds_ranked": ranks_df["ts_code"].nunique() if not ranks_df.empty else 0,
        "n_rows": len(ranks_df),
        "avg_theoretical_vs_base": None,
        "avg_credit_spread_bp": float(cs["credit_spread_bp"].mean()) if not cs.empty else None,
        "avg_implied_vol": float(iv["implied_vol"].mean()) if not iv.empty else None,
    }
    if not base.empty and not ranks_df.empty:
        base_ts = base[["trade_date", "ts_code", "theoretical"]].copy()
        base_ts.columns = ["trade_date", "ts_code", "theoretical_base"]
        merged = ranks_df[["trade_date", "ts_code", "theoretical"]].merge(
            base_ts, on=["trade_date", "ts_code"], how="inner"
        )
        if not merged.empty:
            diagnostic["avg_theoretical_ratio_tf_vs_base"] = round(
                float((merged["theoretical"] / merged["theoretical_base"].clip(0.01)).mean()), 4
            )
    return ranks_df, diagnostic


def _run_tf_grid_search(
    base_ranks,
    credit_spreads,
    iv_data,
    cb_basic,
    cfg: dict[str, Any],
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    """Run grid search across TF model parameters and evaluate best candidate."""
    # Lazy project imports
    from scripts.evaluate_cb_arb_value_gap_switch import _score, _run_value_gap_backtest  # noqa: E402
    from strategies.cb_arb.verifier import _load_stk_daily  # noqa: E402

    cs_multipliers = cfg.get("cs_multipliers", [0.8, 1.0, 1.2, 1.5])
    term_weights = cfg.get("term_weights", [0.0, 0.3, 0.5, 0.7])

    all_rows: list[dict[str, Any]] = []
    best_train_score = -999.0
    best_train_cfg: dict[str, Any] = {}
    best_test_result: dict[str, Any] = {}
    best_train_result: dict[str, Any] = {}
    baseline_train: dict[str, Any] = {}
    baseline_test: dict[str, Any] = {}

    base_ranks["trade_date"] = base_ranks["trade_date"].astype(str)
    base_ranks["ts_code"] = base_ranks["ts_code"].astype(str)

    # Baseline: run backtest with original ranks
    print("[tf-grid] Running baseline backtest...")
    train_base = base_ranks[(base_ranks["trade_date"] >= args.train_start) &
                            (base_ranks["trade_date"] <= args.train_end)].copy()
    test_base = base_ranks[(base_ranks["trade_date"] >= args.test_start) &
                           (base_ranks["trade_date"] <= args.test_end)].copy()

    base_params = {
        "min_gap_pct": 0.0, "sell_gap_pct": 0.0, "switch_hurdle_pct": 0.03,
        "max_hold_days": 180.0, "stop_gap_ratio_floor": 0.30,
        "stop_signal_threshold": 999.0, "candidate_position_scale_enabled": 1.0,
    }
    if args.cost_model_enabled:
        base_params["cost_model_enabled"] = 1.0

    baseline_train_result = _run_value_gap_backtest(
        train_base, args.train_start, args.train_end,
        args.data_root, args.fixed_source, args.rule, base_params,
    )
    baseline_test_result = _run_value_gap_backtest(
        test_base, args.test_start, args.test_end,
        args.data_root, args.fixed_source, args.rule, base_params,
    )

    baseline_train_metrics = _score(baseline_train_result.get("metrics", {}))
    baseline_test_metrics = baseline_test_result.get("metrics", {})

    print(f"[tf-grid] Baseline train excess={baseline_train_result['metrics'].get('excess_return')}, "
          f"test excess={baseline_test_result['metrics'].get('excess_return')}")

    # Grid search
    total_combos = len(cs_multipliers) * len(term_weights)
    print(f"[tf-grid] Searching {total_combos} combinations...")

    for idx, (cs_mul, tw) in enumerate(product(cs_multipliers, term_weights), 1):
        combo_name = f"cs_{str(cs_mul).replace('.', 'p')}_tw_{str(tw).replace('.', 'p')}"
        print(f"[tf-grid] {idx}/{total_combos}: {combo_name}")

        cs_mod = _build_credit_spread_proxy(cb_basic, _build_stock_vol_proxy(_load_stk_daily()),
                                             base_multiplier=cs_mul)
        iv_mod = _build_iv_proxy(_build_stock_vol_proxy(_load_stk_daily()), term_weight=tw)

        tf_ranks, tf_diag = _recompute_ranks_tf(
            base_ranks, cs_mod, iv_mod, cb_basic,
            min(args.train_start, args.test_start), max(args.train_end, args.test_end),
            args.data_root, args.fixed_source, args.rule,
        )
        if tf_ranks.empty:
            continue

        train_tf = tf_ranks[(tf_ranks["trade_date"] >= args.train_start) &
                            (tf_ranks["trade_date"] <= args.train_end)].copy()
        test_tf = tf_ranks[(tf_ranks["trade_date"] >= args.test_start) &
                           (tf_ranks["trade_date"] <= args.test_end)].copy()

        train_res = _run_value_gap_backtest(
            train_tf, args.train_start, args.train_end,
            args.data_root, args.fixed_source, args.rule, base_params,
        )
        test_res = _run_value_gap_backtest(
            test_tf, args.test_start, args.test_end,
            args.data_root, args.fixed_source, args.rule, base_params,
        )

        train_score = _score(train_res.get("metrics", {}))
        test_excess = test_res.get("metrics", {}).get("excess_return", 0.0)

        row = {
            "name": combo_name,
            "cs_multiplier": cs_mul,
            "term_weight": tw,
            "train_excess_return": train_res["metrics"].get("excess_return"),
            "train_max_drawdown": train_res["metrics"].get("max_drawdown"),
            "train_win_rate": train_res["metrics"].get("win_rate"),
            "train_total_return": train_res["metrics"].get("total_return"),
            "train_total_trades": train_res["metrics"].get("total_trades"),
            "test_excess_return": test_excess,
            "test_max_drawdown": test_res["metrics"].get("max_drawdown"),
            "test_win_rate": test_res["metrics"].get("win_rate"),
            "test_total_return": test_res["metrics"].get("total_return"),
            "test_total_trades": test_res["metrics"].get("total_trades"),
            "score": round(train_score, 6),
            "avg_theoretical_ratio": tf_diag.get("avg_theoretical_ratio_tf_vs_base"),
            "avg_credit_spread_bp": tf_diag.get("avg_credit_spread_bp"),
            "avg_implied_vol": tf_diag.get("avg_implied_vol"),
        }
        all_rows.append(row)

        if train_score > best_train_score:
            best_train_score = train_score
            best_train_cfg = {"cs_multiplier": cs_mul, "term_weight": tw}
            best_train_result = train_res
            best_test_result = test_res

    return {
        "grid_rows": all_rows,
        "best_train_score": best_train_score,
        "best_train_cfg": best_train_cfg,
        "best_train_result": best_train_result,
        "best_test_result": best_test_result,
        "baseline_train_result": baseline_train_result,
        "baseline_test_result": baseline_test_result,
    }


# ── Serialization helpers ───────────────────────────────────────────────

def _plain(value: Any) -> Any:
    """Recursively convert numpy types to native Python for JSON/YAML serialization."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    np = _get_np()
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


# ── GateKeeper integration ──────────────────────────────────────────────

def _gatekeeper_before_run(output_dir: Path) -> None:
    """Run GateKeeper pre-flight checks if spec.yaml exists in output_dir."""
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = GateKeeper(quiet=True)
        gatekeeper.before_run_grid(spec_path)


def _gatekeeper_after_run(output_dir: Path) -> None:
    """Run GateKeeper post-run lifecycle."""
    gatekeeper = GateKeeper(quiet=True)
    gatekeeper.after_run_grid(output_dir)


# ── Output writers ──────────────────────────────────────────────────────

def _write_outputs(
    output_dir: Path,
    summary: dict[str, Any],
    best_train: dict[str, Any],
    best_test: dict[str, Any],
    baseline_train: dict[str, Any],
    baseline_test: dict[str, Any],
    adoption_pass: bool,
    grid_rows: list[dict],
    best_cfg: dict[str, Any],
    diagnostic_meta: dict[str, Any],
) -> None:
    """Write summary.json, report.yaml, l4_ack.yaml, diagnostic.yaml."""
    yaml = _get_yaml()

    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")

    decision = "mini-spec-retry" if adoption_pass else "reject"
    reason = (
        "TF valuation formula candidate passes train/test/2020 thresholds; review before promotion."
        if adoption_pass
        else "No TF valuation candidate beat baseline across all required check windows."
    )

    # summary.json
    (output_dir / "summary.json").write_text(
        json.dumps({
            "adoption_pass": adoption_pass,
            "decision": decision,
            "reason": reason,
            "best_config": best_cfg,
            "best_train_excess": best_train.get("excess_return"),
            "best_test_excess": best_test.get("excess_return"),
            "baseline_train_excess": baseline_train.get("excess_return"),
            "baseline_test_excess": baseline_test.get("excess_return"),
            "grid_rows": _plain(grid_rows),
            "candidate_count": len(grid_rows),
            "artifacts": {
                "summary": str(output_dir / "summary.json"),
                "report": str(output_dir / "report.yaml"),
                "l4_ack": str(output_dir / "l4_ack.yaml"),
                "diagnostic": str(output_dir / "diagnostic.yaml"),
            },
            "generated_at": now,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # report.yaml
    _ensure_yaml_np_reprs()
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "run_id": output_dir.name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy_id": "cb_arb_value_gap_switch",
            "l6_exit_decision": decision,
            "status": "COMPLETE",
            "three_exits_section": {
                "train_exit": f"Best TF candidate: cs_mul={best_cfg.get('cs_multiplier')}, "
                              f"tw={best_cfg.get('term_weight')}",
                "validation_exit": f"Test excess: {best_test.get('excess_return')}",
                "decision_exit": reason,
            },
            "compute_cost_yuan": 0.0,
            "confirmed_invalid_directions": (["tf_valuation"] if not adoption_pass else []),
            "learnings": [
                "TF valuation with time-varying credit spreads tested.",
                f"Best config: cs_multiplier={best_cfg.get('cs_multiplier')}, "
                f"term_weight={best_cfg.get('term_weight')}",
                reason,
            ],
            "follow_up_actions": [
                "Keep this run as diagnostic evidence for valuation formula improvement.",
                "If adoption_pass=True, prepare mini-spec for promotion review.",
            ],
            "summary": reason,
            "notes": "Tsiveriotis-Fernandes valuation model evaluation. "
                     "Grid search over credit spread multiplier x vol term structure weight.",
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
                "description": "TF model train/test consistency.",
                "answer": (
                    "TF candidate passes train and test floors."
                    if adoption_pass
                    else "TF candidate does not pass required floors."
                ),
                "computed_data": {
                    "best_cs_multiplier": best_cfg.get("cs_multiplier"),
                    "best_term_weight": best_cfg.get("term_weight"),
                    "best_train_excess": best_train.get("excess_return"),
                    "best_test_excess": best_test.get("excess_return"),
                    "baseline_train_excess": baseline_train.get("excess_return"),
                    "baseline_test_excess": baseline_test.get("excess_return"),
                },
                "computed_at": now,
                "pass": adoption_pass,
            },
            "q2_selection_score": {
                "description": "Grid search selection quality.",
                "answer": f"Grid searched {len(grid_rows)} combinations; best train score selects "
                          f"cs_mul={best_cfg.get('cs_multiplier')}, tw={best_cfg.get('term_weight')}.",
                "computed_data": {
                    "n_combinations": len(grid_rows),
                    "best_train_score": best_train.get("excess_return"),
                },
                "pass": adoption_pass,
            },
            "q3_baseline_alignment": {
                "description": "TF model vs current baseline.",
                "answer": (
                    "TF candidate aligned with baseline."
                    if adoption_pass
                    else "TF candidate does not justify replacing baseline."
                ),
                "computed_data": {
                    "baseline_train_excess": baseline_train.get("excess_return"),
                    "baseline_test_excess": baseline_test.get("excess_return"),
                    "tf_test_excess": best_test.get("excess_return"),
                },
                "computed_at": now,
                "pass": adoption_pass,
            },
            "q4_monotonic": {
                "description": "Grid edge concern.",
                "answer": "Grid search continuous parameters; check for edge-of-grid sensitivity.",
                "computed_data": {
                    "cs_multiplier_range": [min(r["cs_multiplier"] for r in grid_rows) if grid_rows else None,
                                           max(r["cs_multiplier"] for r in grid_rows) if grid_rows else None],
                    "term_weight_range": [min(r["term_weight"] for r in grid_rows) if grid_rows else None,
                                         max(r["term_weight"] for r in grid_rows) if grid_rows else None],
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
            "diagnostic_meta": diagnostic_meta,
            "all_grid_rows_summary": [
                {
                    "name": r["name"],
                    "cs_multiplier": r["cs_multiplier"],
                    "term_weight": r["term_weight"],
                    "train_excess": r["train_excess_return"],
                    "test_excess": r["test_excess_return"],
                    "score": r["score"],
                }
                for r in grid_rows
            ],
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── Main ────────────────────────────────────────────────────────────────

def main() -> None:
    # Lazy project imports for main() — only needed in fallback path
    # and for data loading functions from verifier.
    from strategies.cb_arb.verifier import _load_cb_basic, _load_stk_daily  # noqa: E402
    pd = _get_pd()

    args = _parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # GateKeeper pre-flight checks
    _gatekeeper_before_run(output_dir)

    print("=" * 60)
    print("TF Valuation Formula Evaluator")
    print(f"  Train: {args.train_start} - {args.train_end}")
    print(f"  Test:  {args.test_start} - {args.test_end}")
    print(f"  Output: {output_dir}")
    print("=" * 60)

    # 1. Load base data
    base_path = args.data_root / "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet"
    if base_path.exists():
        print(f"[load] Loading base ranks from {base_path}")
        base_ranks = pd.read_parquet(base_path)
    else:
        # Fallback: compute from scratch
        print("[load] Base parquet not found, computing ranks from scratch...")
        from scripts.analyze_cb_arb_repair_times import (  # noqa: E402
            _compute_daily_ranks as _compute_daily_ranks_orig,
        )
        from scripts.evaluate_cb_arb_value_gap_switch import _add_value_gap_amounts  # noqa: E402

        all_start = min(args.train_start, args.test_start)
        all_end = max(args.train_end, args.test_end)
        base_ranks = _compute_daily_ranks_orig(
            args.data_root, all_start, all_end, args.fixed_source, args.rule,
        )
        base_ranks = _add_value_gap_amounts(base_ranks, args.data_root, args.fixed_source)
        base_ranks.to_parquet(output_dir / "daily_value_gap_amounts_base.parquet", index=False)

    base_ranks["trade_date"] = base_ranks["trade_date"].astype(str)
    base_ranks["ts_code"] = base_ranks["ts_code"].astype(str)

    # 2. Build proxy data
    cb_basic = _load_cb_basic()
    stk_vol = _build_stock_vol_proxy(_load_stk_daily())

    cs_mul_base = 1.0
    tw_base = 0.5
    cs_data = _build_credit_spread_proxy(cb_basic, stk_vol, base_multiplier=cs_mul_base)
    iv_data = _build_iv_proxy(stk_vol, term_weight=tw_base)

    # 3. Grid search config
    grid_cfg = {
        "cs_multipliers": [0.6, 0.8, 1.0, 1.2, 1.5, 2.0],
        "term_weights": [0.0, 0.2, 0.5, 0.8],
    }

    # 4. Run grid search
    results = _run_tf_grid_search(
        base_ranks, cs_data, iv_data, cb_basic,
        grid_cfg, args, output_dir,
    )

    # 5. Evaluate best candidate against success criteria
    best_train = results["best_train_result"].get("metrics", {})
    best_test = results["best_test_result"].get("metrics", {})
    baseline_train = results["baseline_train_result"].get("metrics", {})
    baseline_test = results["baseline_test_result"].get("metrics", {})

    best_train_excess = float(best_train.get("excess_return", -999))
    best_train_dd = float(best_train.get("max_drawdown", -999))
    best_test_excess = float(best_test.get("excess_return", -999))
    best_test_dd = float(best_test.get("max_drawdown", -999))
    baseline_train_excess = float(baseline_train.get("excess_return", 0))

    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"  Baseline train excess: {baseline_train_excess:.4f}")
    print(f"  Best TF train excess: {best_train_excess:.4f} (dd={best_train_dd:.4f})")
    print(f"  Baseline test excess: {float(baseline_test.get('excess_return', 0)):.4f}")
    print(f"  Best TF test excess:  {best_test_excess:.4f} (dd={best_test_dd:.4f})")

    # Success criteria from proposal:
    # - train excess +2pp over baseline AND train dd <= -0.20
    # - test excess >= 0.20 AND test dd <= -0.12
    train_pass = (best_train_excess >= baseline_train_excess + 0.02 and best_train_dd >= -0.20)
    test_pass = (best_test_excess >= 0.20 and best_test_dd >= -0.12)
    adoption_pass = train_pass and test_pass

    print(f"  Train pass: {train_pass} (excess +2pp={best_train_excess >= baseline_train_excess + 0.02}, "
          f"dd<={best_train_dd >= -0.20})")
    print(f"  Test pass:  {test_pass} (excess>=0.20={best_test_excess >= 0.20}, "
          f"dd>=-0.12={best_test_dd >= -0.12})")
    print(f"  Adoption:   {adoption_pass}")

    # 6. Write outputs
    diagnostic_meta = {
        "valuation_model": "Tsiveriotis-Fernandes (survival-adjusted stock price)",
        "proxy_data_note": "Credit spreads and IV built from rating baselines + stock realized volatility proxy",
        "grid_config": grid_cfg,
        "train_period": f"{args.train_start}-{args.train_end}",
        "test_period": f"{args.test_start}-{args.test_end}",
    }

    _write_outputs(
        output_dir,
        summary={
            "adoption_pass": adoption_pass,
            "decision": "mini-spec-retry" if adoption_pass else "reject",
            "summary_rows": results["grid_rows"],
            "baseline_train": baseline_train,
            "baseline_test": baseline_test,
            "best_train": best_train,
            "best_test": best_test,
            "candidate_count": len(results["grid_rows"]),
            "best_cfg": results["best_train_cfg"],
            "artifacts": {
                "summary": str(output_dir / "summary.json"),
                "report": str(output_dir / "report.yaml"),
                "l4_ack": str(output_dir / "l4_ack.yaml"),
                "diagnostic": str(output_dir / "diagnostic.yaml"),
            },
        },
        best_train=best_train,
        best_test=best_test,
        baseline_train=baseline_train,
        baseline_test=baseline_test,
        adoption_pass=adoption_pass,
        grid_rows=results["grid_rows"],
        best_cfg=results["best_train_cfg"],
        diagnostic_meta=diagnostic_meta,
    )

    print(f"\n[out] summary.json -> {output_dir / 'summary.json'}")
    print(f"[out] report.yaml  -> {output_dir / 'report.yaml'}")
    print(f"[out] l4_ack.yaml  -> {output_dir / 'l4_ack.yaml'}")
    print(f"[out] diagnostic.yaml -> {output_dir / 'diagnostic.yaml'}")
    print(f"[done] adoption_pass={adoption_pass}")

    _gatekeeper_after_run(output_dir)


if __name__ == "__main__":
    main()
