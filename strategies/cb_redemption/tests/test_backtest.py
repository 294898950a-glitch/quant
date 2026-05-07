"""Tests for ``backtest.run_backtest_core`` — focuses on the
``oos_event_ids`` holdout-pool integration path.

These tests use a hand-built tiny snapshot DataFrame so we don't depend
on the parquet warehouse. The bond_id ↔ ts_code mapping is exercised by
patching ``DEFAULT_CB_BASIC_PATH`` to a tmp parquet so the resolution
helper has deterministic input.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from strategies.cb_redemption import backtest as bt_mod
from strategies.cb_redemption.backtest import (
    BacktestConfig,
    BacktestResult,
    _resolve_pool_to_ts_codes,
    run_backtest_core,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _build_snapshots(
    *,
    is_codes: list[str],
    oos_codes: list[str],
    n_days_per_phase: int = 30,
) -> pd.DataFrame:
    """Build a synthetic snapshots DataFrame with deterministic price drift.

    Each ts_code appears once per day, with score-friendly factor values
    so signal_rank is high enough to enter a position. Closes start at
    100 and step up by 1 per day so positions naturally hit
    ``target_exit_pct=10%``.

    IS phase: dates 2024-12-02 ... + ``n_days_per_phase``  → entry_date in IS.
    OOS phase: dates 2025-01-02 ... + ``n_days_per_phase`` → entry_date in OOS.
    """
    rows: list[dict] = []

    def _phase_block(start_date: str, codes: list[str]) -> None:
        dates = pd.date_range(start_date, periods=n_days_per_phase, freq="B")
        for i, d in enumerate(dates):
            d_str = d.strftime("%Y%m%d")
            for code in codes:
                rows.append(
                    {
                        "date": d_str,
                        "ts_code": code,
                        "bond_short_name": code,
                        "close": 100.0 + i,  # +1/day → hits +10% in 10 trading days
                        "premium_ratio": 5.0,
                        "redeem_progress": 0.8,
                        "remaining_size": 1.0,
                        "stock_momentum": 1.0,
                        "market_sentiment": 1.0,
                    }
                )

    _phase_block("2024-12-02", is_codes)
    _phase_block("2025-01-02", oos_codes)
    return pd.DataFrame(rows)


def _basic_cfg() -> BacktestConfig:
    return BacktestConfig(
        hold_max_days=15,
        target_exit_pct=10.0,
        stop_loss_pct=-8.0,
        max_positions=10,
        top_k=10,
        alert_threshold=0.5,
        min_close=50.0,
        max_close=500.0,
        max_premium_ratio=50.0,
    )


def _patch_cb_basic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> None:
    """Write a tiny cb_basic.parquet at tmp and patch DEFAULT_CB_BASIC_PATH."""
    df = pd.DataFrame({"ts_code": codes})
    p = tmp_path / "cb_basic.parquet"
    df.to_parquet(p)
    monkeypatch.setattr(bt_mod, "DEFAULT_CB_BASIC_PATH", p)


# --------------------------------------------------------------------------- #
# 1. Default behavior (oos_event_ids=None) is preserved
# --------------------------------------------------------------------------- #


def test_oos_event_ids_none_keeps_old_behavior() -> None:
    """Not passing oos_event_ids must produce identical OOS metrics."""
    weights = [1.0, 0.0, 0.0, 0.0, 0.0]
    snaps = _build_snapshots(
        is_codes=["110001.SH", "110002.SH"],
        oos_codes=["110010.SH", "123010.SZ"],
    )
    cfg = _basic_cfg()

    base = run_backtest_core(snaps, weights, {"alert": 0.5}, cfg)
    explicit = run_backtest_core(snaps, weights, {"alert": 0.5}, cfg, oos_event_ids=None)

    assert base.oos_metrics == explicit.oos_metrics
    assert base.is_metrics == explicit.is_metrics
    assert base.all_metrics == explicit.all_metrics
    # Must have at least one OOS trade so the comparison is meaningful.
    assert base.oos_metrics["total_trades"] > 0


# --------------------------------------------------------------------------- #
# 2. oos_event_ids actually filters OOS trades; IS metrics unchanged
# --------------------------------------------------------------------------- #


def test_oos_event_ids_filters_oos_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pool subset reduces oos_metrics; is_metrics and all_metrics stay put."""
    is_codes = ["110001.SH", "110002.SH"]
    oos_codes = ["110010.SH", "110011.SH", "123010.SZ"]

    # cb_basic must contain *every* code we care about so the bond_id
    # resolver can map back from event_ids.
    _patch_cb_basic(tmp_path, monkeypatch, is_codes + oos_codes)

    weights = [1.0, 0.0, 0.0, 0.0, 0.0]
    snaps = _build_snapshots(is_codes=is_codes, oos_codes=oos_codes)
    cfg = _basic_cfg()

    full = run_backtest_core(snaps, weights, {"alert": 0.5}, cfg)
    # Construct an event_id pool that names only one of the OOS codes:
    # 110010 (event_id is ``"<bond_id>_<meeting_date>"``).
    pool = {"110010_2025-03-15"}
    filtered = run_backtest_core(
        snaps, weights, {"alert": 0.5}, cfg, oos_event_ids=pool
    )

    # IS + all are byte-for-byte identical (same trades).
    assert filtered.is_metrics == full.is_metrics
    assert filtered.all_metrics == full.all_metrics
    # OOS has fewer trades — only 110010.SH survived the filter.
    assert filtered.oos_metrics["total_trades"] < full.oos_metrics["total_trades"]
    assert filtered.oos_metrics["total_trades"] >= 1


