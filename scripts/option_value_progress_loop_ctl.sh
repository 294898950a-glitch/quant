#!/bin/bash
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$REPO_ROOT/.option-value-progress-loop.pid"
LOG_FILE="$REPO_ROOT/logs/option_value_progress_loop.log"
RUNNER="$REPO_ROOT/scripts/option_value_progress_loop_runner.sh"
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

start_loop() {
  mkdir -p "$REPO_ROOT/logs"
  if command -v setsid >/dev/null 2>&1; then
    setsid bash "$RUNNER" >> "$LOG_FILE" 2>&1 < /dev/null &
  else
    nohup bash "$RUNNER" >> "$LOG_FILE" 2>&1 < /dev/null &
  fi
}

case "$ACTION" in
  start)
    if is_running; then
      echo "option_value_progress_loop 已在跑 (PID $(cat "$PID_FILE"))"
      exit 0
    fi
    start_loop
    sleep 1
    if is_running; then
      echo "✓ option_value_progress_loop 启动成功 (PID $(cat "$PID_FILE"))"
      echo "  log: $LOG_FILE"
    else
      echo "✗ option_value_progress_loop 启动失败"
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
      echo "✓ option_value_progress_loop 已停止"
    else
      echo "option_value_progress_loop 没在跑"
    fi
    ;;
  restart)
    bash "$0" stop
    sleep 1
    bash "$0" start
    ;;
  status)
    if is_running; then
      echo "✓ option_value_progress_loop 在跑 (PID $(cat "$PID_FILE"))"
    else
      echo "✗ option_value_progress_loop 没在跑"
    fi
    if [ -f "$LOG_FILE" ]; then
      echo "  最近 10 行 log:"
      tail -10 "$LOG_FILE" | sed 's/^/    /'
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 2
    ;;
esac
