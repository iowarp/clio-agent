#!/usr/bin/env bash
# Sequential EarthScope grind driver for ONE model cell: 5x San Diego positive +
# Seattle alt-positive + Chicago negative. LM Studio / single-GPU providers must
# run serially, so this batches all cells in one process and logs each verdict.
#
# PER-CELL ISOLATION (MANAGE_SERVER=1, default): the gact server is killed and
# restarted CLEAN before every cell, each with its own durable trace dir. This is the fix
# for the cascade failure mode — a provider_timeout leaves `executor_work_may_continue`
# server-side, which poisons the NEXT bind (config_error) on a shared long-lived
# server. Restarting per cell means one hung tool / timed-out run can never cascade
# into the rest of the matrix (the temp sweep already proved per-run restart works).
# Set MANAGE_SERVER=0 to use a pre-existing long-lived server at GACT_URL instead.
#
# Usage:
#   PROVIDER=lm_studio MODEL=qwopus3.5-9b-v3 CTX=65536 API_BASE=http://172.23.32.1:1234/v1 \
#   N_POS=5 RESULTS=/tmp/grind_qwopus_results.txt bash grind_matrix.sh
#
# Env knobs (all optional except PROVIDER/MODEL):
#   GACT_URL (default http://127.0.0.1:17960), CTX (context_length, 0=unset),
#   API_BASE (LM Studio base), PARALLEL (default 1), NO_PROGRESS (default 900),
#   N_POS (San Diego repeats, default 5), RESULTS (verdict log path),
#   LOGDIR (per-run pytest logs, default /tmp), MANAGE_SERVER (default 1),
#   KIT_PATH (default /home/jcernuda/clio-kit), ALLOWED_ROOTS (server file policy).
set -u
cd "$(dirname "$0")/../.." || exit 2

PROVIDER="${PROVIDER:?set PROVIDER}"
MODEL="${MODEL:?set MODEL}"
GACT_URL="${GACT_URL:-http://127.0.0.1:17960}"
CTX="${CTX:-0}"
API_BASE="${API_BASE:-}"
PARALLEL="${PARALLEL:-1}"
NO_PROGRESS="${NO_PROGRESS:-900}"
N_POS="${N_POS:-5}"
LOGDIR="${LOGDIR:-/tmp}"
MANAGE_SERVER="${MANAGE_SERVER:-1}"
KIT_PATH="${KIT_PATH:-/home/jcernuda/clio-kit}"
ALLOWED_ROOTS="${ALLOWED_ROOTS:-/home/jcernuda:/tmp}"
CELL="$(echo "${PROVIDER}-${MODEL}" | tr '/:' '__')"
RESULTS="${RESULTS:-/tmp/grind_${CELL}_results.txt}"
PORT="$(echo "$GACT_URL" | grep -oE ':[0-9]+' | tr -d ':' | head -1)"; PORT="${PORT:-17960}"

kill_server() {
  local pid
  pid=$( (ss -ltnp 2>/dev/null | grep ":${PORT}" || true) | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
  sleep 3
}

start_server() {
  # $1 = per-cell durable-trace dir (canonical single recorder; full + live)
  local tracedir="$1" srvlog="$2"
  rm -rf "$tracedir"; mkdir -p "$tracedir"
  CLIO_DEBUG=med CLIO_KIT_PATH="$KIT_PATH" CLIO_ALLOWED_ROOTS="$ALLOWED_ROOTS" \
    CLIO_SEMANTIC_TRACE_BACKEND=file CLIO_SEMANTIC_TRACE_PATH="$tracedir" \
    uv run clio-agent-gact --host 127.0.0.1 --port "$PORT" \
    > "$srvlog" 2>&1 &
  local i
  for i in $(seq 1 90); do
    curl -s -m3 "http://127.0.0.1:${PORT}/v1/health" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "    -> WARN: server on :${PORT} did not become healthy in 180s" | tee -a "$RESULTS"
  return 1
}

run_one() {
  # $1=label $2=region(or empty) $3=expect(positive|negative)
  local label="$1" region="$2" expect="$3"
  local log="${LOGDIR}/grind_${CELL}_${label}.log"
  local tracedir="${LOGDIR}/trace_${CELL}_${label}"
  local srvlog="${LOGDIR}/gact_${CELL}_${label}.log"
  local t0 t1 verdict
  if [ "$MANAGE_SERVER" = 1 ]; then
    kill_server
    start_server "$tracedir" "$srvlog"
  fi
  t0=$(date +%s)
  CLIO_RUN_LIVE=1 CLIO_GACT_URL="$GACT_URL" CLIO_KIT_PATH="$KIT_PATH" \
    CLIO_AGENTTEST_API_BASE="$API_BASE" CLIO_AGENTTEST_CONTEXT_LENGTH="$CTX" \
    CLIO_AGENTTEST_PARALLEL="$PARALLEL" CLIO_AGENTTEST_NO_PROGRESS_S="$NO_PROGRESS" \
    CLIO_AGENTTEST_REGION="$region" CLIO_AGENTTEST_EXPECT="$expect" \
    uv run pytest "tests/test_real_cases/test_earthscope_case.py::test_earthscope_gnss_region" \
    --provider "$PROVIDER" --model "$MODEL" -o addopts="" -p no:cacheprovider -q \
    > "$log" 2>&1
  t1=$(date +%s)
  if grep -qE '1 passed' "$log"; then verdict=PASS; else verdict=FAIL; fi
  printf '%s  %-18s region=%-10s expect=%-8s  %ss\n' \
    "$verdict" "$label" "${region:-SanDiego}" "$expect" "$((t1-t0))" | tee -a "$RESULTS"
  if [ "$verdict" = FAIL ]; then
    echo "    -> $(grep -oE 'AssertionError.*|provider_timeout.*|made no progress.*|config_error.*' "$log" | head -1)" | tee -a "$RESULTS"
  fi
  # Kill the server after each cell so any lingering executor work (best-effort
  # cancellation on timeout) can't bleed into the next cell's bind.
  [ "$MANAGE_SERVER" = 1 ] && kill_server
}

echo "=== GRIND ${CELL} ctx=${CTX} manage_server=${MANAGE_SERVER} port=${PORT} $(date '+%F %T') ===" | tee -a "$RESULTS"
for i in $(seq 1 "$N_POS"); do run_one "sandiego_${i}" "" positive; done
run_one "seattle_alt" "Seattle" positive
run_one "chicago_neg" "Chicago" negative
echo "=== GRIND ${CELL} DONE $(date '+%F %T') ===" | tee -a "$RESULTS"
echo "--- summary ---"; grep -E '^(PASS|FAIL)' "$RESULTS" | tail -20
