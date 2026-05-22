#!/usr/bin/env bash
# Sync the local research_queue.yaml to the Singapore sig VM so that
# spot_idle_shutdown.py (which runs on sig from cron) sees a fresh queue
# file and can correctly decide whether the Guangzhou spot is idle.
#
# This script does NOT change strategy truth, does NOT start jobs, and
# does NOT touch verifier / cost_model / baseline_registry.
#
# Triggered by WSL cron tag: QUANT_SYNC_QUEUE_TO_SIG

set -euo pipefail

REPO_ROOT="${QUANT_REPO_ROOT:-/home/jay/projects/quant}"
SIG_HOST="${QUANT_SIG_HOST:-root@100.91.245.108}"
SIG_REPO="${QUANT_SIG_REPO:-/root/projects/quant}"
TS="$(date -u +%FT%TZ)"

cd "$REPO_ROOT"

rsync -t \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=8" \
  data/research_framework/research_queue.yaml \
  "$SIG_HOST:$SIG_REPO/data/research_framework/research_queue.yaml"

echo "[$TS] synced research_queue.yaml to $SIG_HOST"
