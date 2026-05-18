#!/usr/bin/env bash
set -u

VM_HOST="${VM_HOST:-root@100.91.245.108}"
VM_PROXY_HOST="${VM_PROXY_HOST:-}"
VM_PROXY_REPO="${VM_PROXY_REPO:-/root/projects/quant}"
VM_IDENTITY_FILE_ON_PROXY="${VM_IDENTITY_FILE_ON_PROXY:-}"
LOCAL_REPO="${LOCAL_REPO:-/home/jay/projects/quant}"
REMOTE_REPO="${REMOTE_REPO:-/root/projects/quant}"
AI_ROOT="${AI_ROOT:-/mnt/c/Users/陈教授/Desktop/ai}"
POLL_SECONDS="${POLL_SECONDS:-60}"

TASK_NAME=""
REMOTE_DIR=""
LOCAL_DIR=""
PROCESS_PATTERN=""
LOG_FILE_NAME="run.log"
DONE_MARKER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --task-name) TASK_NAME="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --local-dir) LOCAL_DIR="$2"; shift 2 ;;
    --process-pattern) PROCESS_PATTERN="$2"; shift 2 ;;
    --log-file) LOG_FILE_NAME="$2"; shift 2 ;;
    --done-marker) DONE_MARKER="$2"; shift 2 ;;
    --poll-seconds) POLL_SECONDS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TASK_NAME" ] || [ -z "$REMOTE_DIR" ] || [ -z "$LOCAL_DIR" ] || [ -z "$PROCESS_PATTERN" ]; then
  echo "usage: $0 --task-name NAME --remote-dir DIR --local-dir DIR --process-pattern PATTERN [--done-marker TEXT]" >&2
  exit 2
fi

SAFE_TASK="$(printf '%s' "$TASK_NAME" | tr -c 'A-Za-z0-9_.-' '_')"
PID_FILE="/tmp/quant_vm_task_${SAFE_TASK}.pid"
MONITOR_LOG="/tmp/quant_vm_task_${SAFE_TASK}.log"
LOCK_DIR="/tmp/quant_vm_task_${SAFE_TASK}.lock"
DONE_FILE="/tmp/quant_vm_task_${SAFE_TASK}.done"

if [ -f "$PID_FILE" ]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$existing_pid" ] && [ "$existing_pid" != "$$" ] && kill -0 "$existing_pid" 2>/dev/null; then
    existing_cmd="$(tr '\0' ' ' < "/proc/$existing_pid/cmdline" 2>/dev/null || true)"
    if printf '%s' "$existing_cmd" | grep -q "watch_quant_vm_task_completion.sh" && printf '%s' "$existing_cmd" | grep -q -- "--task-name $TASK_NAME"; then
      printf '[%s] duplicate monitor skipped; task=%s existing_pid=%s new_pid=%s\n' "$(date '+%F %T %Z')" "$TASK_NAME" "$existing_pid" "$$" >> "$MONITOR_LOG"
      exit 0
    fi
  fi
fi

echo "$$" > "$PID_FILE"

log() {
  printf '[%s] %s\n' "$(date '+%F %T %Z')" "$*" >> "$MONITOR_LOG"
}

remote_log_path="$REMOTE_REPO/$REMOTE_DIR/$LOG_FILE_NAME"
local_abs_dir="$LOCAL_REPO/$LOCAL_DIR"

remote_exec() {
  local quoted_cmd
  quoted_cmd="$(printf '%q' "$1")"
  if [ -n "$VM_PROXY_HOST" ]; then
    if [ -z "$VM_IDENTITY_FILE_ON_PROXY" ]; then
      echo "VM_IDENTITY_FILE_ON_PROXY is required when VM_PROXY_HOST is set" >&2
      return 2
    fi
    ssh -n "$VM_PROXY_HOST" \
      "ssh -n -i '$VM_IDENTITY_FILE_ON_PROXY' -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 '$VM_HOST' bash -lc $quoted_cmd"
  else
    ssh -n "$VM_HOST" "bash -lc $quoted_cmd"
  fi
}

is_complete() {
  remote_exec "cd '$REMOTE_REPO' && \
    running=\$(pgrep -af '$PROCESS_PATTERN' | grep -v pgrep || true); \
    has_top=0; has_wrote=0; has_files=0; \
    if [ -f '$remote_log_path' ] && grep -q '^TOP$' '$remote_log_path'; then has_top=1; fi; \
    if [ -f '$remote_log_path' ] && grep -q '\\[leave-year\\] wrote\\|\\[yearly\\] wrote\\|wrote ' '$remote_log_path'; then has_wrote=1; fi; \
    if [ -n '$DONE_MARKER' ] && [ -f '$remote_log_path' ] && grep -q \"$DONE_MARKER\" '$remote_log_path'; then has_wrote=1; fi; \
    if [ -s '$REMOTE_REPO/$REMOTE_DIR/leave_year_out_summary.csv' ] || [ -s '$REMOTE_REPO/$REMOTE_DIR/yearly_decomposition.csv' ] || [ -s '$REMOTE_REPO/$REMOTE_DIR/summary.csv' ] || [ -s '$REMOTE_REPO/$REMOTE_DIR/summary.json' ]; then has_files=1; fi; \
    printf 'running=%s has_top=%s has_wrote=%s has_files=%s\n' \"\${running:+1}\" \"\$has_top\" \"\$has_wrote\" \"\$has_files\""
}

