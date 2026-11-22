#!/bin/sh
# After pulling or editing either repo: install what changed, rebuild the UI, restart the service.
#   scripts/update.sh
set -eu
API=$(cd "$(dirname "$0")/.." && pwd)
WEB=${RADAR_WEB_DIR:-"$API/../guided-i18n-radar-web"}

echo "1/4 API dependencies"
(cd "$API" && uv sync -q)

echo "2/4 UI dependencies and build"
[ -d "$WEB" ] || { echo "no web repo at $WEB (set RADAR_WEB_DIR)"; exit 1; }
(cd "$WEB" && pnpm install --silent && pnpm build >/dev/null 2>&1) || { echo "UI build failed: run pnpm build in $WEB to see why"; exit 1; }

echo "3/4 restart the background service"
"$API/scripts/launch-agent.sh" install >/dev/null

echo "4/4 check"
i=0; until curl -sf --max-time 2 http://127.0.0.1:8000/health >/dev/null; do
  i=$((i+1)); [ $i -lt 20 ] || { echo "API didn't come back; see ~/Library/Logs/guided-i18n-radar.log"; exit 1; }; sleep 1
done
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/)
[ "$code" = 200 ] && echo "done: http://127.0.0.1:8000" || { echo "API is up but the UI returned $code"; exit 1; }
