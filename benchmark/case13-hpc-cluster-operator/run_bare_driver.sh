#!/usr/bin/env bash
# Bare (non-pytest) live-cell runner for case13. See bare_driver.py's module
# docstring and GRIND-HANDOFF.md for why this exists (clio-agent#1258:
# pytest-launched gact server crashes clio-core's CTE daemon on this box;
# this script's launch path does not).
#
# Usage (run from anywhere; cds into the worktree itself):
#   ./run_bare_driver.sh s1_capability
#   ./run_bare_driver.sh s2_instrumentation
#   ./run_bare_driver.sh s3_visualization
#   ./run_bare_driver.sh s4_honest_negative
#
# Exit code mirrors bare_driver.py: 0 = all matchers passed, 1 = a matcher
# failed (or the run itself errored), 2 = bad usage.
set -u
SCENARIO="${1:?usage: run_bare_driver.sh <s1_capability|s2_instrumentation|s3_visualization|s4_honest_negative>}"

# --- 1. deployment identity (re-sourced fresh EVERY invocation -- never
#     cache/hardcode the session id or generation; it rotates) ------------
. "/d/Libraries/Documents/projects/clio-deployments/bin/deployment-env.sh" \
  "/d/Libraries/Documents/projects/clio-deployments/ares-p5run2"

# --- 2. relay transport (see GRIND-HANDOFF.md section 1 for why each of
#     these has the value it does) ----------------------------------------
export CLIO_RELAY_EXE="C:\\Users\\jaime\\AppData\\Local\\clio-deploy\\ares-p5run2\\bin\\clio-relay.exe"
export CLIO_RELAY_MCP_URL='http://127.0.0.1:18795/mcp'
export CLIO_RELAY_HTTP_URL='http://127.0.0.1:50837'
export CLIO_RELAY_CLUSTER='ares-p5run2'

# --- 3. workspace / file-tool policy --------------------------------------
export CLIO_CASE13_WORKSPACE_ROOT='D:\Libraries\Documents\projects\clio-runs\case13-v170'
export CLIO_ALLOWED_ROOTS='D:\Libraries\Documents\projects\clio-runs;D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate;C:\Users\jaime;/tmp'

# --- 4. match the pytest fixture's own env exactly (conftest.py's
#     gact_server fixture sets these on the spawned subprocess; replicate
#     them here so the bare path is a true apples-to-apples substitute) ----
export CLIO_DEBUG=med
export CLIO_KIT_PATH='C:\Users\jaime\clio-kit'
export CLIO_SEMANTIC_TRACE_BACKEND=file
export CLIO_SEMANTIC_TRACE_PATH="D:\\Libraries\\Documents\\projects\\clio_develop_workspace\\case13-gate\\.grind\\traces\\bare-${SCENARIO}"

cd /d/Libraries/Documents/projects/clio_develop_workspace/case13-gate
mkdir -p "$CLIO_SEMANTIC_TRACE_PATH" 2>/dev/null || true

PORT="${CLIO_BARE_DRIVER_PORT:-17986}"
export CLIO_BARE_DRIVER_PORT="$PORT"
LOG="C:/Users/jaime/AppData/Local/Temp/bare_driver_server.log"
LOG_POSIX="/c/Users/jaime/AppData/Local/Temp/bare_driver_server.log"
echo "starting gact server on :$PORT for bare $SCENARIO run"
# NOTE: bare `uv run` (no --no-sync) deliberately, matching the fixture's
# own subprocess invocation exactly -- see GRIND-HANDOFF.md trap list for
# why (and why --no-sync on the OUTER uv run below is fine/expected: that's
# this driver script's own process, not the server subprocess).
uv run clio-agent-gact --host 127.0.0.1 --port "$PORT" > "$LOG_POSIX" 2>&1 &
SRV_PID=$!
echo "server pid=$SRV_PID"

for i in $(seq 1 90); do
  if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/v1/health" | grep -q 200; then
    echo "healthy after ${i}s"
    break
  fi
  sleep 1
done

echo "--- crash signature at boot? (grep BEFORE trusting this boot) ---"
grep -iE "runtime_crash|0xC0000409|3221226505" "$LOG_POSIX" || echo "none at boot"
echo "--- discovery result (must be reason=None federation=present, NOT relay_catalog_discovery_failed) ---"
grep -E "relay first discovery" "$LOG_POSIX" || echo "!! no discovery line found -- relay not configured?"
echo "--- TOOLSET-INVENTORY gaps (should be EMPTY after clio-agent#1258's sibling mount-gap fix lands) ---"
grep "custom_agent_tool_unavailable" "$LOG_POSIX" || echo "none -- full tool grant"

echo "--- running bare driver (no pytest) ---"
uv run --no-sync python "benchmark/case13-hpc-cluster-operator/bare_driver.py" "$SCENARIO"
PYEXIT=$?
echo "driver exit code: $PYEXIT"

echo "--- crash signature after run? ---"
grep -iE "runtime_crash|0xC0000409|3221226505" "$LOG_POSIX" || echo "none after run"

echo "killing server pid=$SRV_PID"
kill "$SRV_PID" 2>/dev/null || true
sleep 2
kill -9 "$SRV_PID" 2>/dev/null || true

exit $PYEXIT
