#!/usr/bin/env bash
# Sequential EarthScope grind driver for ONE model cell: 5x San Diego positive +
# Seattle alt-positive + Chicago negative. LM Studio / single-GPU providers must
# run serially, so this batches all cells in one process and logs each verdict.
#
# Usage:
#   PROVIDER=lm_studio MODEL=qwopus3.5-9b-v3 CTX=32768 API_BASE=http://172.23.32.1:1234/v1 \
#   N_POS=5 RESULTS=/tmp/grind_qwopus_results.txt bash grind_matrix.sh
#
# Env knobs (all optional except PROVIDER/MODEL):
#   GACT_URL (default http://127.0.0.1:17960), CTX (context_length, 0=unset),
#   API_BASE (LM Studio base), PARALLEL (default 1), NO_PROGRESS (default 900),
#   N_POS (San Diego repeats, default 5), RESULTS (verdict log path),
#   LOGDIR (per-run pytest logs, default /tmp).
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
CELL="$(echo "${PROVIDER}-${MODEL}" | tr '/:' '__')"
RESULTS="${RESULTS:-/tmp/grind_${CELL}_results.txt}"

run_one() {
  # $1=label $2=region(or empty) $3=expect(positive|negative)
  local label="$1" region="$2" expect="$3"
  local log="${LOGDIR}/grind_${CELL}_${label}.log"
  local t0 t1 verdict
  t0=$(date +%s)
  CLIO_RUN_LIVE=1 CLIO_GACT_URL="$GACT_URL" CLIO_KIT_PATH=/home/jcernuda/clio-kit \
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
}

echo "=== GRIND ${CELL} ctx=${CTX} $(date '+%F %T') ===" | tee -a "$RESULTS"
for i in $(seq 1 "$N_POS"); do run_one "sandiego_${i}" "" positive; done
run_one "seattle_alt" "Seattle" positive
run_one "chicago_neg" "Chicago" negative
echo "=== GRIND ${CELL} DONE $(date '+%F %T') ===" | tee -a "$RESULTS"
echo "--- summary ---"; grep -E '^(PASS|FAIL)' "$RESULTS" | tail -20
