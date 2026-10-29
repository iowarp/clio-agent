#!/bin/bash
# Teardown: stop ONLY what this kit owns on this node, and record it.
#   1. every running CLIO-managed service on target "local", through CLIO (vLLM first)
#   2. the recorded CLIO process group (TERM, KILL after 60 s; forced stops are reported)
#   3. leftovers: processes of $USER whose command line names this host's CLIO service
#      directories, and Apptainer instances named clio-* (verified before signalling)
#   4. this job's node-local scratch ($NODE_TMP), after checking the resolved path
# Persistent state, models, envs and evidence on Eagle are kept (resume needs them).
set -uo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cleanup_log="$EVIDENCE/CLEANUP.log"
note() { log "$*"; [ "$DRY_RUN" = 1 ] || echo "$(date -Is) $HOST $JOB_TAG $*" >> "$cleanup_log"; }

if [ "$DRY_RUN" != 1 ] && ! "${KIT_PY[@]}" health; then
  # Services may outlive CLIO; a CLIO is needed to stop them through their owner.
  if pgrep -u "$USER" -f "$STATE/data/services/$HOST" >/dev/null 2>&1; then
    note "CLIO is down but its services are running; starting CLIO to stop them"
    "${KIT_PY[@]}" serve-start || note "could not start CLIO for teardown"
  fi
fi
"${KIT_PY[@]}" teardown-services || note "some services did not stop cleanly (see 13-teardown-services.json)"
"${KIT_PY[@]}" serve-stop || note "CLIO stop reported a problem"

left=()
services_dir="$STATE/data/services/$HOST"
if [ "$DRY_RUN" != 1 ]; then
  sleep 5
  while read -r pid; do
    [ -z "$pid" ] || [ "$pid" = $$ ] || [ "$pid" = "$PPID" ] && continue
    owner=$(stat -c %U "/proc/$pid" 2>/dev/null)
    [ "$owner" = "$USER" ] || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-200)
    case "$cmd" in *"$services_dir"*|*"$NODE_TMP"*) left+=("$pid $cmd") ;; esac
  done < <(pgrep -u "$USER" -f "$services_dir|$NODE_TMP" 2>/dev/null)
  for row in "${left[@]+"${left[@]}"}"; do
    pid=${row%% *}; note "leftover owned process $row: TERM"; kill -TERM "$pid" 2>/dev/null
  done
  [ ${#left[@]} -gt 0 ] && sleep 15
  for row in "${left[@]+"${left[@]}"}"; do
    pid=${row%% *}
    kill -0 "$pid" 2>/dev/null && { note "leftover $pid still alive: KILL (forced)"; kill -KILL "$pid" 2>/dev/null; }
  done
  if command -v apptainer >/dev/null 2>&1; then
    for inst in $(apptainer instance list 2>/dev/null | awk 'NR>1 && $1 ~ /^clio-/ {print $1}'); do
      note "stopping Apptainer instance $inst"; apptainer instance stop "$inst" >/dev/null 2>&1
    done
  fi
  # Node-local scratch of this job only: resolved path must sit under the scratch root.
  real=$(readlink -f "$NODE_TMP" 2>/dev/null || true)
  case "$real" in
    "$(readlink -f "$NODE_SCRATCH")"/clio-"$USER"-*) note "removing node-local scratch $real"; rm -rf --one-file-system "$real" ;;
    "") ;;
    *) note "NOT removing $NODE_TMP (resolves to $real, outside $NODE_SCRATCH)" ;;
  esac
fi
[ "$DRY_RUN" = 1 ] || mkdir -p "$JOB_EVIDENCE" 2>/dev/null || true
receipt 14-teardown ok "leftover_processes=${#left[@]}" "cleanup_log=\"$cleanup_log\"" \
  "kept=\"state, models, envs and evidence under $ROOT\""
