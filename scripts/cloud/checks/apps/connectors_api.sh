#!/bin/bash
# The connector scenarios (AQ2 to AQ11) through the API, on a stack started with
# scripts/cloud/checks/apps/connector-fixtures.env.sh sourced. Prints the evidence.
#
#   source scripts/cloud/checks/apps/connector-fixtures.env.sh
#   bash scripts/cloud/stack.sh start
#   bash scripts/cloud/checks/apps/connectors_api.sh http://127.0.0.1:$AGENT_PORT http://127.0.0.1:$UI_PORT
#
# It starts the OAuth fixture server (tests/fixtures/oauth_mcp_server.py) on
# CONNECTOR_FIXTURE_PORT (default 19101) and stops it at the end. The agent requests
# are plain: they name no tool. Sessions run in bypass approval in a disposable
# workspace under .cloud-run/apps-work/connectors. Enabling a connector adds it to no
# conversation (CS-D1): when the agent offers one, the script answers Add as the person.
set -u
A="${1:?agent url}"; UI="${2:?ui url}"
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
PORT="${CONNECTOR_FIXTURE_PORT:-19101}"
WORK="$ROOT/.cloud-run/apps-work/connectors"; mkdir -p "$WORK"
SECRET="${CONNECTOR_TEST_SECRET:-aq5-$(date +%s)-zebra}"
py() { python3 -c "$@"; }
j() { curl -s "$@"; }
say() { printf '\n=== %s\n' "$*"; }
state() { j "$A/v1/connectors/$1" | py "import sys,json;d=json.load(sys.stdin);print(json.dumps({'enabled':d.get('enabled'),'state':d.get('state'),'tool_count':d.get('tool_count'),'needs':[(n['key'],n['value'],n['has_value']) for n in d.get('needs',[])]}))"; }

(cd "$ROOT" && setsid uv run --no-sync python -B tests/fixtures/oauth_mcp_server.py --port "$PORT" \
  > "$ROOT/.cloud-run/oauth-fixture.log" 2>&1 & echo $! > "$ROOT/.cloud-run/oauth-fixture.pid")
trap 'kill -- -"$(cat "$ROOT/.cloud-run/oauth-fixture.pid")" 2>/dev/null' EXIT
until curl -s -o /dev/null "http://127.0.0.1:$PORT/fixture-counters"; do sleep 1; done

WS=$(j -X POST "$A/v1/workspaces" -H 'content-type: application/json' \
  -d "{\"root_path\":\"$WORK\",\"name\":\"connectors\"}" | py "import sys,json;print(json.load(sys.stdin)['id'])")
