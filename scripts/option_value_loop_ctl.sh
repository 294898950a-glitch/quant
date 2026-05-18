#!/bin/bash
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DAEMON="$REPO_ROOT/scripts/option_value_loop_daemon.py"
PID_FILE="$REPO_ROOT/.option-value-loop.pid"
LOG_FILE="$REPO_ROOT/logs/option_value_loop.log"
STATUS_FILE="$REPO_ROOT/logs/option_value_loop_status.json"
ACTION="${1:-status}"

is_running() {
  if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  return 1
}

case "$ACTION" in
  start)
    if is_running; then
      echo "option_value_loop 已在跑 (PID $(cat "$PID_FILE"))"
      exit 0
    fi
    mkdir -p "$REPO_ROOT/logs"
    if command -v setsid >/dev/null 2>&1; then
      setsid python3 "$DAEMON" >> "$LOG_FILE" 2>&1 < /dev/null &
    else
      nohup python3 "$DAEMON" >> "$LOG_FILE" 2>&1 < /dev/null &
    fi
    sleep 1
    if is_running; then
      echo "✓ option_value_loop 启动成功 (PID $(cat "$PID_FILE"))"
      echo "  log: $LOG_FILE"
    else
      echo "✗ option_value_loop 启动失败"
      exit 1
    fi
    ;;
  stop)
    if is_running; then
      pid="$(cat "$PID_FILE")"
      kill "$pid" 2>/dev/null || true
      sleep 1
      if is_running; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      rm -f "$PID_FILE"
      echo "✓ option_value_loop 已停止"
    else
      echo "option_value_loop 没在跑"
    fi
    ;;
  restart)
    bash "$0" stop
    sleep 1
    bash "$0" start
    ;;
  once)
    cd "$REPO_ROOT" && python3 "$DAEMON" --once
    ;;
  status)
    if is_running; then
      echo "✓ option_value_loop 在跑 (PID $(cat "$PID_FILE"))"
    else
      echo "✗ option_value_loop 没在跑"
    fi
    if [ -f "$STATUS_FILE" ]; then
      echo "  status: $STATUS_FILE"
      tail -20 "$STATUS_FILE" | sed 's/^/    /'
    fi
    if [ -f "$LOG_FILE" ]; then
      echo "  最近 10 行 log:"
      tail -10 "$LOG_FILE" | sed 's/^/    /'
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|once}"
    exit 2
    ;;
esac
