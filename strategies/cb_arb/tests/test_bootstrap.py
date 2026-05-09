"""cb_arb bootstrap 测试 — 全部用 tmp_path + 合成 parquet."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from strategies.cb_arb.bootstrap import (
    DEFAULT_YAML_PATH,
    _slice_dates_chronologically,
    bootstrap,
)


def _write_synthetic_cb_daily(parquet_path: Path, n_oos_days: int = 100) -> None:
    """合成 cb_daily.parquet: IS 200 天 + OOS n_oos_days 天.

    所有 OOS 日期 >= 20250101.
    """
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    is_dates = pd.bdate_range(start="2024-01-01", periods=200).strftime("%Y%m%d")
    oos_dates = pd.bdate_range(start="2025-01-02", periods=n_oos_days).strftime("%Y%m%d")
    all_dates = list(is_dates) + list(oos_dates)
    n = len(all_dates)
    df = pd.DataFrame({
        "ts_code": ["110001.SH"] * n,
        "trade_date": all_dates,
        "open": [100.0] * n,
        "high": [102.0] * n,
        "low": [99.0] * n,
        "close": [101.0] * n,
        "vol": [1000] * n,
    })
    df.to_parquet(parquet_path)


def _write_min_yaml(yaml_path: Path) -> None:
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        "version: 1\n"
        "strategy: cb_arb\n"
        "last_updated: '2026-05-09T00:00:00Z'\n"
        "parameters:\n"
        "  - name: vol_window_days\n"
        "    current: 60\n"
        "    range: [20, 120]\n"
        "    prior: x\n"
        "factors: []\n"
        "thresholds: []\n"
        "rules:\n"
        "  - name: fee_pct\n"
        "    current: 0.0003\n"
        "    range: [0.0001, 0.0010]\n"
        "    prior: x\n",
        encoding="utf-8",
    )


def test_slice_dates_chronologically_keeps_order():
    dates = ["20250101", "20250102", "20250103", "20250104",
             "20250105", "20250106", "20250107", "20250108"]
    pools = _slice_dates_chronologically(dates, n_pools=4)
    assert len(pools) == 4
    assert sum(len(p) for p in pools) == 8
    for p in pools:
        assert p == sorted(p)
    flat = [d for p in pools for d in p]
    assert flat == dates


def test_bootstrap_creates_files(tmp_path: Path):
    """成功路径: sealed_pools.json + state.json 都创建."""
    parquet = tmp_path / "raw" / "cb_daily.parquet"
    _write_synthetic_cb_daily(parquet, n_oos_days=80)
    yaml = tmp_path / "tunable_space.yaml"
    _write_min_yaml(yaml)

    summary = bootstrap(
        data_dir=tmp_path,
        yaml_path=yaml,
        cb_daily_parquet=parquet,
        oos_split_date="20250101",
        n_pools=8,
        seed=42,
        force=False,
        verbose=False,
    )

    sealed = tmp_path / "sealed_pools.json"
    state = tmp_path / "state.json"
    assert sealed.exists()
    assert state.exists()

    sp = json.loads(sealed.read_text())
    assert sp["version"] == 1
    assert sp["strategy"] == "cb_arb"
    assert sp["n_pools"] == 8
    assert len(sp["pools"]) == 8
    for p in sp["pools"]:
        assert "id" in p
        assert "event_ids" in p
        assert p["read_count"] == 0
        assert p["first_read_at"] is None
        assert p["sealed_at"] is None

    st = json.loads(state.read_text())
    assert st["state"] == "stopped"
    assert st["iteration"] == 0
    assert st["current_pool_id"] is None

    assert summary["n_pools"] == 8
    assert sum(summary["pool_sizes"]) == summary["oos_dates"]


def test_bootstrap_pools_chronologically_split(tmp_path: Path):
    """pool 0 dates 全部 < pool 1 dates < ..."""
    parquet = tmp_path / "raw" / "cb_daily.parquet"
    _write_synthetic_cb_daily(parquet, n_oos_days=80)
    yaml = tmp_path / "tunable_space.yaml"
    _write_min_yaml(yaml)

    bootstrap(
        data_dir=tmp_path,
        yaml_path=yaml,
        cb_daily_parquet=parquet,
        oos_split_date="20250101",
        n_pools=8,
        seed=42,
        verbose=False,
    )

    sp = json.loads((tmp_path / "sealed_pools.json").read_text())
    pools = sp["pools"]
    for i in range(len(pools) - 1):
        cur_max = max(pools[i]["event_ids"])
        nxt_min = min(pools[i + 1]["event_ids"])
        assert cur_max < nxt_min, (
            f"pool {i} max={cur_max} not < pool {i+1} min={nxt_min}"
        )


def test_bootstrap_raises_on_existing_pools_without_force(tmp_path: Path):
    """已存在 sealed_pools.json + 不带 --force → FileExistsError."""
    parquet = tmp_path / "raw" / "cb_daily.parquet"
    _write_synthetic_cb_daily(parquet, n_oos_days=40)
    yaml = tmp_path / "tunable_space.yaml"
    _write_min_yaml(yaml)

    bootstrap(
        data_dir=tmp_path, yaml_path=yaml, cb_daily_parquet=parquet,
        oos_split_date="20250101", n_pools=4, verbose=False,
    )
    with pytest.raises(FileExistsError):
        bootstrap(
            data_dir=tmp_path, yaml_path=yaml, cb_daily_parquet=parquet,
            oos_split_date="20250101", n_pools=4, verbose=False, force=False,
        )

    summary = bootstrap(
        data_dir=tmp_path, yaml_path=yaml, cb_daily_parquet=parquet,
        oos_split_date="20250101", n_pools=4, verbose=False, force=True,
    )
    assert summary["n_pools"] == 4


def test_bootstrap_raises_on_missing_cb_daily(tmp_path: Path):
    """cb_daily parquet 缺失 → FileNotFoundError."""
    yaml = tmp_path / "tunable_space.yaml"
    _write_min_yaml(yaml)
    parquet = tmp_path / "missing.parquet"

    with pytest.raises(FileNotFoundError):
        bootstrap(
            data_dir=tmp_path, yaml_path=yaml, cb_daily_parquet=parquet,
            oos_split_date="20250101", n_pools=4, verbose=False,
        )


def test_bootstrap_raises_on_missing_yaml(tmp_path: Path):
    """yaml 缺失 → FileNotFoundError."""
    parquet = tmp_path / "raw" / "cb_daily.parquet"
    _write_synthetic_cb_daily(parquet, n_oos_days=40)

    with pytest.raises(FileNotFoundError):
        bootstrap(
            data_dir=tmp_path, yaml_path=tmp_path / "no_yaml.yaml",
            cb_daily_parquet=parquet, oos_split_date="20250101", n_pools=4,
            verbose=False,
        )


def test_bootstrap_default_n_pools_is_8():
    """默认 n_pools = 8 (对齐其它策略)."""
    from strategies.cb_arb.bootstrap import DEFAULT_N_POOLS
    assert DEFAULT_N_POOLS == 8
