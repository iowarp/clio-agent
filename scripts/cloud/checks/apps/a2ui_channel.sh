#!/bin/bash
# Live run of AQ27-AQ29 (clio.mcp-app.v1 state channels) on a running stack.
#
#   AGENT_PORT=88NN UI_PORT=52NN bash scripts/cloud/checks/apps/a2ui_channel.sh
#   CHECK_SESSION_PATH=/workspaces/<ws>/sessions/<sid> WIDTH=390 HEIGHT=844 ... # reuse
#
# Saves the channel fixture server (tests/fixtures/mcp_apps/channel_app_server.py) as
# the connection "timeline", logging every call to .cloud-run/channel_app.log, then runs
# scripts/cloud/checks/apps/a2ui_channel.mjs at WIDTH x HEIGHT (default 1440x900) and
# prints the fixture's calls.
set -eu
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
AGENT="http://127.0.0.1:${AGENT_PORT:?}"
UI="http://127.0.0.1:${UI_PORT:?}/"
LOG="$ROOT/.cloud-run/channel_app.log"
WIDTH="${WIDTH:-1440}"
HEIGHT="${HEIGHT:-900}"
LABEL="${LABEL:-a2ui-channel-$WIDTH}"
touch "$LOG"

body=$(python3 - "$ROOT" "$LOG" <<'EOF'
import json, sys
root, log = sys.argv[1:3]
print(json.dumps({
    "transport": "stdio",
    "command": f"{root}/.venv/bin/python",
    "args": [f"{root}/tests/fixtures/mcp_apps/channel_app_server.py"],
    "env": {"CHANNEL_APP_LOG": log},
    "always_load": True,
    "name": "Timeline",
}))
EOF
)
# Saving the connection again replaces its running server, so an existing one is kept.
answer=$(curl -s "$AGENT/v1/mcp/configuration/timeline")
for _ in $(seq 1 30); do
  echo "$answer" | grep -q '"status":"ready"' && break
  answer=$(curl -s -X PUT "$AGENT/v1/mcp/configuration/timeline" \
    -H 'content-type: application/json' -d "$body")
  echo "$answer" | grep -q '"status":"ready"' || sleep 5
done
echo "$answer" | python3 -c 'import json,sys; r=json.load(sys.stdin); print("timeline:", r.get("status"), r.get("tools"))'

lines_before=$(wc -l < "$LOG")
AGENT_PORT="${AGENT_PORT}" node "$ROOT/scripts/cloud/ui_check.mjs" --agent "$AGENT" --ui "$UI" \
  --width "$WIDTH" --height "$HEIGHT" \
  --steps "$ROOT/scripts/cloud/checks/apps/a2ui_channel.mjs" --label "$LABEL"
echo "fixture calls during this run:"
tail -n +"$((lines_before + 1))" "$LOG"
