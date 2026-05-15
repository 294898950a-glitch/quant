"""Build and score a CB market panic detector."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PanicWindow:
    name: str
    start: str
    end: str
    kind: str


KNOWN_WINDOWS = [
    PanicWindow("2015_stock_crash_main", "20150615", "20150708", "stock_shock"),
    PanicWindow("2015_stock_crash_aftershock", "20150824", "20150826", "stock_shock"),
    PanicWindow("2019_trade_war_shock", "20190506", "20190510", "stock_shock"),
    PanicWindow("2020_covid_reopen", "20200203", "20200207", "stock_shock"),
    PanicWindow("2020_global_covid_crash", "20200309", "20200323", "stock_shock"),
    PanicWindow("2022_april_a_share_breakdown", "20220425", "20220427", "stock_shock"),
    PanicWindow("2024_a_share_mini_crash", "20240122", "20240208", "stock_shock"),
    PanicWindow("2024_cb_credit_liquidity_panic", "20240621", "20240705", "cb_credit_liquidity"),
    PanicWindow("2025_tariff_shock", "20250407", "20250418", "stock_shock"),
]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _fmt_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _build_features(start: str, end: str) -> pd.DataFrame:
    cb = pd.read_parquet(_REPO_ROOT / "data" / "cb_warehouse" / "cb_daily.parquet")
    cb["trade_date"] = cb["trade_date"].astype(str)
    cb = cb[(cb["trade_date"] >= start) & (cb["trade_date"] <= end)].copy()
    cb = cb.sort_values(["ts_code", "trade_date"])

    cb["prev_close"] = cb.groupby("ts_code")["close"].shift(1)
    cb["bond_day_ret"] = cb["close"] / cb["prev_close"] - 1.0
    cb["ret20_bond"] = cb.groupby("ts_code")["close"].pct_change(20)
    cb["amount_proxy"] = cb["close"].astype(float) * cb["vol"].astype(float)
    cb["positive_20d"] = cb["ret20_bond"] > 0
    cb["negative_1d"] = cb["bond_day_ret"] < 0
    cb["deep_down_1d"] = cb["bond_day_ret"] <= -0.03

    by = cb.groupby("trade_date").agg(
        index_level=("close", "mean"),
        amount=("amount_proxy", "sum"),
        breadth1=("negative_1d", lambda s: 1.0 - float(s.mean())),
        breadth20=("positive_20d", "mean"),
        deep_down_share=("deep_down_1d", "mean"),
        n_bonds=("ts_code", "nunique"),
    ).sort_index()
    by["day_ret"] = by["index_level"].pct_change()
    by["ret5"] = by["index_level"].pct_change(5)
    by["ret20"] = by["index_level"].pct_change(20)
    by["ret60"] = by["index_level"].pct_change(60)
    by["high60"] = by["index_level"].shift(1).rolling(60, min_periods=20).max()
    by["high120"] = by["index_level"].shift(1).rolling(120, min_periods=40).max()
    by["dd60"] = by["index_level"] / by["high60"] - 1.0
    by["dd120"] = by["index_level"] / by["high120"] - 1.0
    by["amount_pctile252"] = by["amount"].rolling(252, min_periods=60).apply(
        lambda x: float((x <= x.iloc[-1]).mean()),
        raw=False,
    )
    return by.reset_index()


def _classify(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    out["shock_day"] = (
        (out["day_ret"] <= -0.025)
        | (
            (out["day_ret"] <= -0.018)
            & (
                (out["ret5"] <= -0.035)
                | (out["ret20"] <= -0.030)
                | (out["breadth1"] <= 0.25)
            )
        )
    )
    out["weak_state"] = (
        (out["dd120"] <= -0.055)
        & (out["ret20"] <= -0.050)
        & (out["breadth20"] <= 0.25)
    )
    out["cb_liquidity_panic"] = (
        (out["day_ret"] <= -0.012)
        & (out["amount_pctile252"] >= 0.85)
        & (out["ret20"] <= -0.040)
        & (out["breadth20"] <= 0.35)
    )
    out["panic_v1"] = out["shock_day"] | out["cb_liquidity_panic"]
    out["panic_or_weak_v1"] = out["panic_v1"] | out["weak_state"]
    active_left = 0
    active_age = 0
    panic_window = []
    for row in out.itertuples(index=False):
        triggered = bool(row.panic_v1)
        weak = bool(row.weak_state)
        if triggered:
            active_left = max(active_left, 6)
            active_age = 0
        is_active = active_left > 0
        panic_window.append(is_active)
        if is_active:
            active_age += 1
            active_left -= 1
            if weak and active_age <= 12:
                active_left = max(active_left, 2)
        else:
            active_age = 0
    out["panic_window_v1"] = panic_window
    reason = []
    for row in out.itertuples(index=False):
        flags = []
        if bool(row.shock_day):
            flags.append("shock_day")
        if bool(row.cb_liquidity_panic):
            flags.append("cb_liquidity_panic")
        if bool(row.weak_state):
            flags.append("weak_state")
        if bool(row.panic_window_v1) and not flags:
            flags.append("panic_window_continues")
        reason.append("+".join(flags) if flags else "normal")
    out["panic_reason"] = reason
    return out


def _label_known_windows(features: pd.DataFrame, windows: list[PanicWindow]) -> pd.DataFrame:
    out = features.copy()
    out["known_panic"] = False
    out["known_window"] = ""
    out["known_kind"] = ""
    for window in windows:
        mask = (out["trade_date"] >= window.start) & (out["trade_date"] <= window.end)
        out.loc[mask, "known_panic"] = True
        out.loc[mask, "known_window"] = out.loc[mask, "known_window"].map(
            lambda x: f"{x}|{window.name}" if x else window.name
        )
        out.loc[mask, "known_kind"] = out.loc[mask, "known_kind"].map(
            lambda x: f"{x}|{window.kind}" if x else window.kind
        )
    return out


def _score_rule(df: pd.DataFrame, rule: str) -> dict[str, Any]:
    predicted = df[rule].fillna(False).astype(bool)
    known = df["known_panic"].fillna(False).astype(bool)
    tp = int((predicted & known).sum())
    fp = int((predicted & ~known).sum())
    fn = int((~predicted & known).sum())
    predicted_n = int(predicted.sum())
    known_n = int(known.sum())
    return {
        "rule": rule,
        "known_days": known_n,
        "predicted_days": predicted_n,
        "hit_days": tp,
        "missed_known_days": fn,
        "outside_known_days": fp,
        "date_recall": _fmt_pct(tp / known_n if known_n else 0.0),
        "date_precision_labeled": _fmt_pct(tp / predicted_n if predicted_n else 0.0),
    }


def _score_events(df: pd.DataFrame, windows: list[PanicWindow], rule: str) -> list[dict[str, Any]]:
    rows = []
    for window in windows:
        sub = df[(df["trade_date"] >= window.start) & (df["trade_date"] <= window.end)]
        if sub.empty:
            continue
        hits = sub[sub[rule].fillna(False).astype(bool)]
        rows.append(
            {
                "window": window.name,
                "kind": window.kind,
                "start": window.start,
                "end": window.end,
                "trade_days": int(len(sub)),
                "hit": bool(len(hits) > 0),
                "hit_days": int(len(hits)),
                "first_hit": str(hits["trade_date"].iloc[0]) if len(hits) else "",
                "max_down_day": str(sub.loc[sub["day_ret"].idxmin(), "trade_date"]),
                "min_day_ret": _fmt_pct(float(sub["day_ret"].min())),
                "min_breadth1": _fmt_pct(float(sub["breadth1"].min())),
                "min_breadth20": _fmt_pct(float(sub["breadth20"].min())),
            }
        )
    return rows


def _detected_windows(df: pd.DataFrame, rule: str) -> list[dict[str, Any]]:
    rows = []
    current: dict[str, Any] | None = None
    for row in df.sort_values("trade_date").itertuples(index=False):
        detected = bool(getattr(row, rule))
        if detected and current is None:
            current = {
                "start": str(row.trade_date),
                "end": str(row.trade_date),
                "days": 1,
                "known_days": int(bool(row.known_panic)),
                "reasons": {str(row.panic_reason)},
                "min_day_ret": float(row.day_ret) if pd.notna(row.day_ret) else None,
            }
        elif detected and current is not None:
            current["end"] = str(row.trade_date)
            current["days"] += 1
            current["known_days"] += int(bool(row.known_panic))
            current["reasons"].add(str(row.panic_reason))
            if pd.notna(row.day_ret):
                cur_min = current["min_day_ret"]
                current["min_day_ret"] = float(row.day_ret) if cur_min is None else min(cur_min, float(row.day_ret))
        elif not detected and current is not None:
            current["reasons"] = "|".join(sorted(current["reasons"]))
            current["min_day_ret"] = _fmt_pct(current["min_day_ret"])
            rows.append(current)
            current = None
    if current is not None:
        current["reasons"] = "|".join(sorted(current["reasons"]))
        current["min_day_ret"] = _fmt_pct(current["min_day_ret"])
        rows.append(current)
    return rows


def _false_positive_months(df: pd.DataFrame, rule: str) -> list[dict[str, Any]]:
    predicted = df[rule].fillna(False).astype(bool)
    fp = df[predicted & ~df["known_panic"].fillna(False).astype(bool)].copy()
    if fp.empty:
        return []
    fp["month"] = fp["trade_date"].astype(str).str.slice(0, 6)
    rows = []
    for month, sub in fp.groupby("month"):
        rows.append(
            {
                "month": str(month),
                "outside_known_days": int(len(sub)),
                "first_date": str(sub["trade_date"].iloc[0]),
                "last_date": str(sub["trade_date"].iloc[-1]),
                "reasons": "|".join(sorted(set(sub["panic_reason"].astype(str)))),
                "min_day_ret": _fmt_pct(float(sub["day_ret"].min())),
                "min_ret20": _fmt_pct(float(sub["ret20"].min())),
                "min_breadth20": _fmt_pct(float(sub["breadth20"].min())),
            }
        )
    return sorted(rows, key=lambda r: (-int(r["outside_known_days"]), str(r["month"])))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="20190101")
    parser.add_argument("--end", default="20251231")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_concurrent_supervised_20260511_094500/panic_detector_v1"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    features = _build_features(args.start, args.end)
    classified = _label_known_windows(_classify(features), KNOWN_WINDOWS)
    rules = [
        "shock_day",
        "cb_liquidity_panic",
        "weak_state",
        "panic_v1",
        "panic_window_v1",
        "panic_or_weak_v1",
    ]

    daily_cols = [
        "trade_date",
        "index_level",
        "day_ret",
        "ret5",
        "ret20",
        "ret60",
        "dd60",
        "dd120",
        "breadth1",
        "breadth20",
        "deep_down_share",
        "amount_pctile252",
        "shock_day",
        "cb_liquidity_panic",
        "weak_state",
        "panic_v1",
        "panic_window_v1",
        "panic_or_weak_v1",
        "panic_reason",
        "known_panic",
        "known_window",
        "known_kind",
        "n_bonds",
    ]
    classified[daily_cols].to_csv(args.output_dir / "panic_daily_features.csv", index=False)

    summary_rows = [_score_rule(classified, rule) for rule in rules]
    for row in summary_rows:
        event_rows = _score_events(classified, KNOWN_WINDOWS, row["rule"])
        row["known_events"] = len(event_rows)
        row["hit_events"] = sum(1 for event in event_rows if event["hit"])
        row["event_recall"] = _fmt_pct(row["hit_events"] / row["known_events"] if row["known_events"] else 0.0)
    _write_csv(args.output_dir / "panic_detector_summary.csv", summary_rows)

    selected_rule = "panic_window_v1"
    event_rows = _score_events(classified, KNOWN_WINDOWS, selected_rule)
    detected_rows = _detected_windows(classified, selected_rule)
    false_month_rows = _false_positive_months(classified, selected_rule)
    _write_csv(args.output_dir / "panic_event_hits.csv", event_rows)
    _write_csv(args.output_dir / "panic_detected_windows.csv", detected_rows)
    _write_csv(args.output_dir / "panic_false_positive_months.csv", false_month_rows)

    summary = {
        "start": args.start,
        "end": args.end,
        "selected_rule": selected_rule,
        "known_windows": [window.__dict__ for window in KNOWN_WINDOWS],
        "rules": summary_rows,
        "selected_rule_events": event_rows,
        "selected_rule_top_false_positive_months": false_month_rows[:10],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
