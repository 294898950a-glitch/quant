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
import os.path
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / "logs" / "framework_watch.log"
PID_FILE = REPO_ROOT / ".framework-watch.pid"

# 按用户 2026-05-17 简化: 整个 repo root 监控, framework_doc_check 自己 dispatch
# (非受管路径 silent skip). 这样新增目录自动覆盖, 不用维护 WATCHED_DIRS 列表.
WATCHED_DIRS = [REPO_ROOT]

# 跳过的"噪音"目录/路径模式 (相对 REPO_ROOT). 避免扫 .venv/__pycache__/.git/等
SKIP_DIR_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", ".idea", ".vscode", "logs", ".claude",
    "archive",  # 历史 archived data, 不算 active
}

# 跳过的文件 pattern (suffix)
SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".pid", ".lock", ".swp", ".tmp",
    ".parquet", ".csv",  # 数据文件, 不是 framework 文档
}

POLL_INTERVAL = 1.0  # seconds
EVENT_DEDUP_WINDOW = 5.0  # 同一 file 同 mtime 5 秒内不重复跑

# state
file_mtimes: dict[Path, float] = {}
initialized = False  # 启动后初次扫完成标志

stop_requested = False


def log(level: str, msg: str, notify: bool = False) -> None:
    """写 log line. notify=True 才触发 desktop notification (避免每行 FATAL 都 notify)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {level} {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if level == "FATAL":
        # 也写 stderr (前台跑用户能看到)
        print(line, file=sys.stderr)
    if notify:
        # 按 Codex 01:38 review: notify 每次 validation 只调一次 (不是每行 log)
        # + 没 notify-send / DBus 时不漏 stderr
        try:
            subprocess.run(
                ["notify-send", "-u", "critical", "framework_watch", msg],
                check=False, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


def signal_handler(signum, frame):
    global stop_requested
    log("INFO", f"received signal {signum}, stopping daemon")
    stop_requested = True


def scan_dir(d: Path) -> list[Path]:
    """递归扫一个目录所有 file, 跳过噪音目录/文件.

    按 Codex 01:43 review: 用 os.walk() 主动 prune 噪音目录 (不递归进去),
    rglob('*') 会先扫完所有再 filter, 在 .git/.venv 几百兆下慢到 1.5+ 秒.
    """
    if not d.exists():
        return []
    files = []
    for root, dirs, filenames in os.walk(d):
        # in-place 修改 dirs 来 prune (os.walk 不会再进去)
        dirs[:] = [x for x in dirs if x not in SKIP_DIR_PARTS and not x.startswith(".")]
        root_path = Path(root)
        for fname in filenames:
            # 跳噪音文件后缀
            ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""
            if ext in SKIP_SUFFIXES:
                continue
            # 跳隐藏文件
            if fname.startswith("."):
                continue
            files.append(root_path / fname)
    return files


def check_one(path: Path) -> None:
    """跑 framework_doc_check 检查一个 file. 按 Codex 01:38 review:
    dedup 改成 mtime-based, 同 path 不同 mtime 都跑 (快速连续修改不漏)."""
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

    # FATAL — 只第一行 log 触发 notify (Codex 01:38 review)
    rel = path.relative_to(REPO_ROOT)
    log("FATAL", f"{rel} 验证失败 (exit {result.returncode})", notify=True)
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


def main() -> int:
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log("INFO", f"daemon started (pid {os.getpid()}), watching: {[str(d) for d in WATCHED_DIRS]}")

    # 按 Codex 01:43 review: 先写 pid file 再 initial scan
    # (避免 ctl.sh start 误报"启动失败")
    PID_FILE.write_text(str(os.getpid()))

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
