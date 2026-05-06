"""Tests for the bootstrap one-shot setup.

All tests are isolated under ``tmp_path`` and never touch the real
``data/cb_redemption/`` directory or the real tunable_space.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from strategies.cb_redemption import bootstrap as bootstrap_mod


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_minimal_yaml(path: Path) -> None:
    payload = {
        "version": 1,
        "strategy": "cb_redemption",
        "last_updated": "2026-05-07T00:00:00Z",
        "parameters": [
            {"name": "w_redeem_progress", "current": 1.0,
             "range": [0.5, 5.0], "prior": "x"},
        ],
        "factors": [],
        "thresholds": [],
        "rules": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)


def _write_warehouse(warehouse_dir: Path) -> None:
    """Touch the parquet filenames bootstrap requires (content unchecked)."""
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    for name in bootstrap_mod.REQUIRED_WAREHOUSE_PARQUETS:
        (warehouse_dir / name).write_bytes(b"PAR1")  # any non-empty bytes


def _write_events_csv(path: Path, n_oos: int = 12, n_is: int = 8) -> Path:
    """Synthesize an events CSV with explicit IS/OOS split."""
    rows = []
    # IS: 2024-01..2024-12
    for i in range(n_is):
        rows.append(
            {
                "bond_id": f"12{i:04d}",
                "name": f"bondIS{i}",
                "meeting_date": f"2024-0{1 + (i % 9)}-15",
                "before_price": 10.0,
                "after_price": 7.0,
                "ratio": 0.7,
                "is_deep": True,
            }
        )
    # OOS: 2025-01..2025-12
    for i in range(n_oos):
        rows.append(
            {
                "bond_id": f"13{i:04d}",
                "name": f"bondOOS{i}",
                "meeting_date": f"2025-0{1 + (i % 9)}-{10 + (i % 18):02d}",
                "before_price": 10.0,
                "after_price": 7.0,
                "ratio": 0.7,
                "is_deep": True,
            }
        )
    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


@pytest.fixture
def setup_paths(tmp_path: Path):
    """Build the minimum filesystem state bootstrap() needs to succeed."""
    data_dir = tmp_path / "data_cb_redemption"
    yaml_path = tmp_path / "tunable_space.yaml"
    events_csv = tmp_path / "events.csv"
    warehouse_dir = tmp_path / "cb_warehouse"

    _write_minimal_yaml(yaml_path)
    _write_warehouse(warehouse_dir)
    _write_events_csv(events_csv)

    return {
        "data_dir": data_dir,
        "yaml_path": yaml_path,
        "events_csv": events_csv,
        "warehouse_dir": warehouse_dir,
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_bootstrap_creates_sealed_pools(setup_paths) -> None:
    summary = bootstrap_mod.bootstrap(
        data_dir=setup_paths["data_dir"],
        yaml_path=setup_paths["yaml_path"],
        events_csv=setup_paths["events_csv"],
        warehouse_dir=setup_paths["warehouse_dir"],
        oos_split_date="2025-01-01",
        n_pools=4,
        seed=42,
        verbose=False,
    )

    sealed_path = setup_paths["data_dir"] / "sealed_pools.json"
    assert sealed_path.exists()

    data = json.loads(sealed_path.read_text())
    assert data["n_pools"] == 4
    assert len(data["pools"]) == 4

    # All OOS event_ids must be partitioned across pools (12 OOS events).
    all_ids = []
    for pool in data["pools"]:
        all_ids.extend(pool["event_ids"])
    assert len(all_ids) == 12
    assert len(set(all_ids)) == 12  # disjoint

    # Summary mirrors the file.
    assert summary["events_oos"] == 12
    assert summary["n_pools"] == 4
    assert sum(summary["pool_sizes"]) == 12


def test_bootstrap_creates_initial_state_json(setup_paths) -> None:
    bootstrap_mod.bootstrap(
        data_dir=setup_paths["data_dir"],
        yaml_path=setup_paths["yaml_path"],
        events_csv=setup_paths["events_csv"],
        warehouse_dir=setup_paths["warehouse_dir"],
        verbose=False,
    )

    state_path = setup_paths["data_dir"] / "state.json"
    assert state_path.exists()

    state = json.loads(state_path.read_text())
    assert state["state"] == "stopped"
    assert state["iteration"] == 0
    assert state["last_verdict"] is None
    assert state["paused_reason"] is None
    assert state["none_streak"] == 0
    assert state["stagnant_streak"] == 0
    assert state["recovery_attempt"] == 0
    assert state["since_iso"]  # non-empty


def test_bootstrap_raises_if_yaml_missing(setup_paths) -> None:
    setup_paths["yaml_path"].unlink()
    with pytest.raises(FileNotFoundError, match="tunable_space.yaml"):
        bootstrap_mod.bootstrap(
            data_dir=setup_paths["data_dir"],
            yaml_path=setup_paths["yaml_path"],
            events_csv=setup_paths["events_csv"],
            warehouse_dir=setup_paths["warehouse_dir"],
            verbose=False,
        )


def test_bootstrap_raises_if_warehouse_missing(setup_paths) -> None:
    # Remove one required parquet.
    target = setup_paths["warehouse_dir"] / bootstrap_mod.REQUIRED_WAREHOUSE_PARQUETS[0]
    target.unlink()
    with pytest.raises(FileNotFoundError, match="missing required parquet"):
        bootstrap_mod.bootstrap(
            data_dir=setup_paths["data_dir"],
            yaml_path=setup_paths["yaml_path"],
            events_csv=setup_paths["events_csv"],
            warehouse_dir=setup_paths["warehouse_dir"],
            verbose=False,
        )


def test_bootstrap_raises_if_existing_pool_without_force(setup_paths) -> None:
    # First call succeeds.
    bootstrap_mod.bootstrap(
        data_dir=setup_paths["data_dir"],
        yaml_path=setup_paths["yaml_path"],
        events_csv=setup_paths["events_csv"],
        warehouse_dir=setup_paths["warehouse_dir"],
        verbose=False,
    )
    # Second call without --force must raise.
    with pytest.raises(FileExistsError, match="pass --force"):
        bootstrap_mod.bootstrap(
            data_dir=setup_paths["data_dir"],
            yaml_path=setup_paths["yaml_path"],
            events_csv=setup_paths["events_csv"],
            warehouse_dir=setup_paths["warehouse_dir"],
            verbose=False,
        )


def test_bootstrap_force_overwrites_pool(setup_paths) -> None:
    # Cut once with seed=42.
    bootstrap_mod.bootstrap(
        data_dir=setup_paths["data_dir"],
        yaml_path=setup_paths["yaml_path"],
        events_csv=setup_paths["events_csv"],
        warehouse_dir=setup_paths["warehouse_dir"],
        seed=42,
        verbose=False,
    )
    sealed_path = setup_paths["data_dir"] / "sealed_pools.json"
    first = json.loads(sealed_path.read_text())

    # Recut with a different seed using --force.
    bootstrap_mod.bootstrap(
        data_dir=setup_paths["data_dir"],
        yaml_path=setup_paths["yaml_path"],
        events_csv=setup_paths["events_csv"],
        warehouse_dir=setup_paths["warehouse_dir"],
        seed=999,
        force=True,
        verbose=False,
    )
    second = json.loads(sealed_path.read_text())

    # Different seed → different pool assignment (overwhelmingly likely
    # for 12 events / 4 pools — collision probability ~1 in 4^12).
    assert first["seed"] == 42
    assert second["seed"] == 999
    assert first["pools"] != second["pools"]


def test_bootstrap_raises_if_events_csv_empty(setup_paths) -> None:
    # Truncate events CSV to header only.
    setup_paths["events_csv"].write_text(
        "bond_id,name,meeting_date,before_price,after_price,ratio,is_deep\n"
    )
    with pytest.raises(ValueError, match="empty"):
        bootstrap_mod.bootstrap(
            data_dir=setup_paths["data_dir"],
            yaml_path=setup_paths["yaml_path"],
            events_csv=setup_paths["events_csv"],
            warehouse_dir=setup_paths["warehouse_dir"],
            verbose=False,
        )


def test_bootstrap_raises_if_no_oos_events(setup_paths) -> None:
    # All events fall before the OOS cutoff → empty OOS slice.
    with pytest.raises(ValueError, match="No OOS events"):
        bootstrap_mod.bootstrap(
            data_dir=setup_paths["data_dir"],
            yaml_path=setup_paths["yaml_path"],
            events_csv=setup_paths["events_csv"],
            warehouse_dir=setup_paths["warehouse_dir"],
            oos_split_date="2099-01-01",  # in the future
            verbose=False,
        )
