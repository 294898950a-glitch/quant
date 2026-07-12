from __future__ import annotations

import yaml
import pandas as pd

from scripts import build_data_inventory
from framework.autonomous.data_inventory import compact_data_inventory


def test_build_data_inventory_records_core_file_metadata_by_default(tmp_path):
    data_root = tmp_path / "data"
    warehouse = data_root / "cb_warehouse"
    warehouse.mkdir(parents=True)
    parquet_path = warehouse / "cb_daily.parquet"
    pd.DataFrame(
        {
            "ts_code": ["A", "A"],
            "trade_date": [20200102, 20200103],
            "close": [100.0, 101.0],
        }
    ).to_parquet(parquet_path)

    run_dir = data_root / "example_run"
    run_dir.mkdir()
    csv_path = run_dir / "trades.csv"
    csv_path.write_text("entry_date,pnl\n2020-01-02,1.5\n2020-01-03,-0.5\n", encoding="utf-8")
    (run_dir / "spec.yaml").write_text(
        yaml.safe_dump(
            {
                "required_data": [
                    {"path": "data/cb_warehouse/cb_daily.parquet"},
                    {"path": "data/example_run/missing_trade_pnl.parquet"},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    inventory = build_data_inventory.build_inventory(repo_root=tmp_path, data_root=data_root)

    assert inventory["summary"]["file_count"] == 1
    assert inventory["summary"]["scope"] == "core_database_only"
    by_path = {entry["path"]: entry for entry in inventory["files"]}
    assert by_path["data/cb_warehouse/cb_daily.parquet"]["category"] == "core_warehouse"
    assert by_path["data/cb_warehouse/cb_daily.parquet"]["rows"] == 2
    assert by_path["data/cb_warehouse/cb_daily.parquet"]["date_ranges"]["trade_date"] == {
        "min": "2020-01-02",
        "max": "2020-01-03",
    }
    assert "data/example_run/trades.csv" not in by_path
    assert "referenced_data_paths" not in inventory


def test_build_data_inventory_can_include_artifacts_for_maintenance(tmp_path):
    data_root = tmp_path / "data"
    warehouse = data_root / "cb_warehouse"
    warehouse.mkdir(parents=True)
    pd.DataFrame({"trade_date": [20200102], "close": [100.0]}).to_parquet(warehouse / "cb_daily.parquet")
    run_dir = data_root / "example_run"
    run_dir.mkdir()
    (run_dir / "trades.csv").write_text("entry_date,pnl\n2020-01-02,1.5\n", encoding="utf-8")
    (run_dir / "spec.yaml").write_text(
        yaml.safe_dump({"required_data": [{"path": "data/example_run/missing_trade_pnl.parquet"}]}),
        encoding="utf-8",
    )

    inventory = build_data_inventory.build_inventory(repo_root=tmp_path, data_root=data_root, include_artifacts=True)

    assert inventory["summary"]["file_count"] == 2
    assert inventory["summary"]["scope"] == "all_data_artifacts"
    assert inventory["referenced_data_paths"]["missing_top"] == [
        {"path": "data/example_run/missing_trade_pnl.parquet", "reference_count": 1}
    ]


def test_compact_data_inventory_keeps_ideation_context_separate():
    inventory = {
        "summary": {"file_count": 3},
        "files": [
            {
                "path": "data/cb_warehouse/cb_daily.parquet",
                "category": "core_warehouse",
                "format": "parquet",
                "rows": 10,
                "columns": ["trade_date", "close"],
                "date_ranges": {"trade_date": {"min": "2020-01-02", "max": "2020-01-03"}},
                "readable": True,
            },
        ],
        "referenced_data_paths": {
            "missing_top": [
                {"path": "data/run_a/missing.parquet", "reference_count": 2},
            ],
        },
    }

    compact = compact_data_inventory(inventory)

    assert compact["available"] is True
    assert compact["core_files"][0]["path"] == "data/cb_warehouse/cb_daily.parquet"
    assert compact["missing_referenced_data_top"][0]["path"] == "data/run_a/missing.parquet"
    assert "data-quality approval" in compact["rule"]
    assert "experiment results" in compact["rule"]
