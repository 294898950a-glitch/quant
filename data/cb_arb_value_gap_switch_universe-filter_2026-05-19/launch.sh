#!/bin/bash
# Launch script for universe-filter evaluator on guangzhou_spot
set -e

cd /home/ubuntu/projects/quant
OUTDIR="data/cb_arb_value_gap_switch_universe-filter_2026-05-19"
mkdir -p "$OUTDIR"

exec > "$OUTDIR/auto_pipeline.log" 2>&1
echo "[$(date -Iseconds)] Starting evaluate_cb_arb_universe_filter.py"

python3 scripts/evaluate_cb_arb_universe_filter.py \
    --data-root data/cb_arb_concurrent_supervised_20260511_094500 \
    --output-dir "$OUTDIR" \
    --cost-model-enabled

echo "[$(date -Iseconds)] Done. Exit code: $?"
