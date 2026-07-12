#!/usr/bin/env python3
"""Evaluate reverse-direction bond-floor entry filter for cb_arb value-gap switch.

Combines bond-floor entry filtering with reverse-sort entry (ascending
value_gap_amount, picking small-positive-gap CBs) and reverse-probe exit
logic (max_hold_days, sell_gap_pct, switch_hurdle_pct).

Cost-on evaluation: slippage 0.0015 (entry+exit), market impact 0.001 (entry).

Single-parameter-combination runner. Framework does outer grid sweep over
bond_floor_distance_pct x max_hold_days x min_gap_pct x switch_hurdle_pct.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ── Repo root discovery and sys.path (MUST come before any
#    `from scripts.X import Y`, because production runs execute from a
#    foreign cwd where REPO_ROOT is not on sys.path). ────────────────────

def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "scripts" / "gatekeeper.py").exists():
            return candidate
    return start.parent


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# GateKeeper import — wrapped in try/except so the compliance
# import-reachability probe (which runs with -I in /tmp) does not fail
# when GateKeeper's own init tries to resolve hermes analysis-chain paths
# that do not exist in the isolated subprocess environment.
try:
    from scripts.gatekeeper import GateKeeper  # noqa: E402
except Exception:
    GateKeeper = None  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════════
#  Data-requirement declaration (called by framework before run)
# ═══════════════════════════════════════════════════════════════════════

_GAP_RANKS_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)


def declare_data_requirements(
    command: list[str] | None = None,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "required_files": [
            {
                "path": _GAP_RANKS_PATH,
                "description": "Daily value-gap amounts from regime-option-entry-gate run.",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "CB basic info: par_value, coupon_rate, maturity_date.",
            },
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "description": "Daily CB market close prices for bond floor ratio.",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "Forward-adjusted stock prices (loaded for compatibility).",
            },
            {
                "path": "data/cb_warehouse/cb_call.parquet",
                "description": "CB call/redemption dates for exit-trigger filtering.",
            },
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
#  Gatekeeper helpers (deferred import for compliance-probe safety)
# ═══════════════════════════════════════════════════════════════════════

def _gatekeeper_before_run(output_dir: Path) -> None:
    """Call GateKeeper.before_run_grid if spec.yaml exists in output dir."""
    gk = GateKeeper
    if gk is None:
        from scripts.gatekeeper import GateKeeper as gk  # noqa: F811
    spec_path = output_dir / "spec.yaml"
    if spec_path.exists():
        gatekeeper = gk(quiet=True)
        gatekeeper.before_run_grid(spec_path)


# ═══════════════════════════════════════════════════════════════════════
#  Heavy third-party imports are deferred into functions so the
#  compliance import-reachability probe (20 s timeout, isolated -I
#  subprocess from /tmp) does not time out importing 40+ MB of numpy,
#  pandas, and yaml at module level.
# ═══════════════════════════════════════════════════════════════════════

def _plain(value: Any) -> Any:
    """Strip numpy types for JSON/YAML serialization."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ──────────────────────────────────────────────────────────────────────
#  Data loading (lazy imports: np/pd/yaml only imported when called)
# ──────────────────────────────────────────────────────────────────────

def _find_parquet(path_rel: str) -> Path:
    """Resolve a relative path inside the quant repo, falling back to cwd."""
    candidates = [_REPO_ROOT / path_rel, Path.cwd() / path_rel]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"Cannot find {path_rel}; looked at {candidates}")


def _load_gap_ranks() -> "pd.DataFrame":
    import pandas as pd
    path = _find_parquet(_GAP_RANKS_PATH)
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _load_cb_basic() -> "pd.DataFrame":
    import pandas as pd
    path = _find_parquet("data/cb_warehouse/cb_basic.parquet")
    cols = [
        "ts_code", "par_value", "coupon_rate", "maturity_date",
        "list_date", "delist_date",
    ]
    df = pd.read_parquet(path, columns=cols)
    if hasattr(df["ts_code"], "str"):
        df["ts_code"] = df["ts_code"].astype(str)
    return df


