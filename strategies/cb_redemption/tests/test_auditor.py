"""Unit tests for the cross-run auditor.

All tests construct synthetic ``runs.jsonl`` (and optional
``sealed_pools.json``) under ``tmp_path`` — they MUST NOT touch any real
state under ``data/cb_redemption/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategies.cb_redemption.auditor import AuditReport, audit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_run(iteration: int, is_sharpe: float, oos_sharpe: float) -> dict:
    """Synthesise a minimal RunRecord-shaped dict."""
    return {
        "run_id": f"run-{iteration:04d}",
        "iteration": iteration,
        "timestamp_iso": f"2026-05-07T00:00:{iteration:02d}Z",
        "phase": "inner",
        "params": {"weights": [], "thresholds": {}},
        "backtest": {
            "is_metrics": {
                "trades": 50,
                "winrate": 0.55,
                "sharpe": is_sharpe,
                "avg_return": 0.01,
                "pnl": 100.0,
            },
            "oos_metrics": {
                "trades": 25,
                "winrate": 0.55,
                "sharpe": oos_sharpe,
                "avg_return": 0.01,
                "pnl": 50.0,
            },
            "all_metrics": {
                "trades": 75,
                "winrate": 0.55,
                "sharpe": (is_sharpe + oos_sharpe) / 2,
                "avg_return": 0.01,
                "pnl": 150.0,
            },
            "date_range": ["2024-01-01", "2025-12-31"],
        },
        "diagnosis": None,
        "hypothesis_attempt": None,
        "audit": None,
        "git_commit": None,
    }


def _write_runs(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _write_pool_file(path: Path, *, has_read: bool) -> None:
    """Create a tiny sealed_pools.json. ``has_read`` controls
    whether one pool has been touched.
    """
    pools = [
        {
            "id": 0,
            "event_ids": ["evt_a", "evt_b"],
            "read_count": 1 if has_read else 0,
            "first_read_at": "2026-05-06T00:00:00Z" if has_read else None,
            "sealed_at": None,
        },
        {
            "id": 1,
            "event_ids": ["evt_c", "evt_d"],
            "read_count": 0,
            "first_read_at": None,
            "sealed_at": None,
        },
    ]
    data = {
        "version": 1,
        "strategy": "cb_redemption",
        "split_at": "2025-01-01",
        "n_pools": 2,
        "seed": 42,
        "event_id_col": "bond_id_meeting_date",
        "created_at": "2026-05-01T00:00:00Z",
        "pools": pools,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def runs_path(tmp_path):
    return tmp_path / "runs.jsonl"


@pytest.fixture
def pool_path_ok(tmp_path):
    """A pool file with at least one read pool — compliance OK."""
    p = tmp_path / "sealed_pools.json"
    _write_pool_file(p, has_read=True)
    return p


@pytest.fixture
def pool_path_missing(tmp_path):
    """A path that does NOT exist."""
    return tmp_path / "no_such_pools.json"


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_empty_runs_returns_healthy_no_veto(runs_path, pool_path_ok):
    """No runs.jsonl on disk → cold start, healthy, no veto."""
    report = audit(runs_path, pool_path_ok, window=10)

    assert isinstance(report, AuditReport)
    assert report.verdict == "healthy"
    assert report.veto is False
    assert report.veto_reason is None
    assert report.evidence == {}
    assert report.window == 0
    assert "Cold start" in report.text


def test_fewer_than_three_runs_is_cold_start(runs_path, pool_path_ok):
    """< 3 runs → cold start, no opinion regardless of pattern."""
    runs = [
        _make_run(1, is_sharpe=0.2, oos_sharpe=0.2),
        _make_run(2, is_sharpe=2.0, oos_sharpe=-1.0),  # would look like data mining
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_ok, window=10)

    assert report.verdict == "healthy"
    assert report.veto is False
    assert report.evidence == {}
    assert report.window == 2


def test_continuously_rising_oos_is_healthy(runs_path, pool_path_ok):
    """OOS rising + gap shrinking + holdout OK → healthy, no veto."""
    runs = [
        _make_run(1, is_sharpe=1.5, oos_sharpe=0.5),  # gap=1.0
        _make_run(2, is_sharpe=1.4, oos_sharpe=0.7),  # gap=0.7
        _make_run(3, is_sharpe=1.3, oos_sharpe=0.9),  # gap=0.4
        _make_run(4, is_sharpe=1.25, oos_sharpe=1.1),  # gap=0.15
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_ok, window=10)

    assert report.verdict == "healthy"
    assert report.veto is False
    assert report.veto_reason is None
    assert report.evidence["holdout_compliance"] is True
    assert report.evidence["oos_improvement"] == pytest.approx(0.6, abs=1e-9)
    assert report.evidence["rolling_window_stability"] is None
    assert report.evidence["oos_sharpe_trend"] == [0.5, 0.7, 0.9, 1.1]
    assert report.iteration == 4


def test_is_up_oos_flat_gap_widening_is_data_mining_with_veto(
    runs_path, pool_path_ok
):
    """Classic over-fit signature → data_mining, veto=True."""
    runs = [
        _make_run(1, is_sharpe=1.0, oos_sharpe=0.5),  # gap=0.5
        _make_run(2, is_sharpe=1.4, oos_sharpe=0.5),  # gap=0.9
        _make_run(3, is_sharpe=1.8, oos_sharpe=0.5),  # gap=1.3
        _make_run(4, is_sharpe=2.2, oos_sharpe=0.5),  # gap=1.7
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_ok, window=10)

    assert report.verdict == "data_mining"
    assert report.veto is True
    assert "data_mining" in (report.veto_reason or "")
    assert report.evidence["holdout_compliance"] is True


def test_three_consecutive_big_drops_is_diverging_with_veto(
    runs_path, pool_path_ok
):
    """Three consecutive > 0.1 drops in oos_sharpe → diverging + veto."""
    runs = [
        _make_run(1, is_sharpe=1.0, oos_sharpe=1.0),
        _make_run(2, is_sharpe=1.0, oos_sharpe=1.2),  # baseline before drops
        _make_run(3, is_sharpe=1.0, oos_sharpe=1.0),  # -0.2
        _make_run(4, is_sharpe=1.0, oos_sharpe=0.8),  # -0.2
        _make_run(5, is_sharpe=1.0, oos_sharpe=0.6),  # -0.2
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_ok, window=10)

    assert report.verdict == "diverging"
    assert report.veto is True
    assert "diverging" in (report.veto_reason or "")


def test_flat_recent_runs_is_stagnant_no_veto(runs_path, pool_path_ok):
    """oos_sharpe nearly identical for last RECENT_N runs → stagnant."""
    runs = [
        _make_run(1, is_sharpe=0.9, oos_sharpe=0.5),
        _make_run(2, is_sharpe=0.91, oos_sharpe=0.601),  # gap=0.309
        _make_run(3, is_sharpe=0.91, oos_sharpe=0.602),  # gap=0.308
        _make_run(4, is_sharpe=0.91, oos_sharpe=0.603),  # gap=0.307
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_ok, window=10)

    assert report.verdict == "stagnant"
    assert report.veto is False
    assert report.veto_reason is None


def test_holdout_pool_missing_triggers_veto(runs_path, pool_path_missing):
    """Pool file absent → holdout_compliance=False, veto=True regardless
    of how nice the trajectory looks.
    """
    runs = [
        _make_run(1, is_sharpe=1.5, oos_sharpe=0.5),
        _make_run(2, is_sharpe=1.4, oos_sharpe=0.7),
        _make_run(3, is_sharpe=1.3, oos_sharpe=0.9),
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_missing, window=10)

    assert report.evidence["holdout_compliance"] is False
    assert report.veto is True
    assert "holdout_compliance=False" in (report.veto_reason or "")


def test_holdout_pool_exists_but_unread_triggers_veto(runs_path, tmp_path):
    """Pool file exists but no pool has been read → still non-compliant."""
    pool_path = tmp_path / "sealed_pools.json"
    _write_pool_file(pool_path, has_read=False)
    runs = [
        _make_run(1, is_sharpe=1.5, oos_sharpe=0.5),
        _make_run(2, is_sharpe=1.4, oos_sharpe=0.7),
        _make_run(3, is_sharpe=1.3, oos_sharpe=0.9),
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path, window=10)

    assert report.evidence["holdout_compliance"] is False
    assert report.veto is True


def test_to_dict_is_json_serialisable(runs_path, pool_path_ok):
    """AuditReport.to_dict() must round-trip through json.dumps."""
    runs = [
        _make_run(1, is_sharpe=1.5, oos_sharpe=0.5),
        _make_run(2, is_sharpe=1.4, oos_sharpe=0.7),
        _make_run(3, is_sharpe=1.3, oos_sharpe=0.9),
    ]
    _write_runs(runs_path, runs)

    report = audit(runs_path, pool_path_ok, window=10)
    d = report.to_dict()

    assert set(d) == {
        "verdict",
        "iteration",
        "window",
        "evidence",
        "veto",
        "veto_reason",
        "text",
    }
    # Must serialise cleanly — proves no datetime/Path/numpy leaked in.
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["verdict"] == report.verdict
    assert decoded["veto"] == report.veto
    assert decoded["evidence"]["holdout_compliance"] is True
