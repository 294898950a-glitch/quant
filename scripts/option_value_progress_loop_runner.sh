#!/bin/bash
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$REPO_ROOT/.option-value-progress-loop.pid"
REPORTER="$REPO_ROOT/scripts/option_value_progress_reporter.py"
INTERVAL_SECONDS="${OPTION_VALUE_PROGRESS_INTERVAL_SECONDS:-600}"

cd "$REPO_ROOT" || exit 1
echo "$BASHPID" > "$PID_FILE"

while true; do
  date '+[%F %T %Z] progress heartbeat tick'
  python3 "$REPORTER"
  sleep "$INTERVAL_SECONDS"
done
