"""Unit tests for the OOS holdout pool guard.

All tests use ``tmp_path`` for isolation — they MUST NOT touch the real
``data/cb_redemption/sealed_pools.json``.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from strategies.cb_redemption.holdout import (
    InvalidPoolIdError,
    PoolAlreadyReadError,
    PoolFileExistsError,
    pools_remaining,
    read_pool,
    seal_pool,
    slice_oos_into_pools,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def pool_file(tmp_path):
    """Isolated pool file path. Does not exist yet."""
    return tmp_path / "sealed_pools.json"


@pytest.fixture
def events_df():
    """37 synthetic OOS events — odd number so round-robin is uneven."""
    return pd.DataFrame(
        {
            "bond_id_meeting_date": [f"bond{i:03d}_2025-01-01" for i in range(37)],
            "noise": list(range(37)),
        }
    )


@pytest.fixture
def sliced(events_df, pool_file):
    """A pool file already sliced into 4 pools."""
    slice_oos_into_pools(events_df, n_pools=4, seed=42, pool_file=pool_file)
    return pool_file


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_slice_creates_n_pools_with_disjoint_events(events_df, pool_file):
    data = slice_oos_into_pools(
        events_df, n_pools=4, seed=42, pool_file=pool_file, split_at="2025-01-01"
    )

    assert pool_file.exists()
    assert data["n_pools"] == 4
    assert data["version"] == 1
    assert data["split_at"] == "2025-01-01"
    assert len(data["pools"]) == 4

    # All event_ids must be partitioned (no overlap, no loss).
    all_ids: list[str] = []
    for pool in data["pools"]:
        assert pool["read_count"] == 0
        assert pool["first_read_at"] is None
        assert pool["sealed_at"] is None
        all_ids.extend(pool["event_ids"])
    expected = set(events_df["bond_id_meeting_date"].astype(str))
    assert set(all_ids) == expected
    assert len(all_ids) == len(expected)  # disjoint

    # Sizes within +/- 1 of each other.
    sizes = sorted(len(p["event_ids"]) for p in data["pools"])
    assert sizes[-1] - sizes[0] <= 1


def test_slice_twice_raises(events_df, pool_file):
    slice_oos_into_pools(events_df, n_pools=4, seed=42, pool_file=pool_file)
    with pytest.raises(PoolFileExistsError):
        slice_oos_into_pools(events_df, n_pools=4, seed=42, pool_file=pool_file)


def test_read_pool_increments_count(sliced):
    pool_file = sliced
    ids = read_pool(0, pool_file=pool_file)
    assert isinstance(ids, list) and len(ids) > 0

    on_disk = json.loads(pool_file.read_text())
    pool0 = next(p for p in on_disk["pools"] if p["id"] == 0)
    assert pool0["read_count"] == 1
    assert pool0["first_read_at"] is not None
    assert pool0["sealed_at"] is None
    assert sorted(pool0["event_ids"]) == sorted(ids)


def test_read_already_read_pool_raises(sliced):
    pool_file = sliced
    read_pool(1, pool_file=pool_file)
    with pytest.raises(PoolAlreadyReadError) as exc_info:
        read_pool(1, pool_file=pool_file)
    assert exc_info.value.pool_id == 1
    assert exc_info.value.read_count == 1

    # State must be unchanged after the failed second read.
    on_disk = json.loads(pool_file.read_text())
    pool1 = next(p for p in on_disk["pools"] if p["id"] == 1)
    assert pool1["read_count"] == 1


def test_pools_remaining_excludes_read(sliced):
    pool_file = sliced
    assert pools_remaining(pool_file=pool_file) == [0, 1, 2, 3]

    read_pool(0, pool_file=pool_file)
    read_pool(2, pool_file=pool_file)
    assert pools_remaining(pool_file=pool_file) == [1, 3]

    read_pool(1, pool_file=pool_file)
    read_pool(3, pool_file=pool_file)
    assert pools_remaining(pool_file=pool_file) == []


# --------------------------------------------------------------------------- #
# Bonus tests for guard branches
# --------------------------------------------------------------------------- #


def test_seal_pool_is_idempotent(sliced):
    pool_file = sliced
    seal_pool(0, pool_file=pool_file)
    first = json.loads(pool_file.read_text())
    sealed_at_first = next(p for p in first["pools"] if p["id"] == 0)["sealed_at"]
    assert sealed_at_first is not None

    seal_pool(0, pool_file=pool_file)  # second call must not change timestamp
    second = json.loads(pool_file.read_text())
    sealed_at_second = next(p for p in second["pools"] if p["id"] == 0)["sealed_at"]
    assert sealed_at_first == sealed_at_second


def test_invalid_pool_id_raises(sliced):
    with pytest.raises(InvalidPoolIdError):
        read_pool(99, pool_file=sliced)
    with pytest.raises(InvalidPoolIdError):
        seal_pool(99, pool_file=sliced)


def test_slice_rejects_too_few_events(pool_file):
    df = pd.DataFrame({"bond_id_meeting_date": ["only_one"]})
    with pytest.raises(ValueError):
        slice_oos_into_pools(df, n_pools=4, pool_file=pool_file)


def test_slice_rejects_duplicate_ids(pool_file):
    df = pd.DataFrame(
        {"bond_id_meeting_date": ["a", "b", "a", "c", "d", "e", "f", "g"]}
    )
    with pytest.raises(ValueError, match="duplicate"):
        slice_oos_into_pools(df, n_pools=4, pool_file=pool_file)
