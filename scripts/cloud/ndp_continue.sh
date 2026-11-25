#!/usr/bin/env bash
# Continue an NDP qualification conversation with its follow-up requests, turn by turn.
#
#   bash scripts/cloud/ndp_continue.sh correlation.tahoe ws_… sess_…
#
# The follow-ups are scripts/cloud/checks/ndp_requests.json "followups"[KEY]: plain
# requests that build on what the conversation already found. Each is typed into the
# same conversation (ndp_study.mjs, STUDY_FOLLOWUPS) and captured at 1440x900; then the
# conversation is captured at 390x844 and its export (JSON and HTML transcript,
# gzipped, refused if a credential-like string appears) is written again.
set -u
cd "$(dirname "$0")/../.."
key=$1 ws=$2 sid=$3
slug="${key//./-}"
followups=$(python3 -c 'import json,sys;print(json.dumps(json.load(open("scripts/cloud/checks/ndp_requests.json"))["followups"][sys.argv[1]]))' "$key") || exit 1
STUDY_WORKSPACE="$ws" STUDY_SESSION="$sid" STUDY_TEXT="" STUDY_FOLLOWUPS="$followups" \
  timeout "${RUN_SECONDS:-10800}" node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/ndp_study.mjs \
  --label "ndp-$slug-multi" --width 1440 --height 900 > ".cloud-run/ndp-runs/$slug-multi.out" 2>&1
OPEN_WORKSPACE="$ws" OPEN_SESSION="$sid" OPEN_COLLAPSED=1 timeout 300 node scripts/cloud/ui_check.mjs \
  --steps scripts/cloud/checks/open_session.mjs --label "ndp-$slug-multi-phone" --width 390 --height 844 \
  > ".cloud-run/ndp-runs/$slug-multi-phone.out" 2>&1
mkdir -p .cloud-run/ndp-runs/exports
base=".cloud-run/ndp-runs/exports/$slug-$sid"
curl -s -m 300 -o "$base.json" "http://127.0.0.1:8787/v1/sessions/$sid/export"
curl -s -m 300 -o "$base.transcript.html" "http://127.0.0.1:8787/v1/sessions/$sid/export?mode=transcript"
if grep -a -q -E "sig=[A-Za-z0-9%]{8}|X-Amz-Signature=|Bearer [A-Za-z0-9._-]{12}|sk-[A-Za-z0-9]{16}|ghp_[A-Za-z0-9]{10}" "$base.json" "$base.transcript.html"; then
  echo "$key: export holds a credential-like string; not kept" >&2
  rm -f "$base.json" "$base.transcript.html"
else
  gzip -f -9 "$base.json" "$base.transcript.html"
fi
echo "$key: continued $ws $sid"
