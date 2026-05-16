#!/bin/bash
# Install cross-AI framework watch autostart.
#
# Goal: the watcher should be alive because the machine/project environment
# starts it, not because an AI remembers to run it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTL="$REPO_ROOT/scripts/framework_watch_ctl.sh"
SERVICE_NAME="quant-framework-watch"
START_CMD="cd '$REPO_ROOT' && bash '$CTL' start >/dev/null 2>&1 || true"

echo "Installing framework watch autostart for: $REPO_ROOT"

installed=false

if command -v systemctl >/dev/null 2>&1 && systemctl --user is-system-running >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Quant framework watch daemon

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
ExecStart=/usr/bin/python3 $REPO_ROOT/scripts/framework_watch_daemon.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "${SERVICE_NAME}.service"
    echo "✓ Installed user systemd service: $SERVICE_FILE"
    installed=true
fi

if command -v powershell.exe >/dev/null 2>&1 && command -v wsl.exe >/dev/null 2>&1; then
    # Windows login task. Use default WSL distro; the repo path is inside this WSL user.
    WIN_TASK_NAME="QuantFrameworkWatch"
    WIN_CMD="wsl.exe bash -lc \"$START_CMD\""
    if powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "\$Action = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument 'bash -lc \"$START_CMD\"'; \$Trigger = New-ScheduledTaskTrigger -AtLogOn; Register-ScheduledTask -TaskName '$WIN_TASK_NAME' -Action \$Action -Trigger \$Trigger -Description 'Start quant framework watch daemon' -Force | Out-Null" >/dev/null 2>&1; then
        echo "✓ Installed/updated Windows logon task: $WIN_TASK_NAME"
        installed=true
    else
        echo "WARN: Windows scheduled task install failed; continuing with shell fallback."
    fi
fi

# Shell fallback for environments without systemd/Task Scheduler. This is not the
# primary guarantee, but it recovers the watcher when a shell enters this repo.
SNIPPET_BEGIN="# >>> quant framework watch autostart >>>"
SNIPPET_END="# <<< quant framework watch autostart <<<"
SNIPPET="$SNIPPET_BEGIN
case \"\$PWD\" in
  $REPO_ROOT|$REPO_ROOT/*)
    bash '$CTL' start >/dev/null 2>&1 || true
    ;;
esac
$SNIPPET_END"

for profile in "$HOME/.bashrc" "$HOME/.profile"; do
    touch "$profile"
    if ! grep -Fq "$SNIPPET_BEGIN" "$profile"; then
        {
            echo ""
            echo "$SNIPPET"
        } >> "$profile"
        echo "✓ Added shell fallback to $profile"
        installed=true
    else
        echo "✓ Shell fallback already present in $profile"
        installed=true
    fi
done

bash "$CTL" start

if [ "$installed" = true ]; then
    echo "✓ framework watch autostart installed"
else
    echo "WARN: no autostart mechanism changed; watcher was started for this session only"
fi
