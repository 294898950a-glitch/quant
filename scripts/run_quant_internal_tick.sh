#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${QUANT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"
mkdir -p logs
/usr/bin/python3 scripts/quant_internal_tick.py >> logs/quant_internal_tick.cron.log 2>&1
