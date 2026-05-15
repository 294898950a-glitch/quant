"""Analyze value-repair timing for cb_arb.

This script measures how long bonds take to move from the cheap band to
less-cheap / sell / high bands under the existing cb_arb valuation ranking.
It does not change strategy behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_daily_regime_switch import _build_daily_features  # noqa: E402
from scripts.search_cb_arb_time_split_grid import _base_configs  # noqa: E402
from strategies.cb_arb.cb_pricer import CBSpec, price_cb  # noqa: E402
from strategies.cb_arb.verifier import (  # noqa: E402
    _VOL_CAP,
    _build_call_index,
    _compute_avg_amount_window,
    _compute_realized_vol_window,
    _is_force_redeemed_on_date,
    _load_cb_basic,
    _load_cb_call,
    _load_cb_daily,
    _load_stk_daily,
    _load_trading_days,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--start", default="20190101")
    p.add_argument("--end", default="20241231")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--cheap-pct", type=float, default=0.10)
    p.add_argument("--sell-pct", type=float, default=0.50)
    p.add_argument("--high-pct", type=float, default=0.80)
    p.add_argument("--max-days", type=int, default=360)
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--reuse-ranks", action="store_true")
    return p.parse_args()


def _trading_day_distance(day_index: dict[str, int], start: str, end: str) -> int | None:
    if not start or not end or start not in day_index or end not in day_index:
        return None
    return int(day_index[end] - day_index[start])


def _q(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * pct))))
    return round(float(s[idx]), 3)


def _summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p25": _q(values, 0.25),
        "p50": _q(values, 0.50),
        "p75": _q(values, 0.75),
        "p90": _q(values, 0.90),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _compute_daily_ranks(
    root: Path,
    start: str,
    end: str,
    fixed_source: int,
    rule: str,
    config_overrides: dict[str, float] | None = None,
) -> pd.DataFrame:
    cfgs = _base_configs(root, fixed_source)
    if config_overrides:
        cfgs = {
            regime: _apply_config_overrides(cfg, config_overrides)
            for regime, cfg in cfgs.items()
        }
    features = _build_daily_features(252, rule)
    config_by_date = {d: cfgs[f["regime"]] for d, f in features.items()}
    vol_cap = float(config_overrides.get("vol_cap", _VOL_CAP)) if config_overrides else _VOL_CAP

    cb_basic = _load_cb_basic()
    cb_daily = _load_cb_daily()
    cb_call = _load_cb_call()
    stk_daily = _load_stk_daily()
    trading_days = [d for d in _load_trading_days() if start <= d <= end]
    days_set = set(trading_days)

    cb_daily_sub = cb_daily[cb_daily["trade_date"].isin(days_set)].copy()
    cb_daily_sub = cb_daily_sub.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    cb_daily_sub["amount_20d"] = _compute_avg_amount_window(cb_daily_sub, 20)

    cb_basic_active = cb_basic[cb_basic["ts_code"].isin(set(cb_daily_sub["ts_code"]))]
    relevant_stk_codes = set(cb_basic_active["stk_code"].dropna().tolist())
    stk_daily_sub = stk_daily[
        (stk_daily["stk_code"].isin(relevant_stk_codes))
        & (stk_daily["trade_date"].isin(days_set))
    ].copy()
    stk_close_map = {
        (row.stk_code, row.trade_date): float(row.close)
        for row in stk_daily_sub.itertuples(index=False)
    }

    vol_windows = sorted({int(c.vol_window_days) for c in cfgs.values()})
    vol_maps = {
        w: _compute_realized_vol_window(stk_daily, relevant_stk_codes, trading_days, w)
        for w in vol_windows
    }
    call_index = _build_call_index(cb_call)

    basic_lookup: dict[str, dict[str, Any]] = {}
    for row in cb_basic.itertuples(index=False):
        basic_lookup[row.ts_code] = {
            "bond_short_name": row.bond_short_name or row.ts_code,
            "stk_code": row.stk_code,
            "issue_size_yuan": float(row.issue_size_yuan)
            if math.isfinite(row.issue_size_yuan)
            else 0.0,
            "conv_price": float(row.conv_price)
            if row.conv_price is not None and math.isfinite(row.conv_price)
            else float("nan"),
            "list_date": row.list_date or "",
            "maturity_date": row.maturity_date or "",
            "coupon_rate": float(row.coupon_rate) if math.isfinite(row.coupon_rate) else 0.01,
            "rating": row.rating or "AA",
            "rating_int": int(row.rating_int),
        }

    daily_by_date = {d: g for d, g in cb_daily_sub.groupby("trade_date")}
    credit_cache: dict[tuple[float, float], dict[str, float]] = {}
    today_dt_cache: dict[str, datetime] = {}

    def credit_spread_for(day_cfg: Any) -> dict[str, float]:
        key = (float(day_cfg.credit_spread_aaa_bp), float(day_cfg.credit_spread_aa_bp))
        if key not in credit_cache:
            credit_cache[key] = day_cfg.credit_spread_dict()
        return credit_cache[key]

    rows_out: list[dict[str, Any]] = []
    for idx, date in enumerate(trading_days, 1):
        day_cfg = config_by_date.get(date, cfgs["neutral"])
        rows_today = daily_by_date.get(date)
        if rows_today is None or rows_today.empty:
            continue
        deviations: list[tuple[str, str, float, float, float, float, float, float]] = []
        for r in rows_today.itertuples(index=False):
            ts = r.ts_code
            mkt = float(r.close)
            spec_d = basic_lookup.get(ts)
            if spec_d is None or not spec_d["stk_code"]:
                continue
            if spec_d["rating_int"] < day_cfg.rating_floor_int:
                continue
            if spec_d["issue_size_yuan"] < day_cfg.min_remaining_size:
                continue
            if float(getattr(r, "amount_20d", 0.0)) < day_cfg.min_avg_amount:
                continue
            mat = spec_d["maturity_date"]
            if not mat or len(mat) != 8:
                continue
            try:
                tdy_dt = today_dt_cache.get(date)
                if tdy_dt is None:
                    tdy_dt = datetime.strptime(date, "%Y%m%d")
                    today_dt_cache[date] = tdy_dt
                if (datetime.strptime(mat, "%Y%m%d") - tdy_dt).days <= 30:
                    continue
            except Exception:
                continue

            stock_price = stk_close_map.get((spec_d["stk_code"], date))
            if stock_price is None or stock_price <= 0:
                continue
            vol = vol_maps.get(day_cfg.vol_window_days, {}).get((spec_d["stk_code"], date))
            if vol is None or not math.isfinite(vol) or vol <= 0:
                continue
            conv_price = spec_d["conv_price"]
            if not math.isfinite(conv_price) or conv_price <= 0:
                continue
            try:
                val = price_cb(
                    spec=CBSpec(
                        ts_code=ts,
                        face_value=100.0,
                        conv_price=conv_price,
                        list_date=spec_d["list_date"],
                        maturity_date=mat,
                        coupon_rate=spec_d["coupon_rate"],
                        rating=spec_d["rating"],
                    ),
                    valuation_date=date,
                    stock_price=stock_price,
                    stock_vol=min(vol_cap, vol * day_cfg.vol_multiplier),
                    risk_free_rate=0.025,
                    credit_spread_bp=credit_spread_for(day_cfg),
                    is_force_redeemed=_is_force_redeemed_on_date(ts, date, call_index),
                )
            except Exception:
                continue
            theo = val.theoretical
            if not math.isfinite(theo) or theo <= 0:
                continue
            dev = (mkt - theo) / theo
            if math.isfinite(dev):
                deviations.append(
                    (
                        ts,
                        spec_d["bond_short_name"],
                        dev,
                        mkt,
                        theo,
                        float(val.bond_floor),
                        float(val.option_value),
                        float(val.intrinsic),
                    )
                )
        deviations.sort(key=lambda x: x[2])
        n = len(deviations)
        if n <= 0:
            continue
        for rank, (ts, name, dev, close, theo, bond_floor, option_value, intrinsic) in enumerate(deviations):
            rows_out.append(
                {
                    "trade_date": date,
                    "ts_code": ts,
                    "name": name,
                    "close": round(close, 6),
                    "theoretical": round(theo, 6),
                    "bond_floor": round(bond_floor, 6),
                    "option_value": round(option_value, 6),
                    "intrinsic": round(intrinsic, 6),
                    "deviation": round(dev, 8),
                    "rank": rank,
                    "n_ranked": n,
                    "rank_pct": round(rank / n, 8),
                    "regime": features.get(date, {}).get("regime", "neutral"),
                }
            )
        if idx % 100 == 0:
            print(f"[repair] ranked {idx}/{len(trading_days)} {date}", flush=True)
    return pd.DataFrame(rows_out)


def _apply_config_overrides(cfg: Any, overrides: dict[str, float]) -> Any:
    values = asdict(cfg)
    if "vol_multiplier_factor" in overrides:
        values["vol_multiplier"] = max(
            0.1,
            float(values["vol_multiplier"]) * float(overrides["vol_multiplier_factor"]),
        )
    if "credit_spread_add_bp" in overrides:
        add_bp = float(overrides["credit_spread_add_bp"])
        values["credit_spread_aaa_bp"] = max(1.0, float(values["credit_spread_aaa_bp"]) + add_bp)
        values["credit_spread_aa_bp"] = max(
            values["credit_spread_aaa_bp"] + 1.0,
            float(values["credit_spread_aa_bp"]) + add_bp,
        )
    for key, val in overrides.items():
        if key in values:
            values[key] = val
    return replace(cfg, **values)


def _extract_episodes(
    ranks: pd.DataFrame,
    trading_days: list[str],
    cheap_pct: float,
    sell_pct: float,
    high_pct: float,
    max_days: int,
) -> pd.DataFrame:
    day_index = {d: i for i, d in enumerate(trading_days)}
    price_map = {
        (r.ts_code, r.trade_date): float(r.close)
        for r in ranks.itertuples(index=False)
    }
    rows: list[dict[str, Any]] = []
    for ts, g in ranks.sort_values("trade_date").groupby("ts_code"):
        g = g.reset_index(drop=True)
        in_episode = False
        start_row = None
        for i, row in g.iterrows():
            is_cheap = float(row.rank_pct) <= cheap_pct
            if is_cheap and not in_episode:
                in_episode = True
                start_row = row
                continue
            if in_episode and start_row is not None:
                start_date = str(start_row.trade_date)
                date = str(row.trade_date)
                if day_index.get(date, 0) - day_index.get(start_date, 0) > max_days:
                    in_episode = False
                    start_row = None
                    continue
                if not is_cheap:
                    future = g.iloc[i:].copy()
                    exit_cheap = row
                    sell_hit = future[future["rank_pct"].astype(float) >= sell_pct]
                    high_hit = future[future["rank_pct"].astype(float) >= high_pct]
                    sell_row = sell_hit.iloc[0] if not sell_hit.empty else None
                    high_row = high_hit.iloc[0] if not high_hit.empty else None
                    start_close = float(start_row.close)

                    def ret_to(r2: Any) -> float | None:
                        if r2 is None or start_close <= 0:
                            return None
                        return round(float(r2.close) / start_close - 1.0, 6)

                    rows.append(
                        {
                            "ts_code": ts,
                            "name": start_row["name"],
                            "start_date": start_date,
                            "start_close": round(start_close, 6),
                            "start_deviation": float(start_row.deviation),
                            "start_rank_pct": float(start_row.rank_pct),
                            "exit_cheap_date": str(exit_cheap.trade_date),
                            "exit_cheap_days": _trading_day_distance(
                                day_index, start_date, str(exit_cheap.trade_date)
                            ),
                            "exit_cheap_return": ret_to(exit_cheap),
                            "sell_band_date": str(sell_row.trade_date) if sell_row is not None else "",
                            "sell_band_days": _trading_day_distance(
                                day_index, start_date, str(sell_row.trade_date)
                            )
                            if sell_row is not None
                            else None,
                            "sell_band_return": ret_to(sell_row),
                            "high_band_date": str(high_row.trade_date) if high_row is not None else "",
                            "high_band_days": _trading_day_distance(
                                day_index, start_date, str(high_row.trade_date)
                            )
                            if high_row is not None
                            else None,
                            "high_band_return": ret_to(high_row),
                        }
                    )
                    in_episode = False
                    start_row = None
    return pd.DataFrame(rows)


def _add_value_gap_amounts(ranks: pd.DataFrame, root: Path, fixed_source: int) -> pd.DataFrame:
    cfgs = _base_configs(root, fixed_source)
    position_cash_by_regime = {
        regime: float(cfg.initial_capital) * float(cfg.max_position_pct)
        for regime, cfg in cfgs.items()
    }
    fee_by_regime = {regime: float(cfg.fee_pct) for regime, cfg in cfgs.items()}
    rows = ranks.copy()
    rows["position_cash"] = rows["regime"].map(position_cash_by_regime).fillna(
        position_cash_by_regime.get("neutral", 30000.0)
    )
    rows["fee_pct"] = rows["regime"].map(fee_by_regime).fillna(
        fee_by_regime.get("neutral", 0.0003)
    )
    rows["buy_qty"] = (
        rows["position_cash"].astype(float)
        / rows["close"].astype(float).clip(lower=1e-9)
        / (1.0 + rows["fee_pct"].astype(float))
    ).apply(math.floor)
    rows["value_gap_amount"] = (
        (rows["theoretical"].astype(float) - rows["close"].astype(float))
        * rows["buy_qty"].astype(float)
    )
    rows["value_gap_pct_of_cash"] = rows["value_gap_amount"] / rows[
        "position_cash"
    ].astype(float).clip(lower=1e-9)
    return rows


def _extract_value_gap_episodes(
    ranks: pd.DataFrame,
    trading_days: list[str],
    max_days: int,
) -> pd.DataFrame:
    day_index = {d: i for i, d in enumerate(trading_days)}
    rows: list[dict[str, Any]] = []
    for ts, g in ranks.sort_values("trade_date").groupby("ts_code"):
        g = g.reset_index(drop=True)
        in_episode = False
        start_row = None
        for i, row in g.iterrows():
            is_undervalued = float(row.value_gap_amount) > 0
            if is_undervalued and not in_episode:
                in_episode = True
                start_row = row
                continue
            if in_episode and start_row is not None:
                start_date = str(start_row.trade_date)
                date = str(row.trade_date)
                if day_index.get(date, 0) - day_index.get(start_date, 0) > max_days:
                    in_episode = False
                    start_row = None
                    continue
                if not is_undervalued:
                    start_close = float(start_row.close)
                    rows.append(
                        {
                            "ts_code": ts,
                            "name": start_row["name"],
                            "start_date": start_date,
                            "start_close": round(start_close, 6),
                            "start_theoretical": round(float(start_row.theoretical), 6),
                            "start_gap_amount": round(float(start_row.value_gap_amount), 2),
                            "start_gap_pct_of_cash": round(
                                float(start_row.value_gap_pct_of_cash), 6
                            ),
                            "fair_or_overvalued_date": str(row.trade_date),
                            "fair_or_overvalued_days": _trading_day_distance(
                                day_index, start_date, str(row.trade_date)
                            ),
                            "fair_or_overvalued_close": round(float(row.close), 6),
                            "fair_or_overvalued_gap_amount": round(
                                float(row.value_gap_amount), 2
                            ),
                            "price_return_to_fair": round(
                                float(row.close) / start_close - 1.0, 6
                            )
                            if start_close > 0
                            else None,
                        }
                    )
                    in_episode = False
                    start_row = None
    return pd.DataFrame(rows)


def _big_winner_value_gap_rows(
    output_dir: Path, episodes: pd.DataFrame, ranks: pd.DataFrame
) -> list[dict[str, Any]]:
    winners_path = output_dir.parent / "phase_loss_review" / "worst_rolling_window_winners.csv"
    if not winners_path.exists():
        return []
    winners = pd.read_csv(winners_path, dtype={"ts_code": str, "phase_start": str, "phase_end": str})
    winners = winners[winners["was_traded"].astype(str).str.lower().isin(["false", "0"])]
    rows: list[dict[str, Any]] = []
    for _, r in winners.iterrows():
        ts = str(r["ts_code"])
        start = str(r["phase_start"])
        end = str(r["phase_end"])
        rr = ranks[
            (ranks["ts_code"] == ts)
            & (ranks["trade_date"] >= start)
            & (ranks["trade_date"] <= end)
        ]
        eps = episodes[
            (episodes["ts_code"] == ts)
            & (episodes["start_date"] >= start)
            & (episodes["start_date"] <= end)
        ]
        positive = rr[rr["value_gap_amount"].astype(float) > 0]
        rows.append(
            {
                "phase": int(r["phase"]),
                "phase_start": start,
                "phase_end": end,
                "ts_code": ts,
                "name": str(r["name"]),
                "phase_return": round(float(r["return"]), 6),
                "ever_eligible_in_phase": not rr.empty,
                "ever_positive_gap_in_phase": not positive.empty,
                "max_gap_amount": round(float(rr["value_gap_amount"].max()), 2)
                if not rr.empty
                else None,
                "max_gap_pct_of_cash": round(float(rr["value_gap_pct_of_cash"].max()), 6)
                if not rr.empty
                else None,
                "first_positive_gap_date": str(positive.iloc[0]["trade_date"])
                if not positive.empty
                else "",
                "positive_gap_episode_count": len(eps),
                "first_fair_days": eps.iloc[0]["fair_or_overvalued_days"]
                if not eps.empty
                else None,
            }
        )
    return rows


def _big_winner_rows(output_dir: Path, episodes: pd.DataFrame, ranks: pd.DataFrame) -> list[dict[str, Any]]:
    winners_path = output_dir.parent / "phase_loss_review" / "worst_rolling_window_winners.csv"
    if not winners_path.exists():
        return []
    winners = pd.read_csv(winners_path, dtype={"ts_code": str, "phase_start": str, "phase_end": str})
    winners = winners[winners["was_traded"].astype(str).str.lower().isin(["false", "0"])]
    rows: list[dict[str, Any]] = []
    for _, r in winners.iterrows():
        eps = episodes[
            (episodes["ts_code"] == str(r["ts_code"]))
            & (episodes["start_date"] >= str(r["phase_start"]))
            & (episodes["start_date"] <= str(r["phase_end"]))
        ]
        rr = ranks[
            (ranks["ts_code"] == str(r["ts_code"]))
            & (ranks["trade_date"] >= str(r["phase_start"]))
            & (ranks["trade_date"] <= str(r["phase_end"]))
        ]
        rows.append(
            {
                "phase": int(r["phase"]),
                "phase_start": str(r["phase_start"]),
                "phase_end": str(r["phase_end"]),
                "ts_code": str(r["ts_code"]),
                "name": str(r["name"]),
                "phase_return": round(float(r["return"]), 6),
                "ever_ranked_in_phase": not rr.empty,
                "best_rank_pct_in_phase": round(float(rr["rank_pct"].min()), 6) if not rr.empty else None,
                "cheap_episode_count_in_phase": len(eps),
                "first_cheap_start": eps.iloc[0]["start_date"] if not eps.empty else "",
                "first_sell_band_days": eps.iloc[0]["sell_band_days"] if not eps.empty else None,
                "first_high_band_days": eps.iloc[0]["high_band_days"] if not eps.empty else None,
            }
        )
    return rows


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "repair_time_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    ranks_path = output_dir / "daily_value_ranks.parquet"

    if args.reuse_ranks and ranks_path.exists():
        ranks = pd.read_parquet(ranks_path)
    else:
        ranks = _compute_daily_ranks(
            args.data_root,
            args.start,
            args.end,
            args.fixed_source,
            args.rule,
        )
        ranks.to_parquet(ranks_path, index=False)
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    trading_days = [d for d in _load_trading_days() if args.start <= d <= args.end]

    value_ranks = _add_value_gap_amounts(ranks, args.data_root, args.fixed_source)
    value_ranks.to_parquet(output_dir / "daily_value_gap_amounts.parquet", index=False)
    value_episodes = _extract_value_gap_episodes(value_ranks, trading_days, args.max_days)
    value_episodes.to_csv(output_dir / "value_gap_repair_episodes.csv", index=False)
    value_days = (
        [float(x) for x in value_episodes["fair_or_overvalued_days"].dropna().tolist()]
        if not value_episodes.empty
        else []
    )
    value_big_rows = _big_winner_value_gap_rows(output_dir, value_episodes, value_ranks)
    _write_csv(output_dir / "missed_big_winner_value_gap_profile.csv", value_big_rows)
    value_big_counts = Counter()
    for r in value_big_rows:
        if not r["ever_eligible_in_phase"]:
            value_big_counts["not_eligible"] += 1
        elif not r["ever_positive_gap_in_phase"]:
            value_big_counts["eligible_but_never_undervalued"] += 1
        else:
            value_big_counts["had_positive_gap"] += 1

    episodes = _extract_episodes(
        ranks,
        trading_days,
        args.cheap_pct,
        args.sell_pct,
        args.high_pct,
        args.max_days,
    )
    episodes.to_csv(output_dir / "repair_episodes.csv", index=False)

    sell_days = [float(x) for x in episodes["sell_band_days"].dropna().tolist()] if not episodes.empty else []
    high_days = [float(x) for x in episodes["high_band_days"].dropna().tolist()] if not episodes.empty else []
    exit_days = [float(x) for x in episodes["exit_cheap_days"].dropna().tolist()] if not episodes.empty else []
    big_rows = _big_winner_rows(output_dir, episodes, ranks)
    _write_csv(output_dir / "missed_big_winner_repair_profile.csv", big_rows)

    cheap_counts = Counter()
    if big_rows:
        for r in big_rows:
            if not r["ever_ranked_in_phase"]:
                cheap_counts["not_ranked"] += 1
            elif int(r["cheap_episode_count_in_phase"]) <= 0:
                cheap_counts["ranked_but_never_cheap"] += 1
            else:
                cheap_counts["had_cheap_episode"] += 1

    summary = {
        "start": args.start,
        "end": args.end,
        "cheap_pct": args.cheap_pct,
        "sell_pct": args.sell_pct,
        "high_pct": args.high_pct,
        "n_rank_rows": len(ranks),
        "n_episodes": len(episodes),
        "value_gap_method": "(theoretical - close) * floor(position_cash / close / (1 + fee))",
        "n_value_gap_episodes": len(value_episodes),
        "value_gap_to_fair_or_overvalued_days": _summary(value_days),
        "missed_big_winner_value_gap_counts": dict(value_big_counts),
        "exit_cheap_days": _summary(exit_days),
        "sell_band_days": _summary(sell_days),
        "high_band_days": _summary(high_days),
        "missed_big_winner_counts": dict(cheap_counts),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"[repair] wrote {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
