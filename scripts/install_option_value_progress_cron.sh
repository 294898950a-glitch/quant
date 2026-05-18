#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOCK_DIR/option_value_progress_reporter.log"
CRON_MARKER="quant-option-value-progress"
CRON_LINE="*/10 * * * * cd '$REPO_ROOT' && mkdir -p '$LOCK_DIR' && flock -n '$LOCK_DIR/option_value_progress_reporter.lock' python3 scripts/option_value_progress_reporter.py >> '$LOG_FILE' 2>&1 # $CRON_MARKER"

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab not found"
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

crontab -l 2>/dev/null | grep -v "$CRON_MARKER" > "$tmp" || true
echo "$CRON_LINE" >> "$tmp"
crontab "$tmp"

echo "✓ installed cron: $CRON_LINE"
