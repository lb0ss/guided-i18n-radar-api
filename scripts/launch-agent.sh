#!/bin/sh
# Start Radar at login and keep it running (macOS launchd). Jobs run while the Mac is awake;
# a job missed during sleep runs once on wake.
#   scripts/launch-agent.sh install | uninstall | status
set -eu
LABEL=dev.guided-i18n.radar
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/guided-i18n-radar.log"
DIR=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || true)

case "${1:-status}" in
install)
  [ -n "$UV" ] || { echo "uv not found on PATH"; exit 1; }
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$UV</string><string>run</string><string>radar</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "$UV"):/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  i=0; while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ $i -lt 20 ]; do sleep 0.5; i=$((i+1)); done
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed: Radar starts at login (http://127.0.0.1:8000, log $LOG)" ;;
uninstall)
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed" ;;
status)
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state =|pid =" || echo "not installed"
  curl -s --max-time 3 http://127.0.0.1:8000/health && echo || echo "API not answering" ;;
*) echo "usage: $0 install | uninstall | status"; exit 1 ;;
esac
