#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${QUANT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCK_PATH="$REPO_ROOT/logs/hermes_executor_handoff_wakeup.lock"
LOG_PATH="$REPO_ROOT/logs/hermes_executor_handoff_wakeup.log"
GATE_OUTPUT="$REPO_ROOT/logs/hermes_executor_handoff_gate.out"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
HERMES_TIMEOUT_SECONDS="${HERMES_TIMEOUT_SECONDS:-900}"

mkdir -p "$REPO_ROOT/logs"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  exit 0
fi

cd "$REPO_ROOT"
if ! python3 scripts/controller_owner_gate.py --repo-root "$REPO_ROOT" --action hermes_executor_handoff_owner_noop >> "$LOG_PATH" 2>&1; then
  exit 0
fi
python3 scripts/hermes_executor_handoff_tick.py > "$GATE_OUTPUT"

if ! grep -q "HERMES_QUANT_EXECUTOR_HANDOFF" "$GATE_OUTPUT"; then
  exit 0
fi

PROMPT="$(cat <<'PROMPT_EOF'
你是 Hermes 的 quant 执行代码工人。下面的脚本输出里有一张 executor-code handoff。

必须按顺序做：
1. 只处理脚本输出里的 handoff_id。
2. 先运行：python3 scripts/hermes_executor_handoff_tick.py --claim <handoff_id> --actor hermes
3. 读取脚本输出里的 descriptor_path 和 handoff_registry。
4. 使用文件写入工具写代码，不要用终端拼接大段代码，不要用 heredoc，不要用 cat > 文件。
5. 只允许写同一 run_dir 下 generated_executor/ 里的执行文件和 generated_executor/executor_completion.yaml。
6. 写完后，必须检查：执行文件能编译；有 main；有 declare_data_requirements；能写 summary.json、report.yaml、l4_ack.yaml、diagnostic.yaml。
7. 检查通过后，写 generated_executor/executor_completion.yaml，格式必须是原始 YAML：
   schema_version: 1
   handoff_id: <handoff_id>
   generated_executor: generated_executor/<implemented_evaluator>.py
   completed_by: hermes
   checks:
     compile_passed: true
     has_main: true
     has_declare_data_requirements: true
     writes_summary_json: true
     summary_has_adoption_pass: true
     writes_report_yaml: true
     writes_l4_ack_yaml: true
     writes_diagnostic_yaml: true
     no_forbidden_markers: true
8. 最后运行：python3 scripts/hermes_executor_handoff_tick.py --complete <handoff_id> --actor hermes
9. 到此停止。不要启动 quant 主流程，不要启动 VM。

禁止：
- 不准运行 scripts/quant_internal_tick.py。
- 不准运行 scripts/research_queue_runner.py。
- 不准启动 VM 或 spot。
- 不准改 research_queue.yaml、current.yaml、baseline_registry.yaml。
- 不准推进下一轮研究。

脚本输出：
PROMPT_EOF
)"

{
  echo "### $(date +%FT%T%z) hermes executor handoff wakeup"
  set +e
  timeout "$HERMES_TIMEOUT_SECONDS" "$HERMES_BIN" --yolo -z "$PROMPT

$(cat "$GATE_OUTPUT")"
  hermes_status=$?
  set -e
  echo "hermes_exit_status: $hermes_status"
  python3 scripts/hermes_executor_handoff_tick.py --finalize-claimed --actor hermes_wakeup
  echo
} >> "$LOG_PATH" 2>&1
