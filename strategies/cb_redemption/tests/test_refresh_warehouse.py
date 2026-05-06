"""Tests for the daily warehouse refresh entrypoint.

All tests are isolated under ``tmp_path``: we monkeypatch the module-level
path constants so no real ``data/cb_warehouse/`` or
``data/cb_redemption/`` is touched, and we mock ``subprocess.run`` plus
``build_historical_snapshots`` so no network or pandas work happens.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# Path-up two levels so we import scripts/refresh_warehouse.py
_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(_SCRIPTS.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS.parent))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import refresh_warehouse as rw  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect every module-level path constant under tmp_path."""
    warehouse = tmp_path / "data" / "cb_warehouse"
    redemption = tmp_path / "data" / "cb_redemption"
    logs = tmp_path / "logs"
    warehouse.mkdir(parents=True)
    redemption.mkdir(parents=True)
    logs.mkdir(parents=True)

    monkeypatch.setattr(rw, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(rw, "WAREHOUSE_DIR", warehouse)
    monkeypatch.setattr(rw, "REDEMPTION_DIR", redemption)
    monkeypatch.setattr(rw, "LOGS_DIR", logs)
    monkeypatch.setattr(rw, "SNAPSHOT_PARQUET", warehouse / "strong_timeline_snapshots.parquet")
    monkeypatch.setattr(rw, "MARKER_PATH", redemption / "last_refresh.json")
    monkeypatch.setattr(rw, "LOG_PATH", logs / "refresh_warehouse.log")
    monkeypatch.setattr(rw, "BUILD_WAREHOUSE_SCRIPT", tmp_path / "scripts" / "build_cb_warehouse.py")

    # 清掉之前测试建好的 logger handlers, 否则 FileHandler 仍指向旧路径
    import logging
    log = logging.getLogger("refresh_warehouse")
    for h in list(log.handlers):
        log.removeHandler(h)

    return {"root": tmp_path, "warehouse": warehouse, "redemption": redemption, "logs": logs}


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["python"], returncode=returncode, stdout=stdout, stderr=stderr)


def _install_fake_data_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_exc: Exception | None = None,
    rows: int = 5,
) -> dict[str, Any]:
    """Inject a stub for ``strategies.cb_redemption.data.build_historical_snapshots``.

    Returns a dict so test can inspect call counts / kwargs.
    """
    state: dict[str, Any] = {"calls": 0, "kwargs": None}

    def fake_build(start: str = "20200101", end: str | None = None, force_rebuild: bool = False):
        state["calls"] += 1
        state["kwargs"] = {"start": start, "end": end, "force_rebuild": force_rebuild}
        if raise_exc is not None:
            raise raise_exc
        # return something with a length attribute
        return list(range(rows))

    # patch on the real module if it's already imported
    real = sys.modules.get("strategies.cb_redemption.data")
    if real is not None:
        monkeypatch.setattr(real, "build_historical_snapshots", fake_build)
    else:
        fake_mod = types.ModuleType("strategies.cb_redemption.data")
        fake_mod.build_historical_snapshots = fake_build
        monkeypatch.setitem(sys.modules, "strategies.cb_redemption.data", fake_mod)
    return state


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_happy_path_writes_marker_exit_zero(
    isolated_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_cb_warehouse OK + snapshot rebuild OK → marker exit_code=0."""
    monkeypatch.setattr(rw.subprocess, "run", lambda *a, **kw: _fake_completed(0, "ok\n", ""))
    state = _install_fake_data_module(monkeypatch, rows=42)

    rc = rw.main()

    assert rc == 0
    assert state["calls"] == 1
    assert state["kwargs"]["force_rebuild"] is True

    marker_path = isolated_paths["redemption"] / "last_refresh.json"
    assert marker_path.exists()
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 0
    assert "ts_iso" in payload
    assert payload["ts_iso"].endswith("Z")
    assert "warehouse_summary" in payload
    assert "snapshot_summary" in payload
    assert isinstance(payload["elapsed_sec"], (int, float))
    assert "error" not in payload


def test_warehouse_subprocess_nonzero_marks_failure(
    isolated_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_cb_warehouse exits 2 → marker exit_code=1, snapshot not called."""
    monkeypatch.setattr(
        rw.subprocess, "run",
        lambda *a, **kw: _fake_completed(2, "", "tushare auth failed\n"),
    )
    state = _install_fake_data_module(monkeypatch, rows=99)

    rc = rw.main()

    assert rc == 1
    # 步骤 1 失败,不应继续 step 2
    assert state["calls"] == 0

    marker_path = isolated_paths["redemption"] / "last_refresh.json"
    assert marker_path.exists()
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert "error" in payload
    assert "build_cb_warehouse" in payload["error"]


def test_snapshot_rebuild_raises_marks_failure(
    isolated_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_historical_snapshots raises → exit_code=1, marker still written."""
    monkeypatch.setattr(rw.subprocess, "run", lambda *a, **kw: _fake_completed(0, "ok", ""))
    _install_fake_data_module(monkeypatch, raise_exc=RuntimeError("disk full"))

    rc = rw.main()

    assert rc == 1
    marker_path = isolated_paths["redemption"] / "last_refresh.json"
    assert marker_path.exists()
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert "step2" in payload["error"]
    assert "disk full" in payload["error"]


def test_already_latest_still_writes_marker_and_exit_zero(
    isolated_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if build_cb_warehouse is a no-op (already latest), marker is written.

    We simulate this with rc=0 and zero-byte stdout from the subprocess; the
    snapshot rebuild still runs. Marker should reflect the up-to-date state.
    """
    monkeypatch.setattr(
        rw.subprocess, "run",
        lambda *a, **kw: _fake_completed(0, "ℹ️  无新数据\n", ""),
    )
    state = _install_fake_data_module(monkeypatch, rows=0)

    rc = rw.main()

    assert rc == 0
    assert state["calls"] == 1

    marker_path = isolated_paths["redemption"] / "last_refresh.json"
    assert marker_path.exists()
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 0
    # snapshot/warehouse summaries default to zero-rows since no parquet exists
    assert payload["snapshot_summary"]["rows"] == 0
    assert payload["warehouse_summary"]["cb_daily_rows"] == 0


def test_subprocess_timeout_marks_failure(
    isolated_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired from build_cb_warehouse → exit_code=1, no exception escapes."""
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="python", timeout=kw.get("timeout", 1))

    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    state = _install_fake_data_module(monkeypatch, rows=10)

    rc = rw.main()

    assert rc == 1
    assert state["calls"] == 0
    marker_path = isolated_paths["redemption"] / "last_refresh.json"
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert "build_cb_warehouse" in payload["error"]
