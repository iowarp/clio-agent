#!/usr/bin/env bash
# The b=transform(a) edge-requalification driver (doc §14.x). Source your
# deployment.env first. Exits after dumping the MLMD graph for inspection.
set -uo pipefail

: "${CLIO_PQ_PORT:?}"
: "${CLIO_PQ_WORKSPACE:?}"
: "${CLIO_PQ_CMF_PYTHON:?}"
: "${CLIO_PQ_CMF_METADATA_PATH:?}"
B="http://127.0.0.1:$CLIO_PQ_PORT"

wait_idle() {
  local sid=$1 budget=$2 st="?"
  for _ in $(seq 1 $((budget / 8))); do
    sleep 8
    st=$(curl -s "$B/v1/sessions/$sid" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
    case "$st" in idle|error|failed) echo "$st"; return;; esac
  done
  echo "budget_exhausted($st)"
}

rm -f "$CLIO_PQ_WORKSPACE/a.csv" "$CLIO_PQ_WORKSPACE/b.csv"

WS=$(curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"edges-requal\",\"root_path\":\"$CLIO_PQ_WORKSPACE\"}" \
  "$B/v1/workspaces" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "workspace=$WS"

curl -s -X PUT -H 'Content-Type: application/json' \
  -d "{\"policies\":[{\"scope\":\"workspace\",\"scope_id\":\"$WS\",\"action\":\"allow\",\"priority\":1,\"tool_name_pattern\":\"*\",\"path_pattern\":\"*\"}]}" \
  "$B/v1/policies" > /dev/null

SID=$(curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"title\":\"edge requalification b=transform(a)\",\"workspace_id\":\"$WS\"}" \
  "$B/v1/sessions" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "session=$SID"

curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"text":"Create a file a.csv in the workspace root containing exactly three lines: header value then rows 10 and 20. Use your file-write tool. Nothing else."}' \
  "$B/v1/sessions/$SID/messages" > /dev/null
echo "turn1: $(wait_idle "$SID" 240)"

sleep 3
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"text":"Read a.csv with your file-read tool, then write b.csv (file-write tool) containing the same header and each value doubled. Report the doubled values. Nothing else."}' \
  "$B/v1/sessions/$SID/messages" > /dev/null
echo "turn2: $(wait_idle "$SID" 300)"

echo "---files---"
ls -l "$CLIO_PQ_WORKSPACE"/*.csv 2>/dev/null
echo "---mlmd events---"
CLIO_PQ_CMF_METADATA_PATH="$CLIO_PQ_CMF_METADATA_PATH" "$CLIO_PQ_CMF_PYTHON" - <<'PY'
import os
from cmflib.store.sqllite_store import SqlliteStore
store = SqlliteStore({"filename": os.environ["CLIO_PQ_CMF_METADATA_PATH"]}).connect()
arts = {a.id: a for a in store.get_artifacts()}
execs = store.get_executions()
print(f"artifacts={len(arts)} executions={len(execs)}")
for a in arts.values():
    print(f"  artifact id={a.id} uri={a.uri} name={a.name}")
events = store.get_events_by_execution_ids([e.id for e in execs]) if execs else []
print(f"events={len(events)}")
for ev in events:
    kind = {3: "INPUT", 4: "OUTPUT"}.get(ev.type, str(ev.type))
    art = arts.get(ev.artifact_id)
    print(f"  {kind} exec={ev.execution_id} artifact={art.uri if art else ev.artifact_id}")
PY
