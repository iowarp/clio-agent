#!/bin/bash
# Live run of AQ27-AQ29 (clio.mcp-app.v1 state channels) on a running stack.
#
#   AGENT_PORT=88NN UI_PORT=52NN bash scripts/cloud/checks/apps/a2ui_channel.sh
#
# 1. Saves the channel fixture server (tests/fixtures/mcp_apps/channel_app_server.py)
#    as the connection "timeline", logging every set_time call to
#    .cloud-run/channel_app.log.
# 2. Sends a tool-directed prompt that makes the agent produce a surface with the app
#    and a chart bound to the same /selection/time path. This verifies the mechanism
#    only; it is not evidence of agent judgement.
# 3. Runs scripts/cloud/checks/apps/a2ui_channel.mjs on that conversation at the
#    width and height given (default 1440x900), then prints the set_time calls.
set -eu
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
AGENT="http://127.0.0.1:${AGENT_PORT:?}"
UI="http://127.0.0.1:${UI_PORT:?}/"
LOG="$ROOT/.cloud-run/channel_app.log"
WIDTH="${WIDTH:-1440}"
HEIGHT="${HEIGHT:-900}"
LABEL="${LABEL:-a2ui-channel-$WIDTH}"
: > "$LOG"

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
for _ in $(seq 1 30); do
  answer=$(curl -s -X PUT "$AGENT/v1/mcp/configuration/timeline" \
    -H 'content-type: application/json' -d "$body")
  echo "$answer" | grep -q '"status":"ready"' && break
  sleep 5
done
echo "$answer" | python3 -c 'import json,sys; r=json.load(sys.stdin); print("timeline:", r.get("status"), r.get("tools"))'

PROMPT='Use create_a2ui_surface to make surface timeline_dash. Its root is a Column with two children. First, a clio.mcp-app.v1 component with id app, server timeline, tool open_timeline, arguments {"steps": 24}, state [{"key": "time", "path": "/selection/time", "tool": "set_time"}] and height 360. Second, a clio.chart.v1 with id chart, title "Value by time step", preset scatter, xField time, yField value, entityField time, selection {"path": "/selection/time"}, selectionField time, and inline data rows for time 0 to 23 where value is time*time/10. Do nothing else and reply with one short sentence.'

node "$ROOT/scripts/cloud/ui_check.mjs" --agent "$AGENT" --ui "$UI" --width "$WIDTH" --height "$HEIGHT" \
  --send "$PROMPT" --steps "$ROOT/scripts/cloud/checks/apps/a2ui_channel.mjs" --label "$LABEL"
echo "set_time calls:"
grep '"set_time"' "$LOG" || echo "(none)"
