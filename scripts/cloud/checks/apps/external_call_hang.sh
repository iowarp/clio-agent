#!/bin/bash
# Live check: a view's call to an external stdio server is never stranded (fix stream).
#
#   AGENT_PORT=88NN bash scripts/cloud/checks/apps/external_call_hang.sh setup
#   AGENT_PORT=88NN bash scripts/cloud/checks/apps/external_call_hang.sh resave   # (b)
#   bash scripts/cloud/stack.sh restart
#   AGENT_PORT=88NN bash scripts/cloud/checks/apps/external_call_hang.sh reopen   # (a)
#
# setup: saves the channel fixture (tests/fixtures/mcp_apps/channel_app_server.py) as the
# connection "timeline" with CHANNEL_APP_OPEN_DELAY_S=6, creates a bypass session in a
# disposable workspace under .cloud-run/apps-work/hang and asks it (tool-directed) to
# open the timeline, so the session owns an assignment and a saved opening call.
# resave: starts the view's own open (the reopen route, outside any turn), saves the
# connection again with a changed environment while that call is in flight, opens the
# conversation as the UI does (the commands listing, then the messages, which warm the
# session's servers), and opens the view again. Reports how each open answered and how
# long it took.
# reopen: after a restart, opens the conversation as the UI does, replays the saved
# opening call outside a turn and reports how it answered and how long it took.
# Every run prints the fixture's calls during it (.cloud-run/channel_app.log).
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
A="http://127.0.0.1:${AGENT_PORT:?}"
STATE="$ROOT/.cloud-run/apps-work/hang"; mkdir -p "$STATE"
LOG="$ROOT/.cloud-run/channel_app.log"; touch "$LOG"
py() { python3 -c "$@"; }
j() { curl -s "$@"; }
config() {  # the connection body with a nonce in its environment
  python3 - "$ROOT" "$LOG" "$1" <<'EOF'
import json, sys
root, log, nonce = sys.argv[1:4]
print(json.dumps({
    "transport": "stdio",
    "command": f"{root}/.venv/bin/python",
    "args": [f"{root}/tests/fixtures/mcp_apps/channel_app_server.py"],
    "env": {"CHANNEL_APP_LOG": log, "CHANNEL_APP_OPEN_DELAY_S": "6", "HANG_NONCE": nonce},
    "always_load": True,
    "name": "Timeline",
}))
EOF
}
save() { j -X PUT "$A/v1/mcp/configuration/timeline" -H 'content-type: application/json' -d "$(config "$1")" \
  | py 'import json,sys; r=json.load(sys.stdin); print("saved timeline:", r.get("status"), r.get("tools"))'; }
fixture_calls() { echo "fixture calls during this run:"; tail -n +"$(( $1 + 1 ))" "$LOG"; }
timed_post() {  # path body -> prints seconds and the answer's shape
  local t0; t0=$(date +%s.%N)
  out=$(curl -s --max-time "${OPEN_LIMIT_S:-90}" -X POST "$A$1" -H 'content-type: application/json' -d "$2")
  rc=$?
  printf 'answer after %.1fs (curl rc=%s): ' "$(echo "$(date +%s.%N) - $t0" | bc)" "$rc"
  echo "$out" | py 'import json,sys
try: r=json.load(sys.stdin)
except Exception: print("<no answer>"); raise SystemExit
print(json.dumps({"error": r.get("error"), "part": bool(r.get("part"))})[:600])'
}
open_conversation() {  # what the UI asks for when it opens the conversation
  j "$A/v1/commands?session_id=$1" | py 'import json,sys; print("commands:", len(json.load(sys.stdin).get("commands", [])))'
  j "$A/v1/sessions/$1/messages?limit=20" >/dev/null
  sleep "${WARM_S:-10}"  # the session's servers warm in the background
}
before=$(wc -l < "$LOG")
case "${1:-}" in
  setup)
    save "setup-$(date +%s)"
    WS=$(j -X POST "$A/v1/workspaces" -H 'content-type: application/json' \
      -d "{\"root_path\":\"$STATE\",\"name\":\"hang\"}" | py "import sys,json;print(json.load(sys.stdin)['id'])")
    SID=$(j -X POST "$A/v1/sessions" -H 'content-type: application/json' \
      -d "{\"workspace_id\":\"$WS\",\"title\":\"hang\",\"approval_mode\":\"bypass\"}" \
      | py "import sys,json;print(json.load(sys.stdin)['id'])")
    echo "$SID" > "$STATE/session"
    j -X POST "$A/v1/sessions/$SID/messages" -H 'content-type: application/json' \
      -d '{"parts":[{"type":"text","text":"Open the timeline view with timeline_open_timeline at time 3."}]}' >/dev/null
    for _ in $(seq 1 60); do
      s=$(j "$A/v1/sessions/$SID" | py "import sys,json;print(json.load(sys.stdin).get('status'))")
      case "$s" in idle|error) break;; esac; sleep 3
    done
    echo "session $SID: $s"
    j "$A/v1/sessions/$SID" | py 'import json,sys
calls=(json.load(sys.stdin).get("metadata") or {}).get("mcp_app_calls") or {}
print("saved calls:", list(calls))'
    ;;
  resave)
    SID=$(cat "$STATE/session")
    timed_post "/v1/sessions/$SID/mcp-apps/reopen" \
      '{"call":{"server":"timeline","tool":"open_timeline","arguments":{"time":5}},"save":false}' &
    sleep 2
    save "resave-$(date +%s)"
    wait
    open_conversation "$SID"
    timed_post "/v1/sessions/$SID/mcp-apps/reopen" \
      '{"call":{"server":"timeline","tool":"open_timeline","arguments":{"time":6}},"save":false}'
    ;;
  reopen)
    SID=$(cat "$STATE/session")
    APP=$(j "$A/v1/sessions/$SID" | py 'import json,sys
calls=(json.load(sys.stdin).get("metadata") or {}).get("mcp_app_calls") or {}
print(list(calls)[-1] if calls else "")')
    open_conversation "$SID"
    echo "reopening $APP"
    timed_post "/v1/sessions/$SID/mcp-apps/reopen" "{\"app_instance_id\":\"$APP\"}"
    ;;
  *) echo "usage: $0 setup|resave|reopen"; exit 2 ;;
esac
fixture_calls "$before"
