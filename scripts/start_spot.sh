#!/usr/bin/env bash
# Start Guangzhou spot through sig (the sig has the Tencent API credentials).
# Run from WSL or anywhere with ssh access to sig.
set -euo pipefail

SIG="${QUANT_SIG_HOST:-root@100.91.245.108}"
SIG_REPO="${QUANT_SIG_REPO:-/root/projects/quant}"

# Push the latest start_spot.py to sig if needed (so the wrapper stays
# in sync with this repo).
rsync -t -e "ssh -o BatchMode=yes -o ConnectTimeout=8" \
    "$(dirname "$0")/start_spot.py" \
    "$SIG:$SIG_REPO/scripts/start_spot.py"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$SIG" "bash -lc 'source /root/.tencent_secrets/cvm.env && python3 $SIG_REPO/scripts/start_spot.py'"
