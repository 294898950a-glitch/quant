"""Tests for the shared strategy evaluator."""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.cb_redemption.evaluator import (
    EvaluationConfig,
    determine_tier,
    evaluate,
)


def _returns(value: float, n: int = 260, start: str = "2024-01-01") -> pd.Series:
    return pd.Series(value, index=pd.bdate_range(start, periods=n))


def _benchmarks(n: int = 260) -> dict[str, pd.Series]:
    return {
        "cash": _returns(0.00005, n),
        "cb_equal": _returns(0.00010, n),
        "csi300": _returns(0.00008, n),
        "dividend": _returns(0.00018, n),
        "sixty_forty": _returns(0.00007, n),
    }


def test_determine_tier_covers_four_tiers() -> None:
    baseline = {
        "beats_cash": True,
        "beats_primary": True,
        "drawdown_ok": True,
        "yearly_consistency_ok": True,
        "beats_stretch": False,
        "information_ratio_ok": False,
        "no_year_worse_than_primary": False,
    }
    assert determine_tier(baseline) == "底线档"

    stretch = baseline | {"beats_stretch": True}
    assert determine_tier(stretch) == "值得真上"

    good = stretch | {
        "information_ratio_ok": True,
        "no_year_worse_than_primary": True,
    }
    assert determine_tier(good) == "明确好"

    assert determine_tier(good | {"beats_primary": False}) == "不上"


def test_evaluate_builds_cumulative_curves_starting_at_one() -> None:
    result = evaluate(_returns(0.00025), _benchmarks())

    assert list(result.cumulative_curves.columns) == [
        "strategy",
        "cash",
        "cb_equal",
        "csi300",
        "dividend",
        "sixty_forty",
    ]
    assert result.cumulative_curves.iloc[0].to_dict() == {
        "strategy": 1.0,
        "cash": 1.0,
        "cb_equal": 1.0,
        "csi300": 1.0,
        "dividend": 1.0,
        "sixty_forty": 1.0,
    }


def test_evaluate_marks_clear_winner_as_best_tier() -> None:
    result = evaluate(_returns(0.00035), _benchmarks())

    assert result.tier == "明确好"
    assert result.thresholds["beats_cash"] is True
    assert result.thresholds["beats_primary"] is True
    assert result.thresholds["beats_stretch"] is True
    assert result.thresholds["information_ratio_ok"] is True
    assert result.thresholds["no_year_worse_than_primary"] is True


def test_evaluate_marks_underperformer_as_rejected() -> None:
    result = evaluate(_returns(0.00002), _benchmarks())

    assert result.tier == "不上"
    assert result.thresholds["beats_cash"] is False
    assert result.thresholds["beats_primary"] is False


def test_evaluate_requires_all_configured_benchmarks() -> None:
    benchmarks = _benchmarks()
    benchmarks.pop("dividend")

    with pytest.raises(ValueError, match="missing benchmark"):
        evaluate(_returns(0.0002), benchmarks)
