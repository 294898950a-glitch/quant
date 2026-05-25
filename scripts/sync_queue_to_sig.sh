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

# Drop -t so the destination file gets a fresh mtime on every sync, even
# when content is unchanged. spot_idle_shutdown.py on sig uses file mtime
# to decide whether the queue is "fresh enough to trust" (max_age = 20 min);
# preserving source mtime caused the guard to lock into keep_on whenever
# the queue went idle for >20 minutes (e.g. while ideation was thinking).
rsync \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=8" \
  data/research_framework/research_queue.yaml \
  "$SIG_HOST:$SIG_REPO/data/research_framework/research_queue.yaml"

# Belt-and-suspenders: explicitly touch the remote file so its mtime
# reflects "we just confirmed this was the latest copy", regardless of
# whether rsync transferred bytes.
ssh -o BatchMode=yes -o ConnectTimeout=8 "$SIG_HOST" \
  "touch $SIG_REPO/data/research_framework/research_queue.yaml"

# Also synchronize the orchestrator pause flag so spot_idle_start.py on
# sig can honour it. Without this, the dispatcher (wsl) can be paused
# while sig still sees queued tasks and auto-starts spot, producing the
# "queue frozen but spot empty-spinning" deadlock observed during the
# 2026-05-26 incident response. If the flag is absent locally, remove
# it on the remote so a fresh recovery propagates correctly.
LOCAL_FLAG="data/research_framework/orchestrator_paused.flag"
REMOTE_FLAG="$SIG_REPO/data/research_framework/orchestrator_paused.flag"
if [[ -f "$LOCAL_FLAG" ]]; then
  rsync \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=8" \
    "$LOCAL_FLAG" \
    "$SIG_HOST:$REMOTE_FLAG"
  echo "[$TS] synced orchestrator_paused.flag to $SIG_HOST"
else
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$SIG_HOST" \
    "rm -f $REMOTE_FLAG"
  echo "[$TS] removed orchestrator_paused.flag on $SIG_HOST (local absent)"
fi

echo "[$TS] synced research_queue.yaml to $SIG_HOST"
