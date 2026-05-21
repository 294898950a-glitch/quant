from __future__ import annotations

from pathlib import Path

from framework.autonomous.queue_remote_execution import should_sync_path_for_run


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sync_filter_skips_large_data_dependencies_but_allows_prepared_data():
    spec_path = REPO_ROOT / "data" / "run_a" / "spec.yaml"

    assert not should_sync_path_for_run(
        REPO_ROOT / "data" / "cb_warehouse" / "cb_daily.parquet",
        "data/cb_warehouse/cb_daily.parquet",
        spec_path,
        REPO_ROOT,
    )
    assert not should_sync_path_for_run(
        REPO_ROOT / "data" / "old_run" / "daily_value_gap_amounts.parquet",
        "data/old_run/daily_value_gap_amounts.parquet",
        spec_path,
        REPO_ROOT,
    )
    assert should_sync_path_for_run(
        REPO_ROOT / "data" / "run_a" / "prepared_data" / "data" / "cb_warehouse" / "cb_daily.parquet",
        "data/run_a/prepared_data/data/cb_warehouse/cb_daily.parquet",
        spec_path,
        REPO_ROOT,
    )
    assert should_sync_path_for_run(
        REPO_ROOT / "data" / "research_framework" / "runtime_entrypoints.yaml",
        "data/research_framework/runtime_entrypoints.yaml",
        spec_path,
        REPO_ROOT,
    )
