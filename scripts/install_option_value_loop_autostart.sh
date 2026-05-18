#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTL="$REPO_ROOT/scripts/option_value_loop_ctl.sh"
SERVICE_NAME="quant-option-value-loop"
START_CMD="cd '$REPO_ROOT' && bash '$CTL' start >/dev/null 2>&1 || true"

echo "Installing option value loop autostart for: $REPO_ROOT"

installed=false

if command -v systemctl >/dev/null 2>&1 && systemctl --user is-system-running >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Quant option value research loop

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
ExecStart=/usr/bin/python3 $REPO_ROOT/scripts/option_value_loop_daemon.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "${SERVICE_NAME}.service"
    echo "✓ Installed user systemd service: $SERVICE_FILE"
    installed=true
fi

if command -v powershell.exe >/dev/null 2>&1 && command -v wsl.exe >/dev/null 2>&1; then
    WIN_TASK_NAME="QuantOptionValueLoop"
    if powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "\$Action = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument 'bash -lc \"$START_CMD\"'; \$Trigger = New-ScheduledTaskTrigger -AtLogOn; Register-ScheduledTask -TaskName '$WIN_TASK_NAME' -Action \$Action -Trigger \$Trigger -Description 'Start quant option value research loop' -Force | Out-Null" >/dev/null 2>&1; then
        echo "✓ Installed/updated Windows logon task: $WIN_TASK_NAME"
        installed=true
    else
        echo "WARN: Windows scheduled task install failed; continuing with shell fallback."
    fi
fi

SNIPPET_BEGIN="# >>> quant option value loop autostart >>>"
SNIPPET_END="# <<< quant option value loop autostart <<<"
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
    echo "✓ option value loop autostart installed"
else
    echo "WARN: no autostart mechanism changed; loop was started for this session only"
fi
