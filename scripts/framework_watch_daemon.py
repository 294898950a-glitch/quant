#!/usr/bin/env python3
"""Framework file watch daemon (cross-AI 实时检查).

按用户 2026-05-17 提出 Claude Code hook 不够通用 - 换 Codex / Cursor / Aider /
任何其他 AI 都跑不了. 真硬约束应该在 OS 文件系统层, 不绑定单一 AI 工具.

本 daemon 后台跑, 监控受管目录 mtime 变化, 任意文件修改 (Claude Code / Codex /
Cursor / vim / nano / 任何编辑器) 都触发 framework_doc_check.py.

输出:
- 错了 → 写到 logs/framework_watch.log (任何 AI 看 log 都能知道) +
  desktop notification (notify-send 装了才弹)
- OK → silent (不刷屏)

启动 (后台):
  nohup python3 scripts/framework_watch_daemon.py > /dev/null 2>&1 &
  echo $! > .framework-watch.pid

停止:
  kill $(cat .framework-watch.pid) && rm .framework-watch.pid

设计:
- 纯 Python, 无 watchdog/inotify 依赖 (用 mtime polling)
- 1 秒扫一遍受管路径
- 跟踪 mtime, 新/改的 file 跑 framework_doc_check
- 仅记录上 5 分钟的事件 (防重复刷 log)

不依赖任何特定 AI 工具的 hook 机制. cross-AI.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / "logs" / "framework_watch.log"
PID_FILE = REPO_ROOT / ".framework-watch.pid"

WATCHED_DIRS = [
    REPO_ROOT / "reports",
    REPO_ROOT / "data",
    REPO_ROOT / "docs" / "research_framework",
]

POLL_INTERVAL = 1.0  # seconds
EVENT_DEDUP_WINDOW = 5.0  # 同一 file 同 mtime 5 秒内不重复跑

# state
file_mtimes: dict[Path, float] = {}
last_check_at: dict[Path, float] = {}
initialized = False  # 启动后初次扫完成标志

stop_requested = False


def log(level: str, msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {level} {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if level == "FATAL":
        # Desktop notification (仅 Linux + notify-send 装了)
        try:
            subprocess.run(
                ["notify-send", "-u", "critical", "framework_watch", msg],
                check=False, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # 也写 stderr (如果 daemon 在前台跑用户能看到)
        print(line, file=sys.stderr)


def signal_handler(signum, frame):
    global stop_requested
    log("INFO", f"received signal {signum}, stopping daemon")
    stop_requested = True


def scan_dir(d: Path) -> list[Path]:
    """递归扫一个目录所有 file, 返回 list."""
    if not d.exists():
        return []
    files = []
    for path in d.rglob("*"):
        if path.is_file() and not any(p.startswith(".") for p in path.parts):
            files.append(path)
    return files


def check_one(path: Path) -> None:
    """跑 framework_doc_check 检查一个 file."""
    now = time.time()
    last = last_check_at.get(path, 0.0)
    if now - last < EVENT_DEDUP_WINDOW:
        return
    last_check_at[path] = now

    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "framework_doc_check.py"),
        str(path),
        "--quiet",
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode == 0:
        # skip 或 OK 都不 log
        return

    # FATAL
    log("FATAL", f"{path.relative_to(REPO_ROOT)} 验证失败 (exit {result.returncode})")
    if result.stdout:
        for line in result.stdout.splitlines():
            log("FATAL", f"  stdout: {line}")
    if result.stderr:
        for line in result.stderr.splitlines():
            log("FATAL", f"  stderr: {line}")


def poll() -> None:
    """扫一遍受管目录, 检查 mtime 变化."""
    global initialized
    for d in WATCHED_DIRS:
        for path in scan_dir(d):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            prev = file_mtimes.get(path)
            if prev is None:
                # 启动前已有的 file: 记 mtime 不跑; 启动后新出现的 file: 跑
                file_mtimes[path] = mtime
                if initialized:
                    check_one(path)
                continue
            if mtime > prev:
                file_mtimes[path] = mtime
                check_one(path)

    # 清理消失的 file (避免内存膨胀)
    for path in list(file_mtimes.keys()):
        if not path.exists():
            del file_mtimes[path]
            last_check_at.pop(path, None)


def main() -> int:
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log("INFO", f"daemon started (pid {os.getpid()}), watching: {[str(d) for d in WATCHED_DIRS]}")

    # 初次扫: 记 mtime 不跑 (启动时已有 file 不算 modification)
    global initialized
    for d in WATCHED_DIRS:
        for path in scan_dir(d):
            try:
                file_mtimes[path] = path.stat().st_mtime
            except FileNotFoundError:
                continue
    initialized = True
    log("INFO", f"initial scan: {len(file_mtimes)} files indexed, watching for new/modified")

    # 写 pid file
    PID_FILE.write_text(str(os.getpid()))

    try:
        while not stop_requested:
            poll()
            time.sleep(POLL_INTERVAL)
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()
        log("INFO", "daemon stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
