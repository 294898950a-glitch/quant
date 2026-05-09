"""Calendar train/holdout splitter for evaluation.

This is separate from sealed OOS pools. The sealed pools protect optimizer
iteration, while this splitter reserves a right-tail calendar segment for final
evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class DateRange:
    """Inclusive calendar date range."""

    start: str
    end: str


@dataclass(frozen=True)
class HoldoutSplit:
    """Training range plus strict right-tail holdout range."""

    train: DateRange
    holdout: DateRange


def split_holdout(
    start: str,
    end: str,
    *,
    holdout_months: int = 10,
    holdout_start: str | None = None,
) -> HoldoutSplit:
    """Split ``start``~``end`` into train and right-tail holdout ranges.

    If ``holdout_start`` is given, it is the first holdout date. Otherwise the
    first holdout date is computed as ``end - holdout_months + 1 day``. All
    returned dates are ISO ``YYYY-MM-DD`` strings.
    """

    start_ts = _date(start)
    end_ts = _date(end)
    if start_ts >= end_ts:
        raise ValueError("start must be before end")
    if holdout_months <= 0:
        raise ValueError("holdout_months must be positive")

    if holdout_start is None:
        holdout_start_ts = end_ts - pd.DateOffset(months=holdout_months) + pd.Timedelta(days=1)
    else:
        holdout_start_ts = _date(holdout_start)

    if holdout_start_ts <= start_ts:
        raise ValueError("holdout_start must be after start")
    if holdout_start_ts > end_ts:
        raise ValueError("holdout_start must not be after end")

    train_end = holdout_start_ts - pd.Timedelta(days=1)
    return HoldoutSplit(
        train=DateRange(_fmt(start_ts), _fmt(train_end)),
        holdout=DateRange(_fmt(holdout_start_ts), _fmt(end_ts)),
    )


def _date(value: str) -> pd.Timestamp:
    return pd.to_datetime(value).normalize()


def _fmt(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%d")


__all__ = ["DateRange", "HoldoutSplit", "split_holdout"]
