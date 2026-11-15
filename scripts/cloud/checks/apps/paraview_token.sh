#!/bin/bash
# AQ49: the node half's token appears in no text CLIO or the browser holds.
#
#   bash scripts/cloud/checks/apps/paraview_token.sh CLUSTER_SH [PV_OUT.json ...]
#
# Reads the token (and port) from the node half's record on node08, then searches
# this stack's state (.cloud-run: transcripts, session metadata, question records,
# logs, ARC store) and each step's recorded browser requests, URLs and headers.
set -u
CLUSTER="${1:?cluster.sh}"; shift
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
record=$(bash "$CLUSTER" exec node08 bash -c 'cat /home/researcher/.clio/paraview/*/node.json')
token=$(printf '%s' "$record" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
port=$(printf '%s' "$record" | python3 -c 'import json,sys; print(json.load(sys.stdin)["port"])')
[ -n "$token" ] || { echo "no node half record on node08"; exit 2; }
echo "node half on node08: port $port, token of ${#token} characters"
found=0
hits=$(grep -rlaF -- "$token" "$ROOT/.cloud-run" 2>/dev/null)
if [ -n "$hits" ]; then echo "token found in:"; echo "$hits"; found=1; else echo "token in nothing under .cloud-run"; fi
for f in "$@"; do
  if grep -qaF -- "$token" "$f"; then echo "token found in $f"; found=1; else echo "token not in $f"; fi
  python3 - "$f" "$port" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
port = sys.argv[2]
urls = [r["url"] for r in data.get("requests", [])] + data.get("sockets", [])
print(f"  {len(urls)} requests and sockets; foreign: {len(data.get('foreign', []))}; "
      f"to the node half's port {port}: {sum(1 for u in urls if ':' + port in u)}")
PY
done
exit $found