new_session() {
  j -X POST "$A/v1/sessions" -H 'content-type: application/json' \
    -d "{\"workspace_id\":\"$WS\",\"title\":\"$1\",\"approval_mode\":\"bypass\"}" \
    | py "import sys,json;print(json.load(sys.stdin)['id'])"
}
send() {
  j -X POST "$A/v1/sessions/$1/messages" -H 'content-type: application/json' \
    -d "$(py 'import json,sys;print(json.dumps({"parts":[{"type":"text","text":sys.argv[1]}]}))' "$2")" > /dev/null
}
card() {  # the pending connector_setup interaction id, or empty
  j "$A/v1/sessions/$1/interactions" | py "import sys,json;r=[x['id'] for x in json.load(sys.stdin)['interactions'] if x['kind']=='connector_setup' and x['status']=='pending'];print(r[0] if r else '')"
}
offer() {  # the pending offer card (connectors are added per conversation, CS-D6), or empty
  j "$A/v1/sessions/$1/interactions" | py "import sys,json;r=[x['id']+' '+(x.get('payload') or {}).get('connector_name','') for x in json.load(sys.stdin)['interactions'] if x['kind']=='connector_setup' and x['status']=='pending' and (x.get('payload') or {}).get('intent') in ('enable','add_plugin')];print(r[0] if r else '')"
}
settle() {  # wait for the turn to end, or for a setup card (prints card <id>)
  # An offer to add a connector to this conversation is answered Add, as the person
  # does on the card (the same respond route); a setup card is returned to the caller.
  sleep 3
  while true; do
    o=$(offer "$1")
    if [ -n "$o" ]; then
      j -X POST "$A/v1/sessions/$1/interactions/${o%% *}/respond" -H 'content-type: application/json' \
        -d '{"action":"submit","values":{}}' >/dev/null
      echo "offer added: ${o#* }"; sleep 3; continue
    fi
    c=$(card "$1"); [ -n "$c" ] && { echo "card $c"; return; }
    s=$(j "$A/v1/sessions/$1" | py "import sys,json;print(json.load(sys.stdin).get('status'))")
    case "$s" in idle|error) echo "turn $s"; return;; esac
    sleep 4
  done
}
calls() {  # the last turn's tool calls and results, and its answer
  j "$A/v1/sessions/$1/messages" | py "
import sys,json
m=json.load(sys.stdin)['messages']
print('user messages:', sum(1 for x in m if x['role']=='user'))
for p in m[0]['parts']:
  if p['type'] in ('tool_call','tool_result'):
    body=p.get('input') if p['type']=='tool_call' else ' '.join(c.get('text','') for c in p.get('content') or [])
    print(' ',p['type'],p.get('tool_name'),json.dumps(body)[:300])
  elif p['type']=='text': print('  answer:',p['text'][:300].replace(chr(10),' '))"
}
sign_in() {
  uv run --no-sync python -B "$ROOT/scripts/cloud/checks/apps/connector_sign_in.py" \
    --agent "$A" --ui "$UI" --connector fixture-signin 2>&1 | grep -v UV_NATIVE
}

say "AQ2 enable the connector with no needs"
j -X POST "$A/v1/connectors/fixture-plain/enable" -H 'content-type: application/json' -d '{"scope":"all"}' >/dev/null
state fixture-plain

say "AQ3 the address and sign-in connector"
j -X POST "$A/v1/connectors/fixture-signin/enable" -H 'content-type: application/json' -d '{"scope":"all"}' >/dev/null
state fixture-signin
j -X PUT "$A/v1/connectors/fixture-signin/values" -H 'content-type: application/json' \
  -d "{\"values\":{\"address\":\"http://127.0.0.1:$PORT/mcp\"}}" >/dev/null
state fixture-signin
sign_in
state fixture-signin

say "AQ4 an address that does not answer"
j -X PUT "$A/v1/connectors/fixture-signin/values" -H 'content-type: application/json' \
  -d '{"values":{"address":"http://127.0.0.1:19199/mcp"}}' >/dev/null
state fixture-signin
j -X PUT "$A/v1/connectors/fixture-signin/values" -H 'content-type: application/json' \
  -d "{\"values\":{\"address\":\"http://127.0.0.1:$PORT/mcp\"}}" >/dev/null
state fixture-signin

say "AQ11 a server by URL with no entry"
j -X POST "$A/v1/connectors" -H 'content-type: application/json' -d "{\"url\":\"http://127.0.0.1:$PORT/mcp\",\"name\":\"By address\"}" \
  | py "import sys,json;d=json.load(sys.stdin);print(json.dumps({k:d[k] for k in ('id','source','logo_url','state')}), [(n['key'],n['kind']) for n in d['needs']])"
j -X DELETE "$A/v1/connectors/by-address" >/dev/null

say "AQ10 a connector with a following plugin, enabled in an open conversation"
j -X POST "$A/v1/plugins/marketplaces" -H 'content-type: application/json' \
  -d "{\"source\":\"$ROOT/tests/fixtures/connectors/marketplace\"}" >/dev/null
