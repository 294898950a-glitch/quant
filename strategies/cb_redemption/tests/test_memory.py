"""Unit tests for the self-loop memory layer.

All tests use ``tmp_path`` for isolation — they MUST NOT touch
``data/cb_redemption/runs.jsonl`` or ``tried_directions.jsonl``.
"""

from __future__ import annotations

import uuid

import pytest

from strategies.cb_redemption.memory import (
    AttemptKey,
    RunRecord,
    append_run,
    has_been_tried,
    latest_iteration,
    read_runs,
    record_attempt,
    search_history,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def runs_path(tmp_path):
    return tmp_path / "runs.jsonl"


@pytest.fixture
def attempts_path(tmp_path):
    return tmp_path / "tried_directions.jsonl"


def _make_record(iteration: int, **overrides) -> RunRecord:
    base = dict(
        run_id=str(uuid.uuid4()),
        iteration=iteration,
        timestamp_iso=f"2026-05-07T00:00:{iteration:02d}Z",
        phase="inner",
        params={"weights": [0.1, 0.2], "thresholds": {"redeem": 0.5}},
        backtest={"sharpe": 1.23, "n_events": 51},
        diagnosis=None,
        hypothesis_attempt=None,
        audit=None,
        git_commit=None,
    )
    base.update(overrides)
    return RunRecord(**base)


# --------------------------------------------------------------------------- #
# RunRecord — append / read
# --------------------------------------------------------------------------- #


def test_append_run_then_read_runs_roundtrip(runs_path):
    rec = _make_record(
        1,
        diagnosis={"verdict": "improved"},
        hypothesis_attempt={
            "what_changed": "increase w_redeem_progress",
            "expected_direction": "up",
            "actual_outcome": "up",
            "verdict": "confirmed",
        },
        audit={"verifier_score": 7},
        git_commit="abc1234",
    )
    append_run(rec, path=runs_path)

    loaded = read_runs(path=runs_path)
    assert len(loaded) == 1
    got = loaded[0]
    assert got.run_id == rec.run_id
    assert got.iteration == 1
    assert got.phase == "inner"
    assert got.params == rec.params
    assert got.backtest == {"sharpe": 1.23, "n_events": 51}
    assert got.diagnosis == {"verdict": "improved"}
    assert got.hypothesis_attempt["verdict"] == "confirmed"
    assert got.audit == {"verifier_score": 7}
    assert got.git_commit == "abc1234"


def test_read_runs_last_n(runs_path):
    for i in range(1, 6):
        append_run(_make_record(i), path=runs_path)
    last_two = read_runs(path=runs_path, last_n=2)
    assert [r.iteration for r in last_two] == [4, 5]


def test_read_runs_missing_file_returns_empty(runs_path):
    assert read_runs(path=runs_path) == []


# --------------------------------------------------------------------------- #
# latest_iteration
# --------------------------------------------------------------------------- #


def test_latest_iteration_empty_file_is_zero(runs_path):
    assert latest_iteration(path=runs_path) == 0


def test_latest_iteration_picks_max_across_rows(runs_path):
    # Append out of order to make sure we take max, not last.
    for i in [3, 1, 7, 2, 5]:
        append_run(_make_record(i), path=runs_path)
    assert latest_iteration(path=runs_path) == 7


# --------------------------------------------------------------------------- #
# AttemptKey
# --------------------------------------------------------------------------- #


def test_attempt_key_to_str_is_stable():
    k1 = AttemptKey(
        item_path="parameters.w_redeem_progress",
        direction="increase",
        bucket_value="0.50",
    )
    k2 = AttemptKey(
        item_path="parameters.w_redeem_progress",
        direction="increase",
        bucket_value="0.50",
    )
    assert k1.to_str() == k2.to_str()
    assert k1.to_str() == "parameters.w_redeem_progress|increase|0.50"


def test_attempt_key_bucket_quantises_floats():
    # 0.501 and 0.502 round to 0.50 — same bucket.
    k1 = AttemptKey.from_value(
        "parameters.w_redeem_progress", "set", 0.501
    )
    k2 = AttemptKey.from_value(
        "parameters.w_redeem_progress", "set", 0.502
    )
    assert k1.bucket_value == k2.bucket_value == "0.50"
    assert k1.to_str() == k2.to_str()

    # But 0.55 rounds to 0.55 — different bucket.
    k3 = AttemptKey.from_value(
        "parameters.w_redeem_progress", "set", 0.55
    )
    assert k3.bucket_value == "0.55"
    assert k3.to_str() != k1.to_str()


def test_attempt_key_bucket_handles_int_bool_str():
    assert AttemptKey.from_value("rules.holding_days", "set", 30).bucket_value == "30"
    assert (
        AttemptKey.from_value("rules.use_stop_loss", "set", True).bucket_value
        == "True"
    )
    assert (
        AttemptKey.from_value("factors.signal", "set", "deep_redeem").bucket_value
        == "deep_redeem"
    )


# --------------------------------------------------------------------------- #
# record_attempt / has_been_tried / search_history
# --------------------------------------------------------------------------- #


def test_record_attempt_then_has_been_tried(attempts_path):
    key = AttemptKey.from_value(
        "parameters.w_redeem_progress", "increase", 0.55
    )
    assert has_been_tried(key, path=attempts_path) == []

    record_attempt(
        key, run_id="run-1", outcome="accepted", path=attempts_path
    )
    hits = has_been_tried(key, path=attempts_path)
    assert len(hits) == 1
    assert hits[0]["run_id"] == "run-1"
    assert hits[0]["outcome"] == "accepted"
    assert hits[0]["item_path"] == "parameters.w_redeem_progress"


def test_has_been_tried_returns_all_rows_for_same_key(attempts_path):
    key = AttemptKey.from_value(
        "parameters.w_redeem_progress", "increase", 0.55
    )
    record_attempt(key, run_id="run-1", outcome="accepted", path=attempts_path)
    record_attempt(key, run_id="run-2", outcome="rejected", path=attempts_path)
    record_attempt(key, run_id="run-3", outcome="no_change", path=attempts_path)

    hits = has_been_tried(key, path=attempts_path)
    assert len(hits) == 3
    assert {h["run_id"] for h in hits} == {"run-1", "run-2", "run-3"}
    assert {h["outcome"] for h in hits} == {"accepted", "rejected", "no_change"}


def test_record_attempt_rejects_unknown_outcome(attempts_path):
    key = AttemptKey.from_value("parameters.x", "set", 1.0)
    with pytest.raises(ValueError):
        record_attempt(key, run_id="r", outcome="maybe", path=attempts_path)


def test_search_history_filter_by_item_path(attempts_path):
    k_a = AttemptKey.from_value("parameters.w_a", "set", 1.0)
    k_b = AttemptKey.from_value("parameters.w_b", "set", 2.0)
    record_attempt(k_a, run_id="r1", outcome="accepted", path=attempts_path)
    record_attempt(k_b, run_id="r2", outcome="rejected", path=attempts_path)
    record_attempt(k_a, run_id="r3", outcome="no_change", path=attempts_path)

    only_a = search_history(item_path="parameters.w_a", path=attempts_path)
    assert len(only_a) == 2
    assert {r["run_id"] for r in only_a} == {"r1", "r3"}

    only_b = search_history(item_path="parameters.w_b", path=attempts_path)
    assert len(only_b) == 1
    assert only_b[0]["run_id"] == "r2"

    everything = search_history(path=attempts_path)
    assert len(everything) == 3


def test_search_history_filter_by_since_iso(attempts_path):
    k = AttemptKey.from_value("parameters.x", "set", 1.0)
    record_attempt(
        k,
        run_id="r-old",
        outcome="accepted",
        path=attempts_path,
        timestamp_iso="2026-05-01T00:00:00Z",
    )
    record_attempt(
        k,
        run_id="r-new",
        outcome="accepted",
        path=attempts_path,
        timestamp_iso="2026-05-07T00:00:00Z",
    )

    recent = search_history(
        since_iso="2026-05-05T00:00:00Z", path=attempts_path
    )
    assert [r["run_id"] for r in recent] == ["r-new"]


# --------------------------------------------------------------------------- #
# File lock — sequential writes succeed without blocking
# --------------------------------------------------------------------------- #


def test_sequential_writes_do_not_block(runs_path, attempts_path):
    """Two back-to-back writes against each store complete and persist.

    The lock is held only for the duration of each call, so a second
    call from the same thread must proceed immediately.
    """
    append_run(_make_record(1), path=runs_path)
    append_run(_make_record(2), path=runs_path)
    assert [r.iteration for r in read_runs(path=runs_path)] == [1, 2]

    k = AttemptKey.from_value("parameters.x", "set", 0.5)
    record_attempt(k, run_id="r1", outcome="accepted", path=attempts_path)
    record_attempt(k, run_id="r2", outcome="rejected", path=attempts_path)
    assert len(has_been_tried(k, path=attempts_path)) == 2