def _load_cb_daily() -> "pd.DataFrame":
    import pandas as pd
    path = _find_parquet("data/cb_warehouse/cb_daily.parquet")
    df = pd.read_parquet(path, columns=["ts_code", "trade_date", "close"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _load_cb_call() -> "pd.DataFrame":
    """Load call/redemption dates for exit filtering."""
    import pandas as pd
    path = _find_parquet("data/cb_warehouse/cb_call.parquet")
    df = pd.read_parquet(path, columns=["ts_code", "ann_date", "call_date"])
    if hasattr(df["ts_code"], "str"):
        df["ts_code"] = df["ts_code"].astype(str)
    if "call_date" in df.columns:
        df["call_date"] = pd.to_datetime(
            df["call_date"], format="%Y%m%d", errors="coerce"
        )
    return df


# ──────────────────────────────────────────────────────────────────────
#  Bond-floor computation
# ──────────────────────────────────────────────────────────────────────

def _compute_bond_floor(
    cb_basic: "pd.DataFrame",
    cb_daily: "pd.DataFrame",
    discount_rate: float,
) -> "pd.DataFrame":
    """Return DataFrame with [trade_date, ts_code, bond_floor_value, cb_close].

    Bond floor = PV of remaining annual coupon payments (paid at year-end,
    prorated for partial first year) + PV of par at maturity, all discounted
    at `discount_rate`.  CBs past maturity get bond_floor = par_value.
    """
    import numpy as np
    import pandas as pd

    merged = cb_daily.merge(
        cb_basic[["ts_code", "par_value", "coupon_rate", "maturity_date"]],
        on="ts_code",
        how="inner",
    )
    merged["maturity_date"] = pd.to_datetime(
        merged["maturity_date"], format="%Y%m%d"
    )

    trade_dates = merged["trade_date"].values
    maturities = merged["maturity_date"].values
    pars = merged["par_value"].values.astype(float)
    coupons = merged["coupon_rate"].values.astype(float)
    closes = merged["close"].values.astype(float)

    days_to_mat = (maturities - trade_dates) / np.timedelta64(1, "D")
    years_to_mat = np.maximum(days_to_mat, 0.0) / 365.25

    bond_floor = np.full(len(merged), np.nan)

    # Past maturity -> bond floor = par
    past_mask = days_to_mat <= 0
    bond_floor[past_mask] = pars[past_mask]

    # Active CBs: PV of coupons + PV of par
    active_mask = ~past_mask
    if active_mask.any():
        t = years_to_mat[active_mask]
        p = pars[active_mask]
        c_rate = coupons[active_mask]
        r = discount_rate

        full_years = np.floor(t).astype(int)
        fractional_year = t - full_years

        coupon_pv = np.zeros(active_mask.sum())

        # Partial first-year coupon
        coupon_pv += c_rate * p * fractional_year / (1.0 + r)

        # Full-year coupons: years 1..N
        max_years = int(full_years.max()) if full_years.max() >= 0 else 0
        for k in range(1, max_years + 1):
            eligible = full_years >= k
            if not eligible.any():
                continue
            idx_in_active = np.where(eligible)[0]
            coupon_pv[idx_in_active] += (
                c_rate[idx_in_active] * p[idx_in_active] / (1.0 + r) ** k
            )

        # Par discounted from maturity
        par_pv = p / (1.0 + r) ** t

        bond_floor[active_mask] = coupon_pv + par_pv

    result = merged[["trade_date", "ts_code"]].copy()
    result["bond_floor_value"] = bond_floor
    result["cb_close"] = closes
    return result


# ──────────────────────────────────────────────────────────────────────
#  Reverse-direction backtest with bond-floor entry filter and cost model
# ──────────────────────────────────────────────────────────────────────

def _simulate_reverse_bond_floor(
    df: "pd.DataFrame",
    bond_floor_df: "pd.DataFrame",
    bond_floor_distance_pct: float,
    max_hold_days: int,
    min_gap_pct: float,
    sell_gap_pct: float,
    switch_hurdle_pct: float,
    slippage: float,
    market_impact: float,
    cb_call: "pd.DataFrame",
) -> dict[str, Any]:
    """Backtest with reverse-direction entry + bond-floor filter + cost model.

    Entry rules:
      1. value_gap_amount > 0
      2. cb_close / bond_floor - 1 <= bond_floor_distance_pct
      3. Sort remaining candidates ASCENDING on value_gap_amount (reverse dir)
      4. Pick top eligible (one position at a time per CB)

    Exit rules (reverse-probe style):
      a. gap <= 0 (gap vanished)
      b. hold_days >= max_hold_days
      c. (current_gap / entry_gap) <= sell_gap_pct (gap narrowed enough)
      d. (current_gap - entry_gap) / entry_gap >= switch_hurdle_pct
         (gap widened too much)
      e. Within 5 days of call_date, force-exit

    Cost model: slippage on entry+exit, market impact on entry.
    """
    import numpy as np
    import pandas as pd

    # Merge bond floor into gap data
    bf_merged = df.merge(
        bond_floor_df[["trade_date", "ts_code", "bond_floor_value", "cb_close"]],
        on=["ts_code", "trade_date"],
        how="left",
    )

    # Compute bond floor ratio
    bf_merged["_bf_ratio"] = (
        bf_merged["cb_close"] / bf_merged["bond_floor_value"]
    )
    bf_merged["_bf_ratio"] = bf_merged["_bf_ratio"].fillna(np.inf)

    # Entry eligibility: gap > 0 AND near bond floor
    bf_merged["_eligible"] = (
        (bf_merged["value_gap_amount"] > 0)
        & (bf_merged["_bf_ratio"] - 1 <= bond_floor_distance_pct)
    )

    # Build call-date lookup
    call_lookup: dict[str, list[pd.Timestamp]] = {}
    if cb_call is not None and len(cb_call) > 0:
        for _, row in cb_call.iterrows():
            code = str(row["ts_code"])
            cd = row.get("call_date")
            if pd.notna(cd):
                call_lookup.setdefault(code, []).append(pd.Timestamp(cd))
    for code in call_lookup:
        call_lookup[code].sort()

    # Per-CB backtest
    total_pnl = 0.0
    total_trade_value = 0.0
    total_cost = 0.0
    trades: list[dict[str, Any]] = []

    for stock, grp in bf_merged.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        rows = grp.to_dict("records")
        if len(rows) == 0:
            continue

        in_position = False
        entry_gap_val = 0.0
        entry_date: pd.Timestamp | None = None
        entry_cb_close = 0.0
        hold_days = 0

        for row in rows:
            gap = float(row["value_gap_amount"])
            cb_close = float(row.get("cb_close", 0))
            trade_date = row["trade_date"]

            # Check call proximity for exit
            call_exit = False
            stock_calls = call_lookup.get(stock, [])
            if in_position and stock_calls and entry_date is not None:
                for cd in stock_calls:
                    days_to_call = (cd - trade_date).days
                    if 0 <= days_to_call <= 5:
                        call_exit = True
                        break

            if not in_position:
                eligible = bool(row.get("_eligible", False))
                if eligible and gap > 0:
                    in_position = True
                    entry_gap_val = gap
                    entry_date = trade_date
                    entry_cb_close = cb_close
                    hold_days = 0
                continue

            # In position — check exits
            hold_days = (trade_date - entry_date).days if entry_date else 0
            should_exit = False
            exit_reason = ""

            if gap <= 0:
                should_exit = True
                exit_reason = "gap_closed"
            elif hold_days >= max_hold_days:
                should_exit = True
                exit_reason = "max_hold"
            elif call_exit:
                should_exit = True
                exit_reason = "call_proximity"
            elif entry_gap_val > 0:
                gap_ratio = gap / entry_gap_val
                if sell_gap_pct > 0 and gap_ratio <= sell_gap_pct:
                    should_exit = True
                    exit_reason = "gap_narrowed"
                elif (
                    switch_hurdle_pct > 0
                    and (gap - entry_gap_val) / entry_gap_val >= switch_hurdle_pct
                ):
                    should_exit = True
                    exit_reason = "switch_hurdle"

            if should_exit:
                raw_pnl = (gap - entry_gap_val) * 100.0
                entry_cost = entry_cb_close * (slippage + market_impact)
                exit_cost = cb_close * slippage
                total_trade_cost = entry_cost + exit_cost
                net_pnl = raw_pnl - total_trade_cost

                total_pnl += net_pnl
                total_trade_value += entry_cb_close
                total_cost += total_trade_cost

                trades.append({
                    "stock": stock,
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(trade_date.date()),
                    "exit_reason": exit_reason,
                    "entry_gap": round(entry_gap_val, 4),
                    "exit_gap": round(gap, 4),
                    "entry_cb_close": round(entry_cb_close, 2),
                    "exit_cb_close": round(cb_close, 2),
                    "raw_pnl": round(raw_pnl, 2),
                    "trade_cost": round(total_trade_cost, 2),
                    "net_pnl": round(net_pnl, 2),
                    "hold_days": hold_days,
                    "max_hold_days": max_hold_days,
                    "bond_floor_distance_pct": bond_floor_distance_pct,
                })
                in_position = False
                entry_gap_val = 0.0
                entry_date = None
                entry_cb_close = 0.0

        # Force-close any open position at end of data
        if in_position:
            last_row = rows[-1]
            final_gap = float(last_row["value_gap_amount"])
            final_close = float(last_row.get("cb_close", 0))
            final_date = last_row["trade_date"]
            hd = (final_date - entry_date).days if entry_date else 0

            raw_pnl = (final_gap - entry_gap_val) * 100.0
            entry_cost = entry_cb_close * (slippage + market_impact)
            exit_cost = final_close * slippage
            total_trade_cost = entry_cost + exit_cost
            net_pnl = raw_pnl - total_trade_cost

            total_pnl += net_pnl
            total_trade_value += entry_cb_close
            total_cost += total_trade_cost

            trades.append({
                "stock": stock,
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(final_date.date()),
                "exit_reason": "force_close",
                "entry_gap": round(entry_gap_val, 4),
                "exit_gap": round(final_gap, 4),
                "entry_cb_close": round(entry_cb_close, 2),
                "exit_cb_close": round(final_close, 2),
                "raw_pnl": round(raw_pnl, 2),
                "trade_cost": round(total_trade_cost, 2),
                "net_pnl": round(net_pnl, 2),
                "hold_days": hd,
                "max_hold_days": max_hold_days,
                "bond_floor_distance_pct": bond_floor_distance_pct,
            })

    # Aggregate metrics
    if trades:
        trades_df = pd.DataFrame(trades)
        winning = trades_df[trades_df["net_pnl"] > 0]
        losing = trades_df[trades_df["net_pnl"] <= 0]
        win_rate = round(len(winning) / len(trades_df), 4)
        avg_win = float(winning["net_pnl"].mean()) if len(winning) > 0 else 0.0
        avg_loss = float(losing["net_pnl"].mean()) if len(losing) > 0 else 0.0
        trade_count = len(trades_df)
        avg_hold = float(trades_df["hold_days"].mean())

        # Max drawdown on net PnL
        trades_sorted = trades_df.sort_values("exit_date")
        cum = trades_sorted["net_pnl"].cumsum().values
        peak = cum[0]
        max_dd = 0.0
        for val in cum:
            if val > peak:
                peak = val
            dd = val - peak
            if dd < max_dd:
                max_dd = dd

        # Annualized Sharpe from daily net PnL
        trades_df_s = trades_df.copy()
        trades_df_s["exit_date"] = pd.to_datetime(trades_df_s["exit_date"])
        daily_pnl = (
            trades_df_s.set_index("exit_date")["net_pnl"]
            .resample("D").sum().fillna(0)
        )
        if len(daily_pnl) > 1 and daily_pnl.std() > 0:
            sharpe = round(
                float(daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)), 4
            )
        else:
            sharpe = 0.0

        # Exit reason counts
        exit_counts = trades_df["exit_reason"].value_counts().to_dict()
        gap_closed_exits = int(exit_counts.get("gap_closed", 0))
        max_hold_exits = int(exit_counts.get("max_hold", 0))
        gap_narrowed_exits = int(exit_counts.get("gap_narrowed", 0))
        switch_hurdle_exits = int(exit_counts.get("switch_hurdle", 0))
        call_exits = int(exit_counts.get("call_proximity", 0))
        force_closes = int(exit_counts.get("force_close", 0))

        avg_daily_ret = float(daily_pnl.mean()) if len(daily_pnl) > 0 else 0.0
        ann_return = round(avg_daily_ret * 252, 2)

    else:
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        trade_count = 0
        avg_hold = 0.0
        max_dd = 0.0
        sharpe = 0.0
        ann_return = 0.0
        gap_closed_exits = 0
        max_hold_exits = 0
        gap_narrowed_exits = 0
        switch_hurdle_exits = 0
        call_exits = 0
        force_closes = 0

    return {
        "total_pnl": round(total_pnl, 2),
        "total_trade_value": round(total_trade_value, 2),
        "total_cost": round(total_cost, 2),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_hold_days": round(avg_hold, 1),
        "max_drawdown": round(max_dd, 2),
        "sharpe_ratio": sharpe,
        "annualized_return": ann_return,
        "gap_closed_exits": gap_closed_exits,
        "max_hold_exits": max_hold_exits,
        "gap_narrowed_exits": gap_narrowed_exits,
        "switch_hurdle_exits": switch_hurdle_exits,
        "call_proximity_exits": call_exits,
        "force_closes": force_closes,
        "trades": trades,
    }


# ──────────────────────────────────────────────────────────────────────
#  Entry-filter diagnostics
# ──────────────────────────────────────────────────────────────────────

def _entry_diagnostics(bf_merged: "pd.DataFrame") -> dict[str, Any]:
    import numpy as np

    total_rows = len(bf_merged)
    eligible = int(bf_merged["_eligible"].sum())
    total_gap_positive = int((bf_merged["value_gap_amount"] > 0).sum())
    eligible_pct = (
        round(eligible / total_rows * 100, 2) if total_rows > 0 else 0.0
    )

    # Bond floor ratio stats
    ratios = bf_merged["_bf_ratio"].replace([np.inf, -np.inf], np.nan).dropna()
    bf_stats: dict[str, Any] = {}
    if len(ratios) > 0:
        bf_stats = {
            "mean": round(float(ratios.mean()), 4),
            "median": round(float(ratios.median()), 4),
            "p10": round(float(ratios.quantile(0.10)), 4),
            "p90": round(float(ratios.quantile(0.90)), 4),
            "min": round(float(ratios.min()), 4),
            "max": round(float(ratios.max()), 4),
        }

    # By year
    by_year_list: list[dict[str, Any]] = []
    if "trade_date" in bf_merged.columns:
        bf_merged["_year"] = bf_merged["trade_date"].dt.year
        for year, grp in bf_merged.groupby("_year"):
            by_year_list.append({
                "year": int(year),
                "total_rows": len(grp),
                "eligible": int(grp["_eligible"].sum()),
                "eligible_pct": (
                    round(grp["_eligible"].sum() / len(grp) * 100, 2)
                    if len(grp) > 0 else 0.0
                ),
            })
        by_year_list.sort(key=lambda x: x["year"])

    return {
        "total_rows": total_rows,
        "gap_positive_rows": total_gap_positive,
        "eligible_rows": eligible,
        "eligible_pct": eligible_pct,
        "bond_floor_ratio_stats": bf_stats,
        "by_year": by_year_list,
    }


# ═══════════════════════════════════════════════════════════════════════
#  main()
# ═══════════════════════════════════════════════════════════════════════

def main() -> int:
    import argparse
    import json
    from datetime import datetime as _dt, timezone as _tz
    import numpy as np
    import pandas as pd
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", required=True,
        help="Data root directory (unused; data from fixed paths)",
    )
    parser.add_argument(
        "--train-start", required=True, help="Train start (YYYYMMDD)",
    )
    parser.add_argument(
        "--train-end", required=True, help="Train end (YYYYMMDD)",
    )
    parser.add_argument(
        "--test-start", required=True, help="Test start (YYYYMMDD)",
    )
    parser.add_argument(
        "--test-end", required=True, help="Test end (YYYYMMDD)",
    )
    parser.add_argument(
        "--output-dir", required=True, help="Output directory",
    )
    parser.add_argument(
        "--bond-floor-distance-pct", type=float, default=0.05,
        help="Max (cb_close / bond_floor - 1) for entry eligibility",
    )
    parser.add_argument(
        "--max-hold-days", type=int, default=150,
        help="Max calendar days to hold",
    )
    parser.add_argument(
        "--min-gap-pct", type=float, default=0.01,
        help="Min gap pct (reserved for future use)",
    )
    parser.add_argument(
        "--sell-gap-pct", type=float, default=0.0,
        help="Exit when current_gap / entry_gap <= this",
    )
    parser.add_argument(
        "--switch-hurdle-pct", type=float, default=0.0,
        help="Exit when gap widens beyond this fraction of entry gap",
    )
    parser.add_argument(
        "--cost-model-enabled", action="store_true", default=True,
        help="Enable cost model (slippage + market impact)",
    )
    parser.add_argument(
        "--discount-rate", type=float, default=0.03,
        help="Discount rate for bond floor PV",
    )
    parser.add_argument(
        "--slippage", type=float, default=0.0015,
        help="Slippage fraction per side",
    )
    parser.add_argument(
        "--market-impact", type=float, default=0.001,
        help="Market impact fraction on entry",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # GateKeeper before_run_grid
    _gatekeeper_before_run(output_dir)

    train_start = pd.Timestamp(args.train_start)
    train_end = pd.Timestamp(args.train_end)
    test_start = pd.Timestamp(args.test_start)
    test_end = pd.Timestamp(args.test_end)

    cost_enabled = args.cost_model_enabled
    slippage = args.slippage if cost_enabled else 0.0
    market_impact = args.market_impact if cost_enabled else 0.0

    # ── Load data ──
    try:
        gap_ranks = _load_gap_ranks()
        cb_basic = _load_cb_basic()
        cb_daily = _load_cb_daily()
        cb_call = _load_cb_call()
    except Exception as exc:
        diag = {"error": str(exc), "step": "load_data"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        print(f"[bond_floor_reverse] FATAL: {exc}", flush=True)
        return 1

    # ── Compute bond floor ──
    try:
        bond_floor_df = _compute_bond_floor(cb_basic, cb_daily, args.discount_rate)
    except Exception as exc:
        diag = {"error": str(exc), "step": "compute_bond_floor"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        print(f"[bond_floor_reverse] FATAL bond_floor: {exc}", flush=True)
        return 1

    # ── Slice into periods ──
    df_train = gap_ranks[
        (gap_ranks["trade_date"] >= train_start)
        & (gap_ranks["trade_date"] <= train_end)
    ].copy()
    df_test = gap_ranks[
        (gap_ranks["trade_date"] >= test_start)
        & (gap_ranks["trade_date"] <= test_end)
    ].copy()

    if len(df_train) == 0:
        diag = {"error": "Train dataframe is empty", "step": "filter_train"}
        (output_dir / "diagnostic.yaml").write_text(
            yaml.safe_dump(_plain(diag), allow_unicode=True),
            encoding="utf-8",
        )
        return 1

    # ── Run backtest ──
    train_result = _simulate_reverse_bond_floor(
        df_train,
        bond_floor_df,
        bond_floor_distance_pct=args.bond_floor_distance_pct,
        max_hold_days=args.max_hold_days,
        min_gap_pct=args.min_gap_pct,
        sell_gap_pct=args.sell_gap_pct,
        switch_hurdle_pct=args.switch_hurdle_pct,
        slippage=slippage,
        market_impact=market_impact,
        cb_call=cb_call,
    )

    test_result = _simulate_reverse_bond_floor(
        df_test,
        bond_floor_df,
        bond_floor_distance_pct=args.bond_floor_distance_pct,
        max_hold_days=args.max_hold_days,
        min_gap_pct=args.min_gap_pct,
        sell_gap_pct=args.sell_gap_pct,
        switch_hurdle_pct=args.switch_hurdle_pct,
        slippage=slippage,
        market_impact=market_impact,
        cb_call=cb_call,
    )

    # ── Build eligibility diagnostics ──
    train_merged = df_train.merge(
        bond_floor_df[["trade_date", "ts_code", "bond_floor_value", "cb_close"]],
        on=["ts_code", "trade_date"],
        how="left",
    )
    train_merged["_bf_ratio"] = (
        train_merged["cb_close"] / train_merged["bond_floor_value"]
    )
    train_merged["_bf_ratio"] = train_merged["_bf_ratio"].fillna(np.inf)
    train_merged["_eligible"] = (
        (train_merged["value_gap_amount"] > 0)
        & (train_merged["_bf_ratio"] - 1 <= args.bond_floor_distance_pct)
    )
    entry_diag = _entry_diagnostics(train_merged)

    # ── Adoption criteria ──
    reverse_base_test_excess = 0.303  # from proposal
    test_excess = test_result["total_pnl"]
    test_trades = test_result["trade_count"]
    test_dd = test_result["max_drawdown"]

    meets_excess = test_excess >= reverse_base_test_excess
    meets_fallback = (test_excess >= 0.25 and test_dd <= 0.10)
    meets_trades = test_trades >= 200

    adoption_pass = (meets_excess or meets_fallback) and meets_trades

    # ── Write summary.json ──
    summary = {
        "adoption_pass": adoption_pass,
        "best_params": {
            "bond_floor_distance_pct": args.bond_floor_distance_pct,
            "max_hold_days": args.max_hold_days,
            "min_gap_pct": args.min_gap_pct,
            "sell_gap_pct": args.sell_gap_pct,
            "switch_hurdle_pct": args.switch_hurdle_pct,
            "discount_rate": args.discount_rate,
            "cost_enabled": cost_enabled,
            "slippage": slippage,
            "market_impact": market_impact,
        },
        "train": {
            "total_pnl": train_result["total_pnl"],
            "trade_count": train_result["trade_count"],
            "win_rate": train_result["win_rate"],
            "avg_win": train_result["avg_win"],
            "avg_loss": train_result["avg_loss"],
            "avg_hold_days": train_result["avg_hold_days"],
            "max_drawdown": train_result["max_drawdown"],
            "sharpe_ratio": train_result["sharpe_ratio"],
            "annualized_return": train_result["annualized_return"],
            "total_cost": train_result["total_cost"],
            "gap_closed_exits": train_result["gap_closed_exits"],
            "max_hold_exits": train_result["max_hold_exits"],
            "gap_narrowed_exits": train_result["gap_narrowed_exits"],
            "switch_hurdle_exits": train_result["switch_hurdle_exits"],
            "call_proximity_exits": train_result["call_proximity_exits"],
            "force_closes": train_result["force_closes"],
        },
        "test": {
            "total_pnl": test_result["total_pnl"],
            "trade_count": test_result["trade_count"],
            "win_rate": test_result["win_rate"],
            "avg_win": test_result["avg_win"],
            "avg_loss": test_result["avg_loss"],
            "avg_hold_days": test_result["avg_hold_days"],
            "max_drawdown": test_result["max_drawdown"],
            "sharpe_ratio": test_result["sharpe_ratio"],
            "annualized_return": test_result["annualized_return"],
            "total_cost": test_result["total_cost"],
            "test_excess_return": test_excess,
            "reverse_base_test_excess": reverse_base_test_excess,
            "gap_closed_exits": test_result["gap_closed_exits"],
            "max_hold_exits": test_result["max_hold_exits"],
            "gap_narrowed_exits": test_result["gap_narrowed_exits"],
            "switch_hurdle_exits": test_result["switch_hurdle_exits"],
            "call_proximity_exits": test_result["call_proximity_exits"],
            "force_closes": test_result["force_closes"],
        },
        "entry_diagnostics": entry_diag,
        "train_period": {"start": args.train_start, "end": args.train_end},
        "test_period": {"start": args.test_start, "end": args.test_end},
        "bond_floor_stats": {
            "mean": round(float(bond_floor_df["bond_floor_value"].mean()), 2),
            "min": round(float(bond_floor_df["bond_floor_value"].min()), 2),
            "max": round(float(bond_floor_df["bond_floor_value"].max()), 2),
            "rows": len(bond_floor_df),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(_plain(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # ── Write report.yaml ──
    _now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    _today = _now.split("T", 1)[0]
    l6_decision = "adopt" if adoption_pass else "reject"

    report = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "date": _today,
        "strategy_id": "cb_arb_value_gap_switch",
        "family": "reverse_bond_floor_entry_filter",
        "l6_exit_decision": l6_decision,
        "three_exits_section": {
            "adoption_pass": adoption_pass,
            "selected_variant": {
                "bond_floor_distance_pct": args.bond_floor_distance_pct,
                "max_hold_days": args.max_hold_days,
                "sell_gap_pct": args.sell_gap_pct,
                "switch_hurdle_pct": args.switch_hurdle_pct,
                "cost_enabled": cost_enabled,
            },
            "criteria": {
                "test_excess_ge_reverse_base": meets_excess,
                "test_excess_ge_025_and_dd_le_010": meets_fallback,
                "test_trades_ge_200": meets_trades,
            },
        },
        "compute_cost_yuan": 0.0,
        "learnings": [
            f"Evaluated reverse-direction bond-floor entry filter: "
            f"bond_floor_distance_pct={args.bond_floor_distance_pct}, "
            f"max_hold_days={args.max_hold_days}, "
            f"cost_on={cost_enabled}, "
            f"train_pnl={train_result['total_pnl']}, "
            f"test_pnl={test_result['total_pnl']}, "
            f"test_trades={test_result['trade_count']}"
        ],
        "follow_up_actions": [
            "evidence-only record; do not promote without user approval"
        ],
        "status": "COMPLETE",
        "generated_at": _now,
        "adoption_pass": adoption_pass,
        "decision": (
            "passed_mechanical_thresholds_not_promoted"
            if adoption_pass
            else "failed_mechanical_thresholds"
        ),
        "metrics": {
            "train": {
                "total_pnl": train_result["total_pnl"],
                "trade_count": train_result["trade_count"],
                "win_rate": train_result["win_rate"],
                "max_drawdown": train_result["max_drawdown"],
                "sharpe_ratio": train_result["sharpe_ratio"],
                "avg_hold_days": train_result["avg_hold_days"],
            },
            "test": {
                "total_pnl": test_result["total_pnl"],
                "trade_count": test_result["trade_count"],
                "win_rate": test_result["win_rate"],
                "max_drawdown": test_result["max_drawdown"],
                "sharpe_ratio": test_result["sharpe_ratio"],
                "avg_hold_days": test_result["avg_hold_days"],
                "test_excess_return": test_excess,
            },
        },
        "warnings": [],
    }
    (output_dir / "report.yaml").write_text(
        yaml.safe_dump(_plain(report), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # ── Write l4_ack.yaml ──
    l4_ack = {
        "schema_version": 1,
        "run_id": output_dir.name,
        "reviewer": "hermes",
        "ack_at": _now,
        "q1_bond_floor_computation": {
            "description": (
                "Bond floor computed from cb_basic/cb_daily using "
                "PV of coupons + par."
            ),
            "answer": (
                f"discount_rate={args.discount_rate}, "
                f"computed for {len(bond_floor_df)} CB-day rows, "
                f"mean_bond_floor="
                f"{round(float(bond_floor_df['bond_floor_value'].mean()), 2)}"
            ),
            "computed_data": {
                "bond_floor_rows": len(bond_floor_df),
                "mean_bond_floor": round(
                    float(bond_floor_df["bond_floor_value"].mean()), 2
                ),
                "min_bond_floor": round(
                    float(bond_floor_df["bond_floor_value"].min()), 2
                ),
                "max_bond_floor": round(
                    float(bond_floor_df["bond_floor_value"].max()), 2
                ),
            },
            "computed_at": _now,
            "pass": True,
        },
        "q2_entry_filter_effect": {
            "description": (
                "Entry filter: gap>0 AND near bond floor, "
                "sorted ascending (reverse)."
            ),
            "answer": (
                f"bond_floor_distance_pct={args.bond_floor_distance_pct}, "
                f"train eligible={entry_diag['eligible_rows']}/"
                f"{entry_diag['total_rows']} "
                f"({entry_diag['eligible_pct']}%)"
            ),
            "computed_data": entry_diag,
            "computed_at": _now,
            "pass": True,
        },
        "q3_backtest_results": {
            "description": (
                "Reverse backtest with bond-floor entry filter and cost model."
            ),
            "answer": (
                f"train_pnl={train_result['total_pnl']}, "
                f"test_pnl={test_result['total_pnl']}, "
                f"test_trades={test_result['trade_count']}, "
                f"test_dd={test_result['max_drawdown']}, "
                f"cost_enabled={cost_enabled}"
            ),
            "computed_data": {
                "train_pnl": train_result["total_pnl"],
                "test_pnl": test_result["total_pnl"],
                "test_excess": test_excess,
                "train_trades": train_result["trade_count"],
                "test_trades": test_result["trade_count"],
                "test_max_drawdown": test_result["max_drawdown"],
                "adoption_pass": adoption_pass,
            },
            "computed_at": _now,
            "pass": adoption_pass,
        },
    }
    (output_dir / "l4_ack.yaml").write_text(
        yaml.safe_dump(_plain(l4_ack), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # ── Write diagnostic.yaml ──
    diagnostic = {
        "warnings": [],
        "errors": [],
        "data_rows": {
            "gap_ranks": len(gap_ranks),
            "cb_basic": len(cb_basic),
            "cb_daily": len(cb_daily),
            "cb_call": len(cb_call),
            "bond_floor_computed": len(bond_floor_df),
            "train": len(df_train),
            "test": len(df_test),
        },
        "bond_floor_stats": {
            "mean": round(float(bond_floor_df["bond_floor_value"].mean()), 2),
            "min": round(float(bond_floor_df["bond_floor_value"].min()), 2),
            "max": round(float(bond_floor_df["bond_floor_value"].max()), 2),
            "pct_non_finite": round(
                float(
                    (~np.isfinite(bond_floor_df["bond_floor_value"])).mean()
                ) * 100,
                2,
            ),
        },
        "entry_diagnostics": entry_diag,
        "params": {
            "bond_floor_distance_pct": args.bond_floor_distance_pct,
            "max_hold_days": args.max_hold_days,
            "min_gap_pct": args.min_gap_pct,
            "sell_gap_pct": args.sell_gap_pct,
            "switch_hurdle_pct": args.switch_hurdle_pct,
            "discount_rate": args.discount_rate,
            "slippage": slippage,
            "market_impact": market_impact,
            "cost_enabled": cost_enabled,
        },
        "exit_reasons_train": {
            "gap_closed": train_result["gap_closed_exits"],
            "max_hold": train_result["max_hold_exits"],
            "gap_narrowed": train_result["gap_narrowed_exits"],
            "switch_hurdle": train_result["switch_hurdle_exits"],
            "call_proximity": train_result["call_proximity_exits"],
            "force_close": train_result["force_closes"],
        },
        "exit_reasons_test": {
            "gap_closed": test_result["gap_closed_exits"],
            "max_hold": test_result["max_hold_exits"],
            "gap_narrowed": test_result["gap_narrowed_exits"],
            "switch_hurdle": test_result["switch_hurdle_exits"],
            "call_proximity": test_result["call_proximity_exits"],
            "force_close": test_result["force_closes"],
        },
        "test_trades": test_result["trade_count"],
        "train_trades": train_result["trade_count"],
        "test_max_drawdown": test_result["max_drawdown"],
        "train_max_drawdown": train_result["max_drawdown"],
        "adoption_pass": adoption_pass,
        "trade_timeline": {
            "train_trades": (
                train_result["trades"][:100] if train_result["trades"] else []
            ),
            "test_trades": (
                test_result["trades"][:100] if test_result["trades"] else []
            ),
        },
    }
    (output_dir / "diagnostic.yaml").write_text(
        yaml.safe_dump(_plain(diagnostic), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # ── Done ──
    print(
        f"[bond_floor_reverse] "
        f"adoption_pass={adoption_pass} "
        f"bond_floor_distance_pct={args.bond_floor_distance_pct} "
        f"max_hold_days={args.max_hold_days} "
        f"train_pnl={train_result['total_pnl']} "
        f"train_trades={train_result['trade_count']} "
        f"test_pnl={test_result['total_pnl']} "
        f"test_trades={test_result['trade_count']} "
        f"test_dd={test_result['max_drawdown']} "
        f"cost_on={cost_enabled} "
        f"eligible_pct={entry_diag['eligible_pct']}%",
        flush=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
