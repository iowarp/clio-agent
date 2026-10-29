#!/usr/bin/env bash
# Record a study's live run continuously with the site's demo pipeline (site/video).
#
#   record_study.sh TAKE WORKSPACE_ID SESSION_ID "KICKOFF PROMPT" [MAX_HOURS]
#
# Starts the recorder (site/video/capture.mjs) with its own browser profile, opens the
# session in the web UI, starts a take, types the kickoff into the composer as a
# person would, and keeps recording until the session settles (status idle and no
# running task) or MAX_HOURS pass. It opens the Science log tab once the study exists.
# The take lands in site/video/public/takes/TAKE.mp4 with its marks in TAKE.json, and a
# still every three minutes in TAKE-NNN.png.
# DEMO_CHROME_PATH must name a Chrome 153+ (as root, a wrapper adding --no-sandbox).
set -euo pipefail
TAKE="$1"; WS="$2"; SID="$3"; PROMPT="$4"; HOURS="${5:-6}"
API="${CLIO_URL:-http://127.0.0.1:8787}"; UI="${CLIO_UI_URL:-http://127.0.0.1:5173}"
VIDEO="$(cd "$(dirname "$0")/../../site/video" && pwd)"
cd "$VIDEO"
# Each take has its own command folder and browser profile, so takes can record at once.
export CAPTURE_COMMANDS=".capture-commands-$TAKE" CAPTURE_PROFILE=".capture-profile-$TAKE" CAPTURE_WAIT_S=180 CAPTURE_GOTO_MS=240000
rm -rf "$CAPTURE_COMMANDS"
cmd() { node command.mjs "$1"; }
node capture.mjs > "/tmp/record-$TAKE.log" 2>&1 &
REC=$!
trap 'cmd "{\"op\":\"close\"}" >/dev/null 2>&1 || true; wait $REC 2>/dev/null || true' EXIT
until grep -q CAPTURE_READY "/tmp/record-$TAKE.log"; do sleep 1; done
cmd '{"op":"viewport","width":1600,"height":1088,"scale":0.8}'
cmd "{\"op\":\"goto\",\"url\":\"$UI/\"}"
# First run of a fresh profile: connect to the agent service.
cmd '{"op":"inspect"}' | grep -q "For example, Homelab" && {
  cmd '{"op":"fill","selector":"input[placeholder=\"For example, Homelab\"]","text":"cloud"}'
  cmd '{"op":"click","selector":"::-p-text(Connect)"}'
  cmd '{"op":"wait","ms":4000}'
}
cmd "{\"op\":\"goto\",\"url\":\"$UI/workspaces/$WS/sessions/$SID\"}"
cmd '{"op":"wait","ms":3000}'
cmd "{\"op\":\"start\",\"name\":\"$TAKE\"}"
# The composer renders after the session loads; under load that takes a while.
for _ in $(seq 1 24); do
  cmd '{"op":"click","selector":"textarea, [contenteditable=\"true\"]"}' >/dev/null 2>&1 && break
  cmd '{"op":"wait","ms":5000}' >/dev/null
done
python3 -c 'import json,sys; print(json.dumps({"op":"type","text":sys.argv[1],"delay":45}))' "$PROMPT" > "/tmp/record-$TAKE.type.json"
cmd "$(cat "/tmp/record-$TAKE.type.json")"
cmd '{"op":"key","key":"Enter"}'
cmd '{"op":"mark","label":"kickoff sent"}'
deadline=$(( $(date +%s) + HOURS * 3600 )); opened=0; shots=0; last_shot=0
status() { curl -s "$API/v1/sessions/$SID" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))'; }
busy_branches() { curl -s "$API/v1/sessions/$SID/science-log" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(b.get("task_status") in ("accepted","running","active","queued") for b in d.get("branches") or []))'; }
in_study() { curl -s "$API/v1/sessions/$SID/science-log" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("in_study", False))'; }
sleep 60
while [ "$(date +%s)" -lt "$deadline" ]; do
  if [ $opened = 0 ] && [ "$(in_study)" = True ]; then
    # The tab lives in the workspace canvas, which starts closed.
    cmd '{"op":"click","selector":"[aria-label=\"Open workspace canvas\"]"}' >/dev/null 2>&1 || true
    cmd '{"op":"wait","ms":1500}' >/dev/null 2>&1 || true
    cmd '{"op":"click","selector":"::-p-text(Science log)"}' >/dev/null 2>&1 && opened=1
    cmd '{"op":"mark","label":"science log opened"}' >/dev/null
  fi
  # A still every few minutes, read during the run as review evidence.
  if [ $(( $(date +%s) - last_shot )) -ge 180 ]; then
    shots=$((shots + 1)); last_shot=$(date +%s)
    cmd "{\"op\":\"shot\",\"name\":\"$TAKE-$(printf %03d $shots)\"}" >/dev/null 2>&1 || true
  fi
  if [ "$(status)" = idle ] && [ "$(busy_branches)" = 0 ]; then
    sleep 120; [ "$(status)" = idle ] && [ "$(busy_branches)" = 0 ] && break
  fi
  sleep 30
done
cmd '{"op":"mark","label":"settled"}'
cmd '{"op":"wait","ms":5000}'
cmd '{"op":"stop"}'
