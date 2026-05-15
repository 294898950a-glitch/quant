#!/bin/bash
# Install/uninstall git pre-commit hook for framework preflight (P1.3 spec).
#
# Usage:
#   bash scripts/install_pre_commit_hook.sh           # install
#   bash scripts/install_pre_commit_hook.sh --uninstall   # uninstall
#
# Hook behavior:
# - Triggers on commits touching: strategies/*, scripts/evaluate_cb_*, scripts/search_cb_*,
#   docs/research_framework/*, data/research_framework/*
# - Runs framework_preflight.py
# - exit 1 from preflight → block commit (use --no-verify to bypass)
# - dirty inventory warnings don't block

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

if [ "$1" = "--uninstall" ]; then
    if [ -f "$HOOK_PATH" ]; then
        rm "$HOOK_PATH"
        echo "Uninstalled pre-commit hook"
    else
        echo "No pre-commit hook to uninstall"
    fi
    exit 0
fi

cat > "$HOOK_PATH" <<'EOF'
#!/bin/bash
# Auto-installed by scripts/install_pre_commit_hook.sh
# Block commits that violate framework strict checks when touching strategy/research files

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

STAGED_FILES=$(git diff --cached --name-only)

NEEDS_PREFLIGHT=false
while IFS= read -r file; do
    case "$file" in
        strategies/*|scripts/evaluate_cb_*|scripts/search_cb_*|\
        docs/research_framework/*|data/research_framework/*)
            NEEDS_PREFLIGHT=true
            break
            ;;
    esac
done <<< "$STAGED_FILES"

if [ "$NEEDS_PREFLIGHT" = false ]; then
    exit 0
fi

echo "[pre-commit] Strategy/research files staged, running framework_preflight..."
if ! python3 scripts/framework_preflight.py --quiet; then
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 1 ]; then
        echo ""
        echo "[pre-commit] STRICT FAILURE in framework_preflight. Commit blocked."
        echo "  Fix the issues or bypass with --no-verify (not recommended)."
        exit 1
    fi
    # exit 2 = warnings only, allow commit
fi

exit 0
EOF

chmod +x "$HOOK_PATH"
echo "Installed pre-commit hook at $HOOK_PATH"
echo "Hook will:"
echo "  - Trigger on commits touching strategies/* / scripts/evaluate_cb_* / docs|data/research_framework/*"
echo "  - Run framework_preflight.py"
echo "  - Block on STRICT failures (exit 1)"
echo "  - Allow with warnings (exit 2)"
echo "  - Bypass with --no-verify"
