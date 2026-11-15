#!/bin/bash
# Slicer plugin (SL): a new conversation in workspace print-lab, whose folder holds the
# CadQuery whistle (tests/fixtures/slicer/whistle.step, made by
# scripts/cloud/checks/apps/fileview_whistle.py as whistle_assembly.step), printed
# as "<workspace id>/<session id>" for more_conn_answer.mjs (MC_SESSION).
#   sl_new.sh [name]     (needs AGENT_PORT and CLAUDE_PROJECT_DIR, as the stack's env sets)
set -euo pipefail
name="${1:-print-lab}"
root="$CLAUDE_PROJECT_DIR/.cloud-run/apps-work/$name"
mkdir -p "$root"
cp "$CLAUDE_PROJECT_DIR/tests/fixtures/slicer/whistle.step" "$root/"
agent="http://127.0.0.1:${AGENT_PORT:-8787}"
ws=$(curl -sf "$agent/v1/workspaces" | python3 -c "
import json,sys
rows=json.load(sys.stdin); rows=rows.get('workspaces', rows) if isinstance(rows, dict) else rows
print(next((r['id'] for r in rows if r.get('name')=='$name'), ''))")
if [ -z "$ws" ]; then
  ws=$(curl -sf -X POST "$agent/v1/workspaces" -H 'content-type: application/json' \
    -d "{\"name\":\"$name\",\"root_path\":\"$root\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
fi
sid=$(curl -sf -X POST "$agent/v1/sessions" -H 'content-type: application/json' \
  -d "{\"workspace_id\":\"$ws\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
echo "$ws/$sid"
