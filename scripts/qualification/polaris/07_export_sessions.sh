#!/bin/bash
# Step 07: export this job's showcase sessions (OPAL chains, broad, the smoke turns) and each
# one's SPOTTER watcher session through CLIO's export API, into $EVIDENCE/sessions/<job>/.
set -uo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
out="$EVIDENCE/sessions/$JOB_TAG"; run mkdir -p "$out"
api="$("${KIT_PY[@]}" endpoint)"
sids="$(python3 - "$EVIDENCE/opal/opal-sessions.json" "$JOB_EVIDENCE" <<'PY'
import glob, json, os, sys
ids = []
try:
    ids += [v for k, v in json.load(open(sys.argv[1])).items()
            if (k.startswith("chain_") or k == "broad") and isinstance(v, str)]
except (OSError, ValueError):
    pass
for name in ("09-attention-smoke.json", "09b-spotter-smoke.json"):
    try:
        ids.append(json.load(open(os.path.join(sys.argv[2], name)))["session_id"])
    except (OSError, ValueError, KeyError):
        pass
print(" ".join(dict.fromkeys(i for i in ids if i)))
PY
)"
export_one() {
  curl -sf -m 120 "$api/v1/sessions/$1/export" -o "$out/$2.json" && log "exported $2"
  curl -sf -m 300 "$api/v1/sessions/$1/export/bundle" -o "$out/$2.bundle.zip" || rm -f "$out/$2.bundle.zip"
  curl -sf -m 60 "$api/v1/sessions/$1/attention/availability" -o "$out/$2.attention.json" || true
}
for sid in $sids; do
  export_one "$sid" "$sid"
  for child in $(curl -sf -m 60 "$api/v1/sessions/$sid/agent-tasks" | python3 -c \
      "import sys, json; [print(t['child_session_id']) for t in json.load(sys.stdin).get('tasks', [])]" 2>/dev/null); do
    export_one "$child" "$sid.spotter-$child"
  done
done
receipt 15-export-sessions ok "dir=\"$out\"" "sessions=\"$sids\""