j -X POST "$A/v1/plugins/marketplaces/clio-connector-fixtures/trust" -H 'content-type: application/json' -d '{}' >/dev/null
j "$A/v1/plugins" | py "import sys,json;print('following plugins:',[(p['id'],p['follows']) for p in json.load(sys.stdin)['plugins'] if p['follows']])"
S10=$(new_session aq10); echo "session $S10"
send "$S10" "Say hello in five words."; settle "$S10"
curl -s -N "$A/v1/sessions/$S10/events" > "$ROOT/.cloud-run/aq10.sse" 2>&1 & SSE=$!
sleep 2
j -X POST "$A/v1/connectors/fixture-secret/enable" -H 'content-type: application/json' -d '{"scope":"all"}' >/dev/null
agent_view() { j "$A/v1/sessions/$S10/agent" | py "import sys,json;d=json.load(sys.stdin);print('pinned plugins following:',[p['id'] for p in d['resolution']['plugins'] if p.get('follows')],'| notice:',json.dumps({k:d['prompt_basis'][k] for k in ('pending','added','removed','skills_now')}))"; }
agent_view; state fixture-secret

say "AQ5 type the secret"
j -X PUT "$A/v1/connectors/fixture-secret/values" -H 'content-type: application/json' \
  -d "{\"values\":{\"token\":\"$SECRET\"}}" >/dev/null
state fixture-secret
echo "expected token hash prefix: $(printf %s "$SECRET" | sha256sum | cut -c1-12)"
send "$S10" "Did the Fixture Secret server get its access token? Check it."; settle "$S10"; calls "$S10"

say "AQ8 a new address for an agent-settable need"
send "$S10" "The Fixture Secret server has moved: it now runs on node08. Point it there and check it."
settle "$S10"; calls "$S10"; state fixture-secret

say "AQ9 ask the agent to set the secret and to sign in"
j -X DELETE "$A/v1/mcp/configuration/fixture-signin/authorization" >/dev/null
send "$S10" "Please set the Fixture Secret access token to tok-aq9-banana for me, and sign me in to the Fixture Sign-in test account."
r=$(settle "$S10"); echo "$r"
case "$r" in card*) echo "the agent's call waits for the person; declining it"
  j -X POST "$A/v1/sessions/$S10/interactions/${r#card }/respond" -H 'content-type: application/json' -d '{"action":"decline"}' >/dev/null
  settle "$S10";; esac
calls "$S10"; state fixture-secret

say "AQ10 disable it again"
j -X POST "$A/v1/connectors/fixture-secret/disable" >/dev/null
agent_view
kill $SSE 2>/dev/null
py "import json;print('connector.changed events:',[json.loads(l[5:])['payload'] for l in open('$ROOT/.cloud-run/aq10.sse') if l.startswith('data:') and 'connector.changed' in l])"

say "AQ6 a revoked sign-in: the call waits, then the same call returns"
S6=$(new_session aq6); echo "session $S6"
send "$S6" "What does the synthetic test account say about its status?"
r=$(settle "$S6"); echo "$r"
sleep 10; echo "still waiting: $(j "$A/v1/sessions/$S6" | py "import sys,json;print(json.load(sys.stdin).get('status'))")"
sign_in
j -X POST "$A/v1/sessions/$S6/interactions/${r#card }/respond" -H 'content-type: application/json' \
  -d '{"action":"submit"}' | py "import sys,json;print('card:',json.load(sys.stdin)['interaction']['status'])"
settle "$S6"; calls "$S6"

say "AQ7 the same, declined"
j -X DELETE "$A/v1/mcp/configuration/fixture-signin/authorization" >/dev/null
S7=$(new_session aq7); echo "session $S7"
send "$S7" "What does the synthetic test account say about its status?"
r=$(settle "$S7"); echo "$r"
j -X POST "$A/v1/sessions/$S7/interactions/${r#card }/respond" -H 'content-type: application/json' \
  -d '{"action":"decline"}' | py "import sys,json;print('card:',json.load(sys.stdin)['interaction']['status'])"
settle "$S7"; calls "$S7"

say "AQ5 where the secret is found"
grep -rl -- "$SECRET" "$ROOT/.cloud-run" 2>/dev/null || true
ls -l "${XDG_CONFIG_HOME:-$ROOT/.cloud-run/xdg/config}/clio-agent/mcp_oauth_tokens.json"
