#!/bin/bash
# Live check (fix stream): a view whose server stopped is not live; a dead service says so.
#
# Start the stack with a short fleet TTL first, so a conversation nobody views is reaped
# within a minute:  CLIO_MCP_WORKSPACE_TTL_S=20 bash scripts/cloud/stack.sh start
#
#   AGENT_PORT=88NN UI_PORT=52NN bash scripts/cloud/checks/apps/stopped_server.sh setup
#   ... then, in order: open | down | reap | reopen
#
# setup: saves the service fixture (tests/fixtures/mcp_app_service_server.py) as the
#   connection "counter" (port STOPPED_SERVICE_PORT, default 20400) and asks a new
#   conversation, in plain words, for the live counter.
# open:  the UI shows the counter (card, then its tab), then leaves the conversation.
# down:  stops the service fixture's process while the view's record stays: the UI's
#   view reads "Service unavailable" and the service address shows a readable page.
# reap:  waits until the reaper closed the conversation's fleet (agent.log; nobody views
#   the conversation), then asks the card's liveness route: the view is no longer
#   live (mcp_app_server_stopped).
# reopen: the UI's card offers Reopen and the replay starts the server and mounts the
#   counter again.
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
A="http://127.0.0.1:${AGENT_PORT:?}"
UI="http://127.0.0.1:${UI_PORT:?}/"
STATE="$ROOT/.cloud-run/apps-work/stopped"; mkdir -p "$STATE"
PORT="${STOPPED_SERVICE_PORT:-20400}"
TOKEN="${STOPPED_SERVICE_TOKEN:-fixstream-$(date +%s)}"
py() { python3 -c "$@"; }
j() { curl -s "$@"; }
ui() {  # phase width height
  STOPPED_PHASE="$1" STOPPED_WORKSPACE="$(cat "$STATE/workspace")" \
  STOPPED_SESSION="$(cat "$STATE/session")" STOPPED_SERVICE_URL="${STOPPED_SERVICE_URL:-}" \
    node "$ROOT/scripts/cloud/ui_check.mjs" --agent "$A" --ui "$UI" --width "$2" --height "$3" \
    --steps "$ROOT/scripts/cloud/checks/apps/stopped_server.mjs" --label "stopped-$1-$2"
}
last_app() {  # app_instance_id and data_ref of the conversation's last mcp_app part
  j "$A/v1/sessions/$(cat "$STATE/session")/messages" | py '
import json,sys
apps=[p for m in json.load(sys.stdin)["messages"] for p in m["parts"] if p["type"]=="mcp_app"]
print(apps[0]["app_instance_id"], apps[0]["data_ref"]) if apps else print("")'
}
case "${1:-}" in
  setup)
    body=$(python3 - "$ROOT" "$PORT" "$TOKEN" <<'EOF'
import json, sys
root, port, token = sys.argv[1:4]
print(json.dumps({
    "transport": "stdio",
    "command": f"{root}/.venv/bin/python",
    "args": ["-B", f"{root}/tests/fixtures/mcp_app_service_server.py"],
    "env": {"CLIO_SERVICE_FIXTURE_PORT": port, "CLIO_SERVICE_FIXTURE_TOKEN": token},
    "always_load": True,
    "name": "Counter",
}))
EOF
)
    j -X PUT "$A/v1/mcp/configuration/counter" -H 'content-type: application/json' -d "$body" \
      | py 'import json,sys; r=json.load(sys.stdin); print("counter:", r.get("status"), r.get("tools"))'
    WS=$(j -X POST "$A/v1/workspaces" -H 'content-type: application/json' \
      -d "{\"root_path\":\"$STATE\",\"name\":\"stopped\"}" | py "import sys,json;print(json.load(sys.stdin).get('id',''))")
    [ -n "$WS" ] || WS=$(cat "$STATE/workspace")  # created by an earlier run
    SID=$(j -X POST "$A/v1/sessions" -H 'content-type: application/json' \
      -d "{\"workspace_id\":\"$WS\",\"title\":\"live counter\",\"approval_mode\":\"bypass\"}" \
      | py "import sys,json;print(json.load(sys.stdin)['id'])")
    echo "$WS" > "$STATE/workspace"; echo "$SID" > "$STATE/session"
    j -X POST "$A/v1/sessions/$SID/messages" -H 'content-type: application/json' \
      -d '{"parts":[{"type":"text","text":"Show me the live counter."}]}' >/dev/null
    sleep 5
    for _ in $(seq 1 80); do
      s=$(j "$A/v1/sessions/$SID" | py "import sys,json;print(json.load(sys.stdin).get('status'))")
      case "$s" in idle|error) break;; esac; sleep 4
    done
    echo "session $WS/$SID: $s; app: $(last_app)"
    ;;
  open) ui open 1440 900 ;;
  reap)
    read -r APP REF <<< "$(last_app)"
    SID=$(cat "$STATE/session")
    root=$(j "$A/v1/sessions/$SID" | py "import sys,json;print(json.load(sys.stdin)['workspace_id'])" \
      | xargs -I{} curl -s "$A/v1/workspaces/{}" | py "import sys,json;print(json.load(sys.stdin)['root_path'])")
    for _ in $(seq 1 40); do
      grep -q "workspace_fleet_reaped root=$root" "$ROOT/.cloud-run/agent.log" && break; sleep 5
    done
    grep "workspace_fleet_reaped root=$root" "$ROOT/.cloud-run/agent.log" | tail -1
    echo "live after the reap: $(j "$A/v1/sessions/$SID/mcp-apps/$APP/live?data_ref=$REF")"
    grep "mcp_app_server_stopped app_instance_id=$APP" "$ROOT/.cloud-run/agent.log" | tail -1
    ;;
  reopen) ui reopen 1440 900; ui reopen 390 844 ;;
  down)
    pkill -f "$ROOT/tests/fixtures/mcp_app_service_server[.]py" && echo "stopped this stack's service fixture process"
    sleep 2
    read -r APP REF <<< "$(last_app)"
    SID=$(cat "$STATE/session")
    host=$(j "$A/v1/sessions/$SID/mcp-apps/$APP?data_ref=$REF" | py 'import json,sys; d=json.load(sys.stdin); print(d.get("sandbox_url",""), d.get("service_refusal"))')
    echo "descriptor: $host"
    sandbox=$(echo "$host" | cut -d' ' -f1)
    origin=$(echo "$sandbox" | py 'import sys; from urllib.parse import urlsplit; u=urlsplit(sys.stdin.read().strip()); print(f"{u.scheme}://{u.netloc}")')
    STOPPED_SERVICE_URL="$origin/v1/sessions/$SID/mcp-apps/$APP/service/$REF/" ui down 1440 900
    STOPPED_SERVICE_URL="$origin/v1/sessions/$SID/mcp-apps/$APP/service/$REF/" ui down 390 844
    ;;
  *) echo "usage: $0 setup|open|reap|reopen|down"; exit 2 ;;
esac
