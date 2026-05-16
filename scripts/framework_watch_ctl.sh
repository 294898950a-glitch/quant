#!/bin/bash
# Framework watch daemon 控制脚本 (启动 / 停止 / 状态).
#
# 用法:
#   bash scripts/framework_watch_ctl.sh start
#   bash scripts/framework_watch_ctl.sh stop
#   bash scripts/framework_watch_ctl.sh status
#   bash scripts/framework_watch_ctl.sh restart

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DAEMON="$REPO_ROOT/scripts/framework_watch_daemon.py"
PID_FILE="$REPO_ROOT/.framework-watch.pid"
LOG_FILE="$REPO_ROOT/logs/framework_watch.log"

ACTION="${1:-status}"

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

case "$ACTION" in
    start)
        if is_running; then
            echo "framework_watch daemon 已在跑 (PID $(cat $PID_FILE))"
            exit 0
        fi
        mkdir -p "$REPO_ROOT/logs"
        nohup python3 "$DAEMON" > /dev/null 2>&1 &
        sleep 1
        if is_running; then
            echo "✓ framework_watch daemon 启动成功 (PID $(cat $PID_FILE))"
            echo "  log: $LOG_FILE"
            echo "  stop: bash scripts/framework_watch_ctl.sh stop"
        else
            echo "✗ daemon 启动失败"
            exit 1
        fi
        ;;
    stop)
        if is_running; then
            local_pid=$(cat "$PID_FILE")
            kill "$local_pid" 2>/dev/null
            sleep 1
            if is_running; then
                echo "⚠ kill 失败, 用 kill -9..."
                kill -9 "$local_pid" 2>/dev/null
            fi
            rm -f "$PID_FILE"
            echo "✓ daemon 已停止"
        else
            echo "framework_watch daemon 没在跑"
        fi
        ;;
    status)
        if is_running; then
            local_pid=$(cat "$PID_FILE")
            echo "✓ daemon 在跑 (PID $local_pid)"
            if [ -f "$LOG_FILE" ]; then
                echo "  最近 5 行 log:"
                tail -5 "$LOG_FILE" | sed 's/^/    /'
            fi
        else
            echo "✗ daemon 没在跑"
            echo "  启动: bash scripts/framework_watch_ctl.sh start"
        fi
        ;;
    restart)
        bash "$0" stop
        sleep 1
        bash "$0" start
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