process_completion() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "completion processor already running"
    return
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' RETURN

  mkdir -p "$local_abs_dir"
  log "sync remote result summaries: $VM_HOST:$REMOTE_REPO/$REMOTE_DIR/ -> $local_abs_dir/"
  rsync_filter=(
    --include='*/'
    --include='*.yaml'
    --include='*.json'
    --include='*.csv'
    --include='*.log'
    --include='*.txt'
    --exclude='*'
  )
  if [ -n "$VM_PROXY_HOST" ]; then
    safe_stage="$(printf '%s' "$SAFE_TASK" | tr -c 'A-Za-z0-9_.-' '_')"
    stage="/tmp/quant_vm_task_${safe_stage}_sync"
    ssh "$VM_PROXY_HOST" "rm -rf '$stage' && mkdir -p '$stage' && rsync -av --include='*/' --include='*.yaml' --include='*.json' --include='*.csv' --include='*.log' --include='*.txt' --exclude='*' -e \"ssh -i '$VM_IDENTITY_FILE_ON_PROXY' -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new\" '$VM_HOST:$REMOTE_REPO/$REMOTE_DIR/' '$stage/'" >> "$MONITOR_LOG" 2>&1
    rsync -av "${rsync_filter[@]}" "$VM_PROXY_HOST:$stage/" "$local_abs_dir/" >> "$MONITOR_LOG" 2>&1
  else
    rsync -av "${rsync_filter[@]}" "$VM_HOST:$REMOTE_REPO/$REMOTE_DIR/" "$local_abs_dir/" >> "$MONITOR_LOG" 2>&1
  fi

  local ts run_log last_msg
  ts="$(date '+%Y%m%d_%H%M%S')"
  run_log="/tmp/quant_vm_task_${SAFE_TASK}_${ts}.codex.log"
  last_msg="/tmp/quant_vm_task_${SAFE_TASK}_${ts}.final.txt"
  log "launch codex exec for completed VM task; run_log=$run_log"
  (
    cd "$LOCAL_REPO" || exit 1
    codex exec \
      --cd "$LOCAL_REPO" \
      --sandbox danger-full-access \
      --output-last-message "$last_msg" \
      - <<PROMPT
You are Codex running from the quant VM task completion monitor.

Scope:
- Project is exactly quant.
- Communication files:
  - $AI_ROOT/projects/quant/codex/outbox.md
  - $AI_ROOT/projects/quant/claude/outbox.md
  - $AI_ROOT/projects/quant/state.md
- Do not read or update other projects.
- Worktree may be dirty; do not revert user or generated changes.

Completed VM task:
- Task name: $TASK_NAME
- Remote dir: $REMOTE_REPO/$REMOTE_DIR
- Local synced dir: $LOCAL_DIR
- Remote log file: $remote_log_path

Task:
1. Inspect the synced result files under $LOCAL_DIR.
2. Summarize the result concisely.
3. Append a HANDOFF/REVIEW to quant codex/outbox.md for Claude.
4. Update quant state.md.
5. If the next agreed action is obvious and safe, start it on VM; otherwise state the next waiting point.
PROMPT
  ) > "$run_log" 2>&1
  local status=$?
  log "codex exec finished status=$status; final=$last_msg"
}

log "VM task monitor started; pid=$$; task=$TASK_NAME; poll=${POLL_SECONDS}s; remote_dir=$REMOTE_DIR"

while true; do
  if [ -f "$DONE_FILE" ]; then
    log "done file exists; exiting"
    exit 0
  fi

  status="$(is_complete 2>&1 || true)"
  log "status: $status"

  running="$(printf '%s' "$status" | sed -n 's/.*running=\([^ ]*\).*/\1/p')"
  has_wrote="$(printf '%s' "$status" | sed -n 's/.*has_wrote=\([^ ]*\).*/\1/p')"
  has_files="$(printf '%s' "$status" | sed -n 's/.*has_files=\([^ ]*\).*/\1/p')"

  if [ -z "$running" ] && [ "$has_wrote" = "1" ] && [ "$has_files" = "1" ]; then
    log "completion detected"
    process_completion
    date '+%F %T %Z' > "$DONE_FILE"
    log "marked done; exiting"
    exit 0
  fi

  sleep "$POLL_SECONDS"
done
