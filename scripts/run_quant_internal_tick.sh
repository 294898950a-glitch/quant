#!/usr/bin/env bash
set -euo pipefail

cd /home/jay/projects/quant
mkdir -p logs
/usr/bin/python3 scripts/quant_internal_tick.py >> logs/quant_internal_tick.cron.log 2>&1
