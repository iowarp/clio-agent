#!/bin/bash
# Geo globe (GG): a new conversation in a workspace folder holding the example station
# list, printed as "<workspace id>/<session id>" for more_conn_answer.mjs (MC_SESSION).
#   gg_new.sh <name>     (needs AGENT_PORT and CLAUDE_PROJECT_DIR, as the stack's env sets)
set -euo pipefail
name="$1"
root="$CLAUDE_PROJECT_DIR/.cloud-run/apps-work/$name"
mkdir -p "$root"
cp "$CLAUDE_PROJECT_DIR/tests/fixtures/geo_globe/example_stations_rainier.csv" "$root/"
agent="http://127.0.0.1:${AGENT_PORT:-8787}"
ws=$(curl -sf -X POST "$agent/v1/workspaces" -H 'content-type: application/json' \
  -d "{\"name\":\"$name\",\"root_path\":\"$root\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
sid=$(curl -sf -X POST "$agent/v1/sessions" -H 'content-type: application/json' \
  -d "{\"workspace_id\":\"$ws\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
echo "$ws/$sid"
