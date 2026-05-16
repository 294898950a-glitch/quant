#!/usr/bin/env bash
set -u

PROJECT_ID="quant"
REPO="/home/jay/projects/quant"
AI_ROOT="/mnt/c/Users/陈教授/Desktop/ai"
CLAUDE_BOX="$AI_ROOT/projects/$PROJECT_ID/claude/outbox.md"
CODEX_BOX="$AI_ROOT/projects/$PROJECT_ID/codex/outbox.md"
STATE_FILE="$AI_ROOT/projects/$PROJECT_ID/state.md"

LOG="/tmp/quant_claude_processor.log"
PID_FILE="/tmp/quant_claude_processor.pid"
LOCK_DIR="/tmp/quant_claude_processor.lock"
LAST_SIG_FILE="/tmp/quant_claude_processor.last"
LAST_CODEX_SIG_FILE="/tmp/quant_codex_outbox_guard.last"
POLL_SECONDS="${POLL_SECONDS:-15}"
PREFLIGHT_SCRIPT="$REPO/scripts/outbox_protocol_preflight.py"
MISROUTE_SCRIPT="$REPO/scripts/check_quant_outbox_misroute.py"
PROTOCOL_DOC="$REPO/data/research_framework/protocol_rules.yaml"
PROCESSED_CACHE="$REPO/data/research_framework/processed_claude_messages.jsonl"
MISROUTE_CACHE="$REPO/data/research_framework/misrouted_claude_messages.jsonl"
LEDGER="$REPO/data/research_framework/experiments.yaml"
DISAGREEMENT_COUNTERS="$REPO/data/research_framework/disagreement_counters.json"

echo "$$" > "$PID_FILE"

signature() {
  local path="${1:-$CLAUDE_BOX}"
  if [ ! -f "$path" ]; then
    printf 'missing\n'
    return
  fi
  stat -c '%Y:%s' "$path"
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T %Z')" "$*" >> "$LOG"
}

run_processor() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "processor already running; skip this change"
    return
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' RETURN

  if [ -x "$PREFLIGHT_SCRIPT" ] || [ -f "$PREFLIGHT_SCRIPT" ]; then
    python3 "$PREFLIGHT_SCRIPT" check \
      --claude-box "$CLAUDE_BOX" \
      --codex-box "$CODEX_BOX" \
      --state-file "$STATE_FILE" \
      --protocol-doc "$PROTOCOL_DOC" \
      --repo "$REPO" \
      --ledger "$LEDGER" \
      --disagreement-counters "$DISAGREEMENT_COUNTERS" \
      --cache "$PROCESSED_CACHE" \
      --emit-handoff >> "$LOG" 2>&1
    local preflight_status=$?
    if [ "$preflight_status" -eq 10 ]; then
      log "preflight skipped already-processed/latest-non-Claude message"
      return
    fi
    if [ "$preflight_status" -eq 20 ]; then
      log "preflight wrote handoff; codex exec not launched"
      return
    fi
    if [ "$preflight_status" -ne 0 ]; then
      log "preflight failed status=$preflight_status; codex exec not launched"
      return
    fi
  else
    log "missing preflight script: $PREFLIGHT_SCRIPT"
    return
  fi

  local ts
  ts="$(date '+%Y%m%d_%H%M%S')"
  local run_log="/tmp/quant_claude_processor_${ts}.log"
  local last_msg="/tmp/quant_claude_processor_${ts}.final.txt"

  log "launch codex exec; run_log=$run_log"
  (
    cd "$REPO" || exit 1
    codex exec \
      --cd "$REPO" \
      --sandbox danger-full-access \
      --output-last-message "$last_msg" \
      - <<PROMPT
You are Codex running from the quant Claude outbox processor.

Scope:
- Project is exactly: quant.
- Read only these communication files unless repo execution requires local quant files:
  - $CLAUDE_BOX
  - $CODEX_BOX
  - $STATE_FILE
- Do not read or update tax/sitecheck or root legacy outboxes.
- Worktree may be dirty; do not revert user or generated changes.
- Heavy backtests must run on VM root@100.91.245.108, not local.
- For ACK/state updates, prefer repo tool:
  python3 scripts/process_quant_claude_outbox.py --mode REVIEW --status <STATUS> --task <TASK> --gate <GATE>

Task:
1. Read the latest message in $CLAUDE_BOX.
2. Decide whether it is already acknowledged in $CODEX_BOX / $STATE_FILE.
3. If not acknowledged, append a concise ACK/REVIEW/HANDOFF to $CODEX_BOX and update $STATE_FILE.
4. If Claude requested a concrete next action that is safe and already agreed, start or continue it.
5. Keep responses quant-only and avoid duplicate work.

Current user intent:
- User asked to add the second processing layer so Claude outbox updates are handled automatically.
- If the latest Claude message only confirms prior state, acknowledge it and do not invent work.
PROMPT
  ) > "$run_log" 2>&1
  local status=$?
  log "codex exec finished status=$status; final=$last_msg"
  if [ "$status" -eq 0 ]; then
    python3 "$PREFLIGHT_SCRIPT" mark \
      --claude-box "$CLAUDE_BOX" \
      --cache "$PROCESSED_CACHE" >> "$LOG" 2>&1 || \
      log "failed to mark processed Claude message"
  fi
}

check_codex_misroute() {
  if [ ! -f "$MISROUTE_SCRIPT" ]; then
    log "missing misroute guard script: $MISROUTE_SCRIPT"
    return
  fi
  python3 "$MISROUTE_SCRIPT" \
    --codex-box "$CODEX_BOX" \
    --state-file "$STATE_FILE" \
    --protocol-doc "$PROTOCOL_DOC" \
    --cache "$MISROUTE_CACHE" >> "$LOG" 2>&1
  local status=$?
  if [ "$status" -eq 20 ]; then
    log "misrouted Claude message alert appended"
  elif [ "$status" -ne 0 ] && [ "$status" -ne 10 ]; then
    log "misroute guard failed status=$status"
  fi
}

baseline="$(signature "$CLAUDE_BOX")"
codex_baseline="$(signature "$CODEX_BOX")"
printf '%s\n' "$baseline" > "$LAST_SIG_FILE"
printf '%s\n' "$codex_baseline" > "$LAST_CODEX_SIG_FILE"
log "processor monitor started; pid=$$; baseline=$baseline; codex_baseline=$codex_baseline; poll=${POLL_SECONDS}s; box=$CLAUDE_BOX"

while true; do
  sleep "$POLL_SECONDS"
  codex_current="$(signature "$CODEX_BOX")"
  codex_previous="$(cat "$LAST_CODEX_SIG_FILE" 2>/dev/null || printf 'missing\n')"
  if [ "$codex_current" != "$codex_previous" ]; then
    log "codex outbox change detected: $codex_previous -> $codex_current"
    printf '%s\n' "$codex_current" > "$LAST_CODEX_SIG_FILE"
    if [ "$codex_current" != "missing" ]; then
      check_codex_misroute
    fi
  fi

  current="$(signature "$CLAUDE_BOX")"
  previous="$(cat "$LAST_SIG_FILE" 2>/dev/null || printf 'missing\n')"
  if [ "$current" != "$previous" ]; then
    log "change detected: $previous -> $current"
    printf '%s\n' "$current" > "$LAST_SIG_FILE"
    if [ "$current" != "missing" ]; then
      run_processor
    fi
  fi
done
