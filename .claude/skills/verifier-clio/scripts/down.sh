#!/usr/bin/env bash
# End a verification run: delete the sessions and workspaces the drivers
# recorded in resources.json, remove the run's scratch workspace folders, and
# stop the stack unless --keep-stack. A session delete can take a minute.
# Screenshots, panes and turn captures stay
# in the run directory as the evidence; delete them yourself once reported.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
RUN="${VERIFY_DIR:-$ROOT/.cloud-run/verify/current}"
RUN="$(cd "$RUN" && pwd -P)"
case "$RUN" in "$ROOT"/.cloud-run/verify/*) ;; *) echo "refusing: $RUN is not a verify run"; exit 2 ;; esac
if [ -f "$RUN/resources.json" ]; then
  for sid in $(python3 -c "import json,sys;print(' '.join(json.load(open(sys.argv[1])).get('sessions',[])))" "$RUN/resources.json"); do
    printf 'session %s: %s\n' "$sid" "$(curl -s -m 300 -o /dev/null -w '%{http_code}' -X DELETE "http://127.0.0.1:8787/v1/sessions/$sid")"
  done
  for ws in $(python3 -c "import json,sys;print(' '.join(json.load(open(sys.argv[1])).get('workspaces',[])))" "$RUN/resources.json"); do
    printf 'workspace %s: %s\n' "$ws" "$(curl -s -m 300 -o /dev/null -w '%{http_code}' -X DELETE "http://127.0.0.1:8787/v1/workspaces/$ws")"
  done
fi
for d in "$RUN"/ws-*; do
  [ -d "$d" ] && rm -rf "${d:?}" && echo "removed $d"
done
tmux -L clio-verify kill-server 2>/dev/null || true
[ "${1:-}" = "--keep-stack" ] || bash "$ROOT/scripts/cloud/stack.sh" stop
