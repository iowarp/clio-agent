#!/bin/bash
# Permissions follow the mode (apps ledger, PM), live. On a stack started with
# scripts/cloud/checks/apps/connector-fixtures.env.sh sourced:
#
#   AGENT_PORT=88NN UI_PORT=52NN bash scripts/cloud/checks/apps/perm_mode.sh [round ...]
#
# Saves the board (tests/fixtures/mcp_apps/view_tools_server.py), the host panel
# (host_panel_server.py) and the A2UI channel fixture (channel_app_server.py) as the
# connections board, hostpanel and timeline; turns on the Fixture Secret connector with
# a synthetic token; then runs scripts/cloud/checks/apps/perm_mode.mjs for each round
# (default: bypass ask-a ask-b ask-c phone). The ask rounds share one workspace, so
# "This workspace" carries from ask-b to ask-c. Each round prints PERM_RESULT.
set -eu
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
AGENT="http://127.0.0.1:${AGENT_PORT:?}"
UI="http://127.0.0.1:${UI_PORT:?}/"
STAMP="${PERM_STAMP:-$(date +%H%M%S)}"
ROUNDS=("$@"); [ ${#ROUNDS[@]} -gt 0 ] || ROUNDS=(bypass ask-a ask-b ask-c phone)

save() {  # name, fixture file, extra env json
  local body answer
  body=$(python3 -c 'import json,sys; r,n,f,e=sys.argv[1:5]; print(json.dumps({"transport":"stdio","command":f"{r}/.venv/bin/python","args":[f"{r}/tests/fixtures/mcp_apps/{f}"],"env":json.loads(e),"always_load":True,"name":n}))' "$ROOT" "$1" "$2" "$3")
  answer=$(curl -s "$AGENT/v1/mcp/configuration/$1")
  for _ in $(seq 1 30); do
    echo "$answer" | grep -q '"status":"ready"' && break
    answer=$(curl -s -X PUT "$AGENT/v1/mcp/configuration/$1" -H 'content-type: application/json' -d "$body")
    echo "$answer" | grep -q '"status":"ready"' || sleep 5
  done
  echo "$answer" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(sys.argv[1]+":", r.get("status"), r.get("tools"))' "$1"
}
save board view_tools_server.py "{}"
save hostpanel host_panel_server.py "{}"
save timeline channel_app_server.py "{\"CHANNEL_APP_LOG\": \"$ROOT/.cloud-run/channel_app.log\"}"

curl -s -X POST "$AGENT/v1/connectors/fixture-secret/enable" -H 'content-type: application/json' \
  -d '{"scope":"all"}' >/dev/null
curl -s -X PUT "$AGENT/v1/connectors/fixture-secret/values" -H 'content-type: application/json' \
  -d "{\"values\":{\"token\":\"perm-$STAMP-synthetic\"}}" >/dev/null
curl -s "$AGENT/v1/connectors/fixture-secret" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("fixture-secret:", d.get("enabled"), d.get("state"))'

for round in "${ROUNDS[@]}"; do
  size=(--width 1440 --height 900); ws="perm-ask-$STAMP"
  case "$round" in
    bypass) ws="perm-bypass-$STAMP" ;;
    phone) size=(--width 390 --height 844); ws="perm-phone-$STAMP" ;;
  esac
  echo "=== round $round in $ws"
  PERM_RUN="$round" PERM_WS="$ws" AGENT_PORT="$AGENT_PORT" node "$ROOT/scripts/cloud/ui_check.mjs" \
    --agent "$AGENT" --ui "$UI" "${size[@]}" \
    --steps "$ROOT/scripts/cloud/checks/apps/perm_mode.mjs" --label "perm-$round" \
    || echo "round $round: the step failed (see its PERM_RESULT and failure shot)"
done
