"""Train threshold rules for a CB market panic-day detector."""

from __future__ import annotations

import argparse
import csv
import json
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analyze_cb_panic_detector import KNOWN_WINDOWS, _build_features


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


def _fmt(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _date_to_year(date: str) -> int:
    return int(str(date)[:4])


def _candidate_params() -> list[dict[str, float]]:
    rows = []
    for hard, soft, ret5, ret20, breadth1, breadth20 in product(
        [-0.018, -0.020, -0.022, -0.025, -0.030],
        [-0.010, -0.012, -0.015, -0.018],
        [-0.020, -0.030, -0.040],
        [-0.020, -0.030, -0.040, -0.050],
        [0.20, 0.25, 0.30, 0.40],
        [0.15, 0.20, 0.25, 0.35],
    ):
        if soft < hard:
            continue
        rows.append(
            {
                "hard_day_ret": hard,
                "soft_day_ret": soft,
                "ret5": ret5,
                "ret20": ret20,
                "breadth1": breadth1,
                "breadth20": breadth20,
            }
        )
    return rows


def _predict(features: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    hard = features["day_ret"] <= params["hard_day_ret"]
    soft = (features["day_ret"] <= params["soft_day_ret"]) & (
        (features["ret5"] <= params["ret5"])
        | (features["ret20"] <= params["ret20"])
        | (features["breadth1"] <= params["breadth1"])
        | (features["breadth20"] <= params["breadth20"])
    )
    return (hard | soft).fillna(False)


def _label(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    out["known_panic"] = False
    out["known_window"] = ""
    for window in KNOWN_WINDOWS:
        mask = (out["trade_date"] >= window.start) & (out["trade_date"] <= window.end)
        out.loc[mask, "known_panic"] = True
        out.loc[mask, "known_window"] = out.loc[mask, "known_window"].map(
            lambda x, name=window.name: f"{x}|{name}" if x else name
        )
    return out


def _active_windows(features: pd.DataFrame, start_year: int, end_year: int) -> list[Any]:
    return [
        window
        for window in KNOWN_WINDOWS
        if start_year <= _date_to_year(window.start) <= end_year
        and not features[(features["trade_date"] >= window.start) & (features["trade_date"] <= window.end)].empty
    ]


def _score(features: pd.DataFrame, pred: pd.Series, start_year: int, end_year: int) -> dict[str, Any]:
    sub = features[
        (features["trade_date"] >= f"{start_year}0101") & (features["trade_date"] <= f"{end_year}1231")
    ].copy()
    pred_sub = pred.loc[sub.index].astype(bool)
    known = sub["known_panic"].fillna(False).astype(bool)

    tp = int((pred_sub & known).sum())
    fp = int((pred_sub & ~known).sum())
    fn = int((~pred_sub & known).sum())
    predicted_n = int(pred_sub.sum())
    known_n = int(known.sum())

    event_hits = 0
    first_day_hits = 0
    lags: list[int] = []
    event_rows = []
    for window in _active_windows(features, start_year, end_year):
        w = sub[(sub["trade_date"] >= window.start) & (sub["trade_date"] <= window.end)]
        if w.empty:
            continue
        w_pred = pred.loc[w.index].astype(bool)
        hit_dates = w.loc[w_pred, "trade_date"].astype(str).tolist()
        hit = bool(hit_dates)
        if hit:
            event_hits += 1
            first_hit_idx = int(w.index.get_loc(w.loc[w_pred].index[0]))
            lags.append(first_hit_idx)
            if first_hit_idx == 0:
                first_day_hits += 1
        event_rows.append(
            {
                "window": window.name,
                "start": window.start,
                "end": window.end,
                "trade_days": int(len(w)),
                "hit": hit,
                "first_day_hit": bool(hit and lags[-1] == 0),
                "first_hit": hit_dates[0] if hit_dates else "",
                "hit_days": int(w_pred.sum()),
            }
        )

    years = max(1, end_year - start_year + 1)
    event_n = len(event_rows)
    event_recall = event_hits / event_n if event_n else 0.0
    date_recall = tp / known_n if known_n else 0.0
    precision = tp / predicted_n if predicted_n else 0.0
    first_day_recall = first_day_hits / event_n if event_n else 0.0
    alarm_days_per_year = predicted_n / years
    avg_lag = sum(lags) / len(lags) if lags else None

    score = (
        event_recall * 100.0
        + first_day_recall * 40.0
        + date_recall * 20.0
        + precision * 5.0
        - alarm_days_per_year * 0.35
        - (avg_lag or 0.0) * 2.0
    )
    return {
        "score": _fmt(score),
        "known_days": known_n,
        "predicted_days": predicted_n,
        "hit_days": tp,
        "missed_known_days": fn,
        "outside_known_days": fp,
        "date_recall": _fmt(date_recall),
        "precision_labeled": _fmt(precision),
        "known_events": event_n,
        "hit_events": event_hits,
        "event_recall": _fmt(event_recall),
        "first_day_hits": first_day_hits,
        "first_day_recall": _fmt(first_day_recall),
        "avg_first_hit_lag_days": _fmt(avg_lag),
        "alarm_days_per_year": _fmt(alarm_days_per_year),
        "event_rows": event_rows,
    }


def _score_fast(
    dates: list[str],
    known: np.ndarray,
    pred: np.ndarray,
    event_indices: list[dict[str, Any]],
    start_year: int,
    end_year: int,
) -> dict[str, Any]:
    period = np.array([(f"{start_year}0101" <= d <= f"{end_year}1231") for d in dates], dtype=bool)
    pred_sub = pred & period
    known_sub = known & period

    tp = int((pred_sub & known_sub).sum())
    fp = int((pred_sub & ~known_sub).sum())
    fn = int((~pred_sub & known_sub).sum())
    predicted_n = int(pred_sub.sum())
    known_n = int(known_sub.sum())

    event_rows = []
    event_hits = 0
    first_day_hits = 0
    lags: list[int] = []
    for event in event_indices:
        if not (start_year <= _date_to_year(event["start"]) <= end_year):
            continue
        idx = event["idx"]
        if len(idx) == 0:
            continue
        hits = pred[idx]
        hit_positions = np.flatnonzero(hits)
        hit = len(hit_positions) > 0
        first_hit = ""
        first_day_hit = False
        if hit:
            event_hits += 1
            lag = int(hit_positions[0])
            lags.append(lag)
            first_day_hit = lag == 0
            first_day_hits += int(first_day_hit)
            first_hit = dates[int(idx[lag])]
        event_rows.append(
            {
                "window": event["name"],
                "start": event["start"],
                "end": event["end"],
                "trade_days": int(len(idx)),
                "hit": hit,
                "first_day_hit": first_day_hit,
                "first_hit": first_hit,
                "hit_days": int(hits.sum()),
            }
        )

    years = max(1, end_year - start_year + 1)
    event_n = len(event_rows)
    event_recall = event_hits / event_n if event_n else 0.0
    date_recall = tp / known_n if known_n else 0.0
    precision = tp / predicted_n if predicted_n else 0.0
    first_day_recall = first_day_hits / event_n if event_n else 0.0
    alarm_days_per_year = predicted_n / years
    avg_lag = sum(lags) / len(lags) if lags else None
    score = (
        event_recall * 100.0
        + first_day_recall * 40.0
        + date_recall * 20.0
        + precision * 5.0
        - alarm_days_per_year * 0.35
        - (avg_lag or 0.0) * 2.0
    )
    return {
        "score": _fmt(score),
        "known_days": known_n,
        "predicted_days": predicted_n,
        "hit_days": tp,
        "missed_known_days": fn,
        "outside_known_days": fp,
        "date_recall": _fmt(date_recall),
        "precision_labeled": _fmt(precision),
        "known_events": event_n,
        "hit_events": event_hits,
        "event_recall": _fmt(event_recall),
        "first_day_hits": first_day_hits,
        "first_day_recall": _fmt(first_day_recall),
        "avg_first_hit_lag_days": _fmt(avg_lag),
        "alarm_days_per_year": _fmt(alarm_days_per_year),
        "event_rows": event_rows,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="20150101")
    parser.add_argument("--end", default="20251231")
    parser.add_argument("--train-start-year", type=int, default=2015)
    parser.add_argument("--train-end-year", type=int, default=2022)
    parser.add_argument("--test-start-year", type=int, default=2024)
    parser.add_argument("--test-end-year", type=int, default=2025)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cb_arb_concurrent_supervised_20260511_094500/panic_detector_training"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    features = _label(_build_features(args.start, args.end))
    dates = features["trade_date"].astype(str).tolist()
    known = features["known_panic"].fillna(False).to_numpy(dtype=bool)
    event_indices = []
    for window in KNOWN_WINDOWS:
        idx = features.index[(features["trade_date"] >= window.start) & (features["trade_date"] <= window.end)].to_numpy()
        if len(idx) > 0:
            event_indices.append(
                {
                    "name": window.name,
                    "start": window.start,
                    "end": window.end,
                    "idx": idx,
                }
            )

    scored = []
    for params in _candidate_params():
        pred = _predict(features, params).to_numpy(dtype=bool)
        train = _score_fast(
            dates,
            known,
            pred,
            event_indices,
            args.train_start_year,
            args.train_end_year,
        )
        test = _score_fast(
            dates,
            known,
            pred,
            event_indices,
            args.test_start_year,
            args.test_end_year,
        )
        scored.append(
            {
                **params,
                "train_score": train["score"],
                "train_event_recall": train["event_recall"],
                "train_first_day_recall": train["first_day_recall"],
                "train_date_recall": train["date_recall"],
                "train_precision_labeled": train["precision_labeled"],
                "train_alarm_days_per_year": train["alarm_days_per_year"],
                "test_event_recall": test["event_recall"],
                "test_first_day_recall": test["first_day_recall"],
                "test_date_recall": test["date_recall"],
                "test_precision_labeled": test["precision_labeled"],
                "test_alarm_days_per_year": test["alarm_days_per_year"],
                "_train": train,
                "_test": test,
            }
        )
    scored = sorted(
        scored,
        key=lambda r: (
            float(r["train_event_recall"]),
            float(r["train_first_day_recall"]),
            float(r["train_date_recall"]),
            -float(r["train_alarm_days_per_year"]),
            float(r["test_event_recall"]),
        ),
        reverse=True,
    )
    top_rows = [{k: v for k, v in row.items() if not k.startswith("_")} for row in scored[:50]]
    _write_csv(args.output_dir / "panic_detector_grid_top50.csv", top_rows)

    best = scored[0]
    best_params = {k: best[k] for k in ["hard_day_ret", "soft_day_ret", "ret5", "ret20", "breadth1", "breadth20"]}
    pred = _predict(features, best_params)
    daily = features.copy()
    daily["panic_day_trained"] = pred
    daily.to_csv(args.output_dir / "panic_detector_trained_daily.csv", index=False)
    _write_csv(args.output_dir / "panic_detector_train_events.csv", best["_train"]["event_rows"])
    _write_csv(args.output_dir / "panic_detector_test_events.csv", best["_test"]["event_rows"])

    summary = {
        "train_years": [args.train_start_year, args.train_end_year],
        "test_years": [args.test_start_year, args.test_end_year],
        "best_params": best_params,
        "train": {k: v for k, v in best["_train"].items() if k != "event_rows"},
        "test": {k: v for k, v in best["_test"].items() if k != "event_rows"},
        "outputs": [
            str(args.output_dir / "panic_detector_grid_top50.csv"),
            str(args.output_dir / "panic_detector_trained_daily.csv"),
            str(args.output_dir / "panic_detector_train_events.csv"),
            str(args.output_dir / "panic_detector_test_events.csv"),
        ],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
