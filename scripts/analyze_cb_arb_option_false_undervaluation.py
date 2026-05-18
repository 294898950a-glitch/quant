"""Analyze unreliable option-sourced value gaps in cb_arb value-gap trades."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_daily_regime_switch import _build_daily_features  # noqa: E402
from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _build_panic_dates,
    _gap_source_shares,
    _run_value_gap_backtest,
)


BASE_PARAMS: dict[str, Any] = {
    "min_gap_pct": 0.0,
    "sell_gap_pct": 0.0,
    "switch_hurdle_pct": 0.03,
    "max_hold_days": 180.0,
    "stop_gap_ratio_floor": 0.30,
    "stop_signal_threshold": 999.0,
    "cost_model_enabled": 1.0,
    "slippage_pct": 0.0015,
    "market_impact_coeff": 0.0010,
    "market_impact_cap_pct": 0.02,
    "holding_cost_pct": 0.0,
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=Path("data/cb_arb_concurrent_supervised_20260511_094500"))
    p.add_argument(
        "--ranks-path",
        type=Path,
        default=Path("data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet"),
    )
    p.add_argument("--start-year", type=int, default=2019)
    p.add_argument("--end-year", type=int, default=2024)
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_option_false_undervalue_attribution_2026-05-17"),
    )
    return p.parse_args()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _load_stock_feature_map() -> dict[tuple[str, str], dict[str, float]]:
    stk = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/stk_daily_qfq.parquet")
    stk["trade_date"] = stk["trade_date"].astype(str)
    stk = stk.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    stk["ret_5d"] = stk.groupby("ts_code")["close"].pct_change(5)
    stk["ret_20d"] = stk.groupby("ts_code")["close"].pct_change(20)
    stk["ret_60d"] = stk.groupby("ts_code")["close"].pct_change(60)
    log_ret = stk.groupby("ts_code")["close"].transform(lambda s: (s.astype(float) / s.astype(float).shift(1)).map(_safe_log))
    stk["vol_20d"] = (
        log_ret.groupby(stk["ts_code"])
        .rolling(window=20, min_periods=10)
        .std()
        .reset_index(level=0, drop=True)
        * math.sqrt(252)
    )
    stk["vol_60d"] = (
        log_ret.groupby(stk["ts_code"])
        .rolling(window=60, min_periods=20)
        .std()
        .reset_index(level=0, drop=True)
        * math.sqrt(252)
    )
    out: dict[tuple[str, str], dict[str, float]] = {}
    for row in stk.itertuples(index=False):
        item = {
            "stock_close": _num(row.close),
            "stock_ret_5d": _num(row.ret_5d),
            "stock_ret_20d": _num(row.ret_20d),
            "stock_ret_60d": _num(row.ret_60d),
            "stock_vol_20d": _num(row.vol_20d),
            "stock_vol_60d": _num(row.vol_60d),
        }
        out[(str(row.ts_code), str(row.trade_date))] = item
        out[(str(row.stk_code), str(row.trade_date))] = item
    return out


def _safe_log(value: float) -> float:
    try:
        if value and value > 0:
            return math.log(value)
    except Exception:
        pass
    return float("nan")


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _basic_lookup() -> dict[str, dict[str, Any]]:
    basic = pd.read_parquet(_REPO_ROOT / "data/cb_warehouse/cb_basic.parquet")
    out: dict[str, dict[str, Any]] = {}
    for row in basic.itertuples(index=False):
        out[str(row.ts_code)] = {
            "stk_code": str(row.stk_code),
            "maturity_date": str(row.maturity_date) if row.maturity_date is not None else "",
            "conv_price": _num(row.conv_price),
            "rating": str(row.rating) if row.rating is not None else "",
        }
    return out


def _years_to_maturity(entry_date: str, maturity_date: str) -> float | None:
    if not entry_date or not maturity_date or len(maturity_date) != 8:
        return None
    try:
        return (datetime.strptime(maturity_date, "%Y%m%d") - datetime.strptime(entry_date, "%Y%m%d")).days / 365.25
    except Exception:
        return None


def _candidate_flags(row: dict[str, Any]) -> dict[str, bool]:
    ret20 = row.get("stock_ret_20d")
    ratio = row.get("close_to_bond_floor")
    vol60 = row.get("stock_vol_60d")
    vol20 = row.get("stock_vol_20d")
    years = row.get("years_to_maturity")
    sigma_ratio = row.get("sigma_ratio_20_60")
    return {
        "flag_strong_stock_ret20": ret20 is not None and ret20 > 0.05,
        "flag_far_from_bond_floor": ratio is not None and ratio > 1.50,
        "flag_high_vol60": vol60 is not None and vol60 > 0.30,
        "flag_short_vol_below_long": sigma_ratio is not None and sigma_ratio < 0.80,
        "flag_long_maturity": years is not None and years > 1.50,
        "flag_hot_option_setup": (
            ret20 is not None
            and ratio is not None
            and vol60 is not None
            and ret20 > 0.05
            and ratio > 1.50
            and vol60 > 0.30
        ),
        "flag_hot_trend_sigma_setup": (
            ret20 is not None
            and ratio is not None
            and vol60 is not None
            and sigma_ratio is not None
            and ret20 > 0.05
            and ratio > 1.50
            and vol60 > 0.30
            and sigma_ratio < 0.80
        ),
    }


def _trade_rows(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ranks = pd.read_parquet(args.ranks_path)
    ranks["trade_date"] = ranks["trade_date"].astype(str)
    rank_by_key = {(str(r.trade_date), str(r.ts_code)): r for r in ranks.itertuples(index=False)}
    features = _build_daily_features(252, args.rule)
    stock_features = _load_stock_feature_map()
    basic = _basic_lookup()
    rows: list[dict[str, Any]] = []
    yearly_metrics: list[dict[str, Any]] = []

    for year in range(args.start_year, args.end_year + 1):
        start = f"{year}0101"
        end = f"{year}1231"
        year_ranks = ranks[(ranks["trade_date"] >= start) & (ranks["trade_date"] <= end)]
        result = _run_value_gap_backtest(
            year_ranks,
            start,
            end,
            args.data_root,
            args.fixed_source,
            args.rule,
            BASE_PARAMS,
        )
        yearly_metrics.append({"year": year, **result["metrics"]})
        panic_dates = _build_panic_dates(start, end, BASE_PARAMS)
        for trade in result["trades"]:
            ts = str(trade["cb_code"])
            entry_date = str(trade["entry_date"])
            rank_row = rank_by_key.get((entry_date, ts))
            source = "missing"
            bond_share = 0.0
            option_share = 0.0
            close_to_bond_floor = None
            if rank_row is not None:
                source, bond_share, option_share = _gap_source_shares(rank_row, {})
                bond_floor = _num(getattr(rank_row, "bond_floor", None))
                close = _num(getattr(rank_row, "close", None))
                if bond_floor and close:
                    close_to_bond_floor = close / bond_floor

            cb_info = basic.get(ts, {})
            stk_code = cb_info.get("stk_code")
            stock = stock_features.get((stk_code, entry_date), {}) if stk_code else {}
            vol20 = stock.get("stock_vol_20d")
            vol60 = stock.get("stock_vol_60d")
            sigma_ratio = vol20 / vol60 if vol20 is not None and vol60 not in (None, 0) else None
            conv_price = cb_info.get("conv_price")
            stock_close = stock.get("stock_close")
            moneyness = stock_close / conv_price if stock_close is not None and conv_price not in (None, 0) else None
            years = _years_to_maturity(entry_date, str(cb_info.get("maturity_date", "")))
            row = {
                **trade,
                "year": year,
                "source": source,
                "bond_share": round(float(bond_share), 6),
                "option_share": round(float(option_share), 6),
                "close_to_bond_floor": _round(close_to_bond_floor),
                "entry_regime": features.get(entry_date, {}).get("regime", "unknown"),
                "entry_panic": entry_date in panic_dates,
                "stk_code": stk_code,
                "stock_ret_5d": _round(stock.get("stock_ret_5d")),
                "stock_ret_20d": _round(stock.get("stock_ret_20d")),
                "stock_ret_60d": _round(stock.get("stock_ret_60d")),
                "stock_vol_20d": _round(vol20),
                "stock_vol_60d": _round(vol60),
                "sigma_ratio_20_60": _round(sigma_ratio),
                "years_to_maturity": _round(years),
                "moneyness_stock_to_conv": _round(moneyness),
                "rating": cb_info.get("rating"),
            }
            row.update({key: int(value) for key, value in _candidate_flags(row).items()})
            rows.append(row)
    return rows, yearly_metrics


def _round(value: Any, ndigits: int = 6) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    return round(parsed, ndigits)


def _flag_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    option_rows = [r for r in rows if r.get("source") == "option"]
    total_loss = -sum(float(r["pnl_amount"]) for r in option_rows if float(r["pnl_amount"]) < 0)
    total_profit = sum(float(r["pnl_amount"]) for r in option_rows if float(r["pnl_amount"]) > 0)
    flags = [
        "flag_strong_stock_ret20",
        "flag_far_from_bond_floor",
        "flag_high_vol60",
        "flag_short_vol_below_long",
        "flag_long_maturity",
        "flag_hot_option_setup",
        "flag_hot_trend_sigma_setup",
    ]
    out: list[dict[str, Any]] = []
    for flag in flags:
        selected = [r for r in option_rows if int(r.get(flag, 0)) == 1]
        not_selected = [r for r in option_rows if int(r.get(flag, 0)) != 1]
        loss = -sum(float(r["pnl_amount"]) for r in selected if float(r["pnl_amount"]) < 0)
        profit = sum(float(r["pnl_amount"]) for r in selected if float(r["pnl_amount"]) > 0)
        selected_loss_trades = sum(1 for r in selected if float(r["pnl_amount"]) < 0)
        selected_profit_trades = sum(1 for r in selected if float(r["pnl_amount"]) > 0)
        out.append(
            {
                "flag": flag,
                "selected_trades": len(selected),
                "selected_loss_trades": selected_loss_trades,
                "selected_profit_trades": selected_profit_trades,
                "selected_loss_amount_abs": round(loss, 2),
                "selected_profit_amount": round(profit, 2),
                "loss_capture_rate": round(loss / total_loss, 6) if total_loss else None,
                "profit_capture_rate": round(profit / total_profit, 6) if total_profit else None,
                "avg_pnl_selected": round(sum(float(r["pnl_amount"]) for r in selected) / len(selected), 2) if selected else None,
                "avg_pnl_not_selected": round(sum(float(r["pnl_amount"]) for r in not_selected) / len(not_selected), 2) if not_selected else None,
            }
        )
    return out


def _bin_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    option_rows = [r for r in rows if r.get("source") == "option"]
    specs = {
        "close_to_bond_floor": [1.05, 1.20, 1.50, 2.00],
        "stock_ret_20d": [-0.10, 0.0, 0.05, 0.15, 0.30],
        "stock_vol_60d": [0.15, 0.30, 0.50, 0.80],
        "sigma_ratio_20_60": [0.60, 0.80, 1.00, 1.30],
        "years_to_maturity": [1.0, 1.5, 3.0, 5.0],
        "moneyness_stock_to_conv": [0.80, 1.00, 1.30, 1.60],
    }
    out: list[dict[str, Any]] = []
    for field, cuts in specs.items():
        for label, selected in _bins(option_rows, field, cuts):
            if not selected:
                continue
            pnl = sum(float(r["pnl_amount"]) for r in selected)
            losses = [r for r in selected if float(r["pnl_amount"]) < 0]
            out.append(
                {
                    "field": field,
                    "bin": label,
                    "count": len(selected),
                    "loss_count": len(losses),
                    "loss_rate": round(len(losses) / len(selected), 6),
                    "pnl_amount": round(pnl, 2),
                    "avg_pnl": round(pnl / len(selected), 2),
                }
            )
    return out


def _bins(rows: list[dict[str, Any]], field: str, cuts: list[float]) -> list[tuple[str, list[dict[str, Any]]]]:
    bins: list[tuple[str, list[dict[str, Any]]]] = []
    prev: float | None = None
    for cut in cuts:
        if prev is None:
            label = f"<= {cut:g}"
            selected = [r for r in rows if r.get(field) is not None and float(r[field]) <= cut]
        else:
            label = f"({prev:g}, {cut:g}]"
            selected = [r for r in rows if r.get(field) is not None and prev < float(r[field]) <= cut]
        bins.append((label, selected))
        prev = cut
    label = f"> {cuts[-1]:g}"
    bins.append((label, [r for r in rows if r.get(field) is not None and float(r[field]) > cuts[-1]]))
    return bins


def _year_summary(rows: list[dict[str, Any]], yearly_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_year = {int(m["year"]): dict(m) for m in yearly_metrics}
    out: list[dict[str, Any]] = []
    for year in sorted({int(r["year"]) for r in rows}):
        year_rows = [r for r in rows if int(r["year"]) == year]
        option = [r for r in year_rows if r.get("source") == "option"]
        losses = [r for r in option if float(r["pnl_amount"]) < 0]
        hot = [r for r in losses if int(r.get("flag_hot_option_setup", 0)) == 1]
        hot_trend = [r for r in losses if int(r.get("flag_hot_trend_sigma_setup", 0)) == 1]
        loss_abs = -sum(float(r["pnl_amount"]) for r in losses)
        hot_abs = -sum(float(r["pnl_amount"]) for r in hot)
        hot_trend_abs = -sum(float(r["pnl_amount"]) for r in hot_trend)
        out.append(
            {
                "year": year,
                "total_return": by_year.get(year, {}).get("total_return"),
                "excess_return": by_year.get(year, {}).get("excess_return"),
                "max_drawdown": by_year.get(year, {}).get("max_drawdown"),
                "option_trades": len(option),
                "option_loss_trades": len(losses),
                "option_loss_amount_abs": round(loss_abs, 2),
                "hot_option_loss_amount_abs": round(hot_abs, 2),
                "hot_option_loss_capture": round(hot_abs / loss_abs, 6) if loss_abs else None,
                "hot_trend_sigma_loss_amount_abs": round(hot_trend_abs, 2),
                "hot_trend_sigma_loss_capture": round(hot_trend_abs / loss_abs, 6) if loss_abs else None,
            }
        )
    return out


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows, yearly_metrics = _trade_rows(args)
    option_rows = [r for r in rows if r.get("source") == "option"]
    flag_rows = _flag_summary(rows)
    bin_rows = _bin_summary(rows)
    year_rows = _year_summary(rows, yearly_metrics)

    _write_csv(args.output_dir / "option_trade_features.csv", option_rows)
    _write_csv(args.output_dir / "flag_summary.csv", flag_rows)
    _write_csv(args.output_dir / "bin_summary.csv", bin_rows)
    _write_csv(args.output_dir / "year_summary.csv", year_rows)

    hot = next((r for r in flag_rows if r["flag"] == "flag_hot_option_setup"), {})
    hot_trend = next((r for r in flag_rows if r["flag"] == "flag_hot_trend_sigma_setup"), {})
    report = {
        "schema_version": 1,
        "run_id": "cb_arb_option_false_undervalue_attribution_2026-05-17",
        "status": "COMPLETE",
        "scope": "local attribution only; no VM/spot; no strategy change",
        "inputs": {
            "data_root": str(args.data_root),
            "ranks_path": str(args.ranks_path),
            "years": [args.start_year, args.end_year],
        },
        "hypothesis": "False option-sourced undervaluation concentrates where CB price is far above bond floor, stock has recently trended up, and historical volatility is high.",
        "key_metrics": {
            "option_trade_count": len(option_rows),
            "hot_option_setup": hot,
            "hot_trend_sigma_setup": hot_trend,
        },
        "artifacts": [
            "option_trade_features.csv",
            "flag_summary.csv",
            "bin_summary.csv",
            "year_summary.csv",
        ],
    }
    (args.output_dir / "report.yaml").write_text(yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (args.output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(yaml.safe_dump(report["key_metrics"], allow_unicode=True, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
