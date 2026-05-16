#!/bin/bash
# Claude Code PostToolUse hook wrapper for framework_doc_check.py
#
# Claude Code 把 tool call 信息 (JSON) 写到 stdin. 这里解析出 file_path,
# 调 framework_doc_check.py. 输出到 stderr 让 Claude Code 注入回 AI 会话.
#
# 装法: 在 .claude/settings.local.json 的 hooks.PostToolUse 里指向本脚本.

set -u

REPO_ROOT="/home/jay/projects/quant"

# 从 stdin 读 JSON 取 tool_input.file_path
INPUT="$(cat 2>/dev/null || true)"

# 提取 file_path (用 python, 不假设 jq 装了)
FILE_PATH=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    p = d.get('tool_input', {}).get('file_path', '')
    print(p)
except Exception:
    pass
" <<< "$INPUT" 2>/dev/null)

# 没 path 或非 framework 受管, 直接退出 (silent)
if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# 调 framework_doc_check, 错了输出到 stderr (Claude Code 注入 AI 会话)
python3 "$REPO_ROOT/scripts/framework_doc_check.py" "$FILE_PATH" --quiet 2>&1 1>/dev/null
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "" >&2
    echo "🔴 [framework_doc_check] $FILE_PATH 写完后验证失败 (exit $EXIT_CODE)" >&2
    echo "🔴 AI: 立即回到这个文件修, 不要带着错继续往下做." >&2
    # 按 Codex 01:26 review: Claude Code PostToolUse hook 必须 exit 2 (而不是
    # exit 0) 才能把 stderr 注入 AI 会话. exit 0 silent, AI 看不到; exit 2
    # 是 blocking feedback - tool 完成但 stderr 进下个 AI turn.
    exit 2
fi

exit 0
