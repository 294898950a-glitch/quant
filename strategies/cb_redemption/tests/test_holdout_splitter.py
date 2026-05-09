"""Tests for calendar right-tail holdout splitting."""

from __future__ import annotations

import pytest

from strategies.cb_redemption.holdout_splitter import split_holdout


def test_explicit_cb_arb_calendar_split() -> None:
    split = split_holdout(
        "2022-01-01",
        "2026-05-08",
        holdout_start="2025-07-01",
    )

    assert split.train.start == "2022-01-01"
    assert split.train.end == "2025-06-30"
    assert split.holdout.start == "2025-07-01"
    assert split.holdout.end == "2026-05-08"


def test_default_right_tail_uses_requested_months() -> None:
    split = split_holdout("2024-01-01", "2024-12-31", holdout_months=3)

    assert split.train.end == "2024-09-30"
    assert split.holdout.start == "2024-10-01"
    assert split.holdout.end == "2024-12-31"


def test_split_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="start must be before end"):
        split_holdout("2024-01-01", "2024-01-01")
    with pytest.raises(ValueError, match="holdout_start must be after start"):
        split_holdout("2024-01-01", "2024-12-31", holdout_start="2023-12-31")
    with pytest.raises(ValueError, match="holdout_start must not be after end"):
        split_holdout("2024-01-01", "2024-12-31", holdout_start="2025-01-01")
