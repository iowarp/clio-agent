#!/usr/bin/env bash
# Run NDP qualification requests one after another on the running cloud stack.
#
#   bash scripts/cloud/ndp_batch.sh organizations.trpa october.alta ...
#
# Each key names a request in scripts/cloud/checks/ndp_requests.json. Every run gets a
# fresh workspace under .cloud-run/ndp-ws/<key>, goes through ndp_study.mjs at
# 1440x900 (Enable all, then the request typed into the composer), then reopens the
# session at 390x844 for the phone capture. Outputs: .cloud-run/ndp-runs/<key>.out
# (the study's JSON summary) and screenshots in .cloud-run/shots/ndp-<key>-*.png.
set -u
cd "$(dirname "$0")/../.."
mkdir -p .cloud-run/ndp-runs
for key in "$@"; do
  slug="${key//./-}"
  text=$(python3 -c 'import json,sys;d=json.load(open("scripts/cloud/checks/ndp_requests.json"))
v=d
for k in sys.argv[1].split("."): v=v[k]
print(v)' "$key") || { echo "$key: no such request" >&2; continue; }
  root="$PWD/.cloud-run/ndp-ws/$slug-$(date -u +%H%M%S)"
  mkdir -p "$root"
  echo "== $key -> $root"
  STUDY_ROOT="$root" STUDY_AGENT="${STUDY_AGENT:-ndp}" STUDY_TEXT="$text" \
    timeout "${RUN_SECONDS:-3000}" node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/ndp_study.mjs \
    --label "ndp-$slug" --width 1440 --height 900 > ".cloud-run/ndp-runs/$slug.out" 2>&1
  ids=$(python3 - ".cloud-run/ndp-runs/$slug.out" <<'EOF'
import json, re, sys
text = open(sys.argv[1]).read()
m = re.search(r'"session": "(sess_\w+)",\s*"workspace": "(ws_\w+)"', text)
print(f"{m.group(2)} {m.group(1)}" if m else "")
EOF
)
  if [ -n "$ids" ]; then
    read -r ws sid <<< "$ids"
    OPEN_WORKSPACE="$ws" OPEN_SESSION="$sid" OPEN_COLLAPSED=1 timeout 300 node scripts/cloud/ui_check.mjs \
      --steps scripts/cloud/checks/open_session.mjs --label "ndp-$slug-phone" --width 390 --height 844 \
      > ".cloud-run/ndp-runs/$slug-phone.out" 2>&1
    echo "$key: session $ids"
  else
    echo "$key: no session recorded (see .cloud-run/ndp-runs/$slug.out)"
  fi
done
