#!/usr/bin/env python3
"""
每日数据刷新入口 — 由 systemd timer / cron 调度。

步骤:
  1. 调 build_cb_warehouse.py --update 拉新交易日数据
  2. 调 build_historical_snapshots(force_rebuild=True) 重建 snapshot
  3. 写 marker data/cb_redemption/last_refresh.json
  4. log 全程到 logs/refresh_warehouse.log

退出码:
  0 = 成功(包括"已是最新无需刷新"这种早退)
  1 = 失败(orchestrator 的审计员后续会看 last_refresh 时间戳判过期)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# 路径常量 — 全部基于仓库根目录,与 orchestrator/data.py 对齐。
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
WAREHOUSE_DIR = ROOT_DIR / "data" / "cb_warehouse"
REDEMPTION_DIR = ROOT_DIR / "data" / "cb_redemption"
LOGS_DIR = ROOT_DIR / "logs"

BUILD_WAREHOUSE_SCRIPT = SCRIPTS_DIR / "build_cb_warehouse.py"
SNAPSHOT_PARQUET = WAREHOUSE_DIR / "strong_timeline_snapshots.parquet"
MARKER_PATH = REDEMPTION_DIR / "last_refresh.json"
LOG_PATH = LOGS_DIR / "refresh_warehouse.log"

BUILD_TIMEOUT_SEC = 30 * 60  # 子进程最多跑 30 分钟


# --------------------------------------------------------------------------- #
# Logging — 同时写文件和 stdout (systemd 会再 append 到 cb_refresh.stdout.log)
# --------------------------------------------------------------------------- #

def _setup_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    REDEMPTION_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("refresh_warehouse")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 防止重复 handler (测试里多次调用)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# --------------------------------------------------------------------------- #
# 步骤 1: build_cb_warehouse.py --update
# --------------------------------------------------------------------------- #

def run_warehouse_update(logger: logging.Logger) -> tuple[int, str, str]:
    """调子进程拉最新交易日。返回 (returncode, stdout, stderr)。"""
    cmd = [sys.executable, str(BUILD_WAREHOUSE_SCRIPT), "--update"]
    logger.info("step1: %s", " ".join(cmd))
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("step1: build_cb_warehouse 超时 %ds", BUILD_TIMEOUT_SEC)
        return 124, exc.stdout or "", (exc.stderr or "") + f"\nTimeoutExpired after {BUILD_TIMEOUT_SEC}s"
    elapsed = time.time() - t0
    logger.info(
        "step1: returncode=%d elapsed=%.1fs stdout_len=%d stderr_len=%d",
        proc.returncode, elapsed, len(proc.stdout or ""), len(proc.stderr or ""),
    )
    if proc.stdout:
        for line in proc.stdout.splitlines()[-20:]:
            logger.info("  [stdout] %s", line)
    if proc.stderr:
        for line in proc.stderr.splitlines()[-20:]:
            logger.info("  [stderr] %s", line)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# --------------------------------------------------------------------------- #
# 步骤 2: 重建 snapshot
# --------------------------------------------------------------------------- #

def rebuild_snapshot(logger: logging.Logger) -> None:
    """调用 build_historical_snapshots(force_rebuild=True)。
    任何异常向上抛(由 main 捕获)。"""
    logger.info("step2: build_historical_snapshots(force_rebuild=True)")
    t0 = time.time()
    # Lazy import — 测试里可以 monkeypatch sys.modules 注入 fake。
    from strategies.cb_redemption.data import build_historical_snapshots
    df = build_historical_snapshots(force_rebuild=True)
    elapsed = time.time() - t0
    rows = len(df) if df is not None else 0
    logger.info("step2: snapshot rebuilt rows=%d elapsed=%.1fs", rows, elapsed)


# --------------------------------------------------------------------------- #
# 仓库摘要 — 给 marker 和 log 用
# --------------------------------------------------------------------------- #

def _safe_parquet_summary(path: Path) -> dict[str, Any]:
    """读 parquet 拿 (rows, max_date)。读不到返回空 dict,不抛。"""
    if not path.exists():
        return {"exists": False}
    try:
        import pandas as pd
        df = pd.read_parquet(path)
        out: dict[str, Any] = {"rows": int(len(df))}
        if "trade_date" in df.columns and len(df):
            out["max_date"] = str(df["trade_date"].max())
        elif "date" in df.columns and len(df):
            out["max_date"] = str(df["date"].max())
        if "date" in df.columns and len(df):
            out["trade_days"] = int(df["date"].nunique())
        return out
    except Exception as exc:  # pragma: no cover - I/O 边界
        return {"exists": True, "error": str(exc)}


def collect_summaries() -> tuple[dict[str, Any], dict[str, Any]]:
    """采集 warehouse_summary 和 snapshot_summary。"""
    cb_daily = _safe_parquet_summary(WAREHOUSE_DIR / "cb_daily.parquet")
    cb_call = _safe_parquet_summary(WAREHOUSE_DIR / "cb_call.parquet")

    warehouse_summary = {
        "cb_daily_rows": cb_daily.get("rows", 0),
        "cb_daily_max_date": cb_daily.get("max_date", ""),
        "cb_call_rows": cb_call.get("rows", 0),
    }

    snap = _safe_parquet_summary(SNAPSHOT_PARQUET)
    snapshot_summary = {
        "rows": snap.get("rows", 0),
        "max_date": snap.get("max_date", ""),
        "trade_days": snap.get("trade_days", 0),
    }
    return warehouse_summary, snapshot_summary


# --------------------------------------------------------------------------- #
# Marker
# --------------------------------------------------------------------------- #

def write_marker(
    *,
    exit_code: int,
    elapsed_sec: float,
    warehouse_summary: dict[str, Any],
    snapshot_summary: dict[str, Any],
    error: Optional[str] = None,
) -> Path:
    """写 last_refresh.json。原子写(tmp + rename)。"""
    REDEMPTION_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warehouse_summary": warehouse_summary,
        "snapshot_summary": snapshot_summary,
        "elapsed_sec": round(float(elapsed_sec), 2),
        "exit_code": int(exit_code),
    }
    if error:
        payload["error"] = str(error)[:500]

    tmp_path = MARKER_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, MARKER_PATH)
    return MARKER_PATH


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    logger = _setup_logger()
    logger.info("=" * 60)
    logger.info("refresh_warehouse start | python=%s | cwd=%s", sys.executable, os.getcwd())
    overall_t0 = time.time()

    error_msg: Optional[str] = None
    exit_code = 0

    # --- step 1 ---
    try:
        rc, _stdout, stderr = run_warehouse_update(logger)
        if rc != 0:
            error_msg = f"build_cb_warehouse --update exited rc={rc}; stderr_tail={stderr.strip()[-200:]}"
            logger.error(error_msg)
            exit_code = 1
    except Exception as exc:
        error_msg = f"step1 exception: {exc!r}"
        logger.exception("step1 exception")
        exit_code = 1

    # --- step 2 ---
    if exit_code == 0:
        try:
            rebuild_snapshot(logger)
        except Exception as exc:
            error_msg = f"step2 exception: {exc!r}"
            logger.exception("step2 exception")
            exit_code = 1

    # --- 摘要 + marker (无论成功失败都写) ---
    elapsed = time.time() - overall_t0
    try:
        warehouse_summary, snapshot_summary = collect_summaries()
    except Exception as exc:
        logger.exception("collect_summaries failed")
        warehouse_summary = {"cb_daily_rows": 0, "cb_daily_max_date": "", "cb_call_rows": 0}
        snapshot_summary = {"rows": 0, "max_date": "", "trade_days": 0}
        if error_msg is None:
            error_msg = f"summary exception: {exc!r}"
            exit_code = 1

    try:
        marker = write_marker(
            exit_code=exit_code,
            elapsed_sec=elapsed,
            warehouse_summary=warehouse_summary,
            snapshot_summary=snapshot_summary,
            error=error_msg,
        )
        logger.info("marker written: %s exit_code=%d elapsed=%.1fs", marker, exit_code, elapsed)
    except Exception:
        logger.exception("write_marker failed (continuing)")
        # marker 写不出说明磁盘问题,直接判失败
        return 1

    logger.info("refresh_warehouse end | exit_code=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