# --------------------------------------------------------------------------- #
# 3. Empty pool yields zero OOS trades
# --------------------------------------------------------------------------- #


def test_oos_event_ids_empty_set_yields_zero_oos_trades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing an empty set ⇒ no OOS trade survives the pool filter."""
    is_codes = ["110001.SH"]
    oos_codes = ["110010.SH", "110011.SH"]
    _patch_cb_basic(tmp_path, monkeypatch, is_codes + oos_codes)

    weights = [1.0, 0.0, 0.0, 0.0, 0.0]
    snaps = _build_snapshots(is_codes=is_codes, oos_codes=oos_codes)
    cfg = _basic_cfg()

    result = run_backtest_core(
        snaps, weights, {"alert": 0.5}, cfg, oos_event_ids=set()
    )
    assert result.oos_metrics["total_trades"] == 0
    # IS untouched by the filter.
    assert result.is_metrics["total_trades"] >= 1


# --------------------------------------------------------------------------- #
# 4. bond_id ↔ ts_code resolver: parquet path
# --------------------------------------------------------------------------- #


def test_resolve_pool_uses_cb_basic_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver maps 6-digit bond_ids to the matching ts_code in the parquet."""
    parquet = tmp_path / "cb_basic.parquet"
    pd.DataFrame(
        {"ts_code": ["110010.SH", "123010.SZ", "110099.SH"]}
    ).to_parquet(parquet)

    out = _resolve_pool_to_ts_codes(
        {"110010_2025-03-15", "123010_2025-04-20"},
        cb_basic_path=parquet,
    )
    assert out == {"110010.SH", "123010.SZ"}


# --------------------------------------------------------------------------- #
# 5. Resolver fallback: missing parquet → heuristic bond_id → ts_code
# --------------------------------------------------------------------------- #


def test_resolve_pool_fallback_heuristic(tmp_path: Path) -> None:
    """If cb_basic.parquet is absent the resolver falls back to a SH/SZ heuristic."""
    bogus = tmp_path / "does_not_exist.parquet"
    out = _resolve_pool_to_ts_codes(
        {"110010_2025-03-15", "123010_2025-04-20", "127001_2025-05-05"},
        cb_basic_path=bogus,
    )
    # 11x → SH; everything else → SZ in the fallback.
    assert out == {"110010.SH", "123010.SZ", "127001.SZ"}


# --------------------------------------------------------------------------- #
# 6. Empty input is harmless
# --------------------------------------------------------------------------- #


def test_resolve_pool_empty_input_returns_empty_set(tmp_path: Path) -> None:
    assert _resolve_pool_to_ts_codes(set()) == set()
    assert _resolve_pool_to_ts_codes(["", "  "]) == set()
