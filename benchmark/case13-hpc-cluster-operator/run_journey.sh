#!/usr/bin/env bash
# Journey runner for the v1.7.0 final science deliverables (darshan_journey,
# paraview_images). Same server recipe as run_bare_driver.sh (clio-agent#1258:
# never pytest on this box); see journey_driver.py for what a journey does.
#
# Usage:
#   ./run_journey.sh darshan_journey
#   ./run_journey.sh paraview_images
set -u
JOURNEY="${1:?usage: run_journey.sh <darshan_journey|paraview_images>}"

# --- 1. deployment identity (re-sourced fresh EVERY invocation) ------------
. "/d/Libraries/Documents/projects/clio-deployments/bin/deployment-env.sh" \
  "/d/Libraries/Documents/projects/clio-deployments/ares-p5run2"

# --- 2. relay transport ----------------------------------------------------
export CLIO_RELAY_EXE="C:\\Users\\jaime\\AppData\\Local\\clio-deploy\\ares-p5run2\\bin\\clio-relay.exe"
export CLIO_RELAY_MCP_URL='http://127.0.0.1:18795/mcp'
export CLIO_RELAY_HTTP_URL='http://127.0.0.1:50837'
export CLIO_RELAY_CLUSTER='ares-p5run2'

# --- 3. workspace / file-tool policy --------------------------------------
export CLIO_CASE13_WORKSPACE_ROOT='D:\Libraries\Documents\projects\clio-runs\case13-v170'
export CLIO_ALLOWED_ROOTS='D:\Libraries\Documents\projects\clio-runs;D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate;C:\Users\jaime;/tmp'

# --- 4. match the pytest fixture's env exactly -----------------------------
export CLIO_DEBUG=med
export PYTHONUNBUFFERED=1
export CLIO_KIT_PATH='C:\Users\jaime\clio-kit'
export CLIO_SEMANTIC_TRACE_BACKEND=file
export CLIO_SEMANTIC_TRACE_PATH="D:\\Libraries\\Documents\\projects\\clio_develop_workspace\\case13-gate\\.grind\\traces\\journey-${JOURNEY}"

cd /d/Libraries/Documents/projects/clio_develop_workspace/case13-gate
mkdir -p "$CLIO_SEMANTIC_TRACE_PATH" 2>/dev/null || true

PORT="${CLIO_BARE_DRIVER_PORT:-17986}"
export CLIO_BARE_DRIVER_PORT="$PORT"
LOG_POSIX="/c/Users/jaime/AppData/Local/Temp/bare_driver_server.log"
echo "starting gact server on :$PORT for journey $JOURNEY"
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

echo "--- crash signature at boot? ---"
grep -iE "runtime_crash|0xC0000409|3221226505" "$LOG_POSIX" || echo "none at boot"
echo "--- discovery result (must be reason=None federation=present) ---"
grep -E "relay first discovery" "$LOG_POSIX" || echo "!! no discovery line found -- relay not configured?"
echo "--- TOOLSET-INVENTORY gaps (should be EMPTY) ---"
grep "custom_agent_tool_unavailable" "$LOG_POSIX" || echo "none -- full tool grant"

echo "--- running journey driver (no pytest) ---"
uv run --no-sync python "benchmark/case13-hpc-cluster-operator/journey_driver.py" "$JOURNEY"
PYEXIT=$?
echo "driver exit code: $PYEXIT"

echo "--- crash signature after run? ---"
grep -iE "runtime_crash|0xC0000409|3221226505" "$LOG_POSIX" || echo "none after run"

# preserve this run's server log next to its results
RUN_DIR="/d/Libraries/Documents/projects/clio-runs/case13-v170/${JOURNEY}_barepy"
mkdir -p "$RUN_DIR" 2>/dev/null || true
cp "$LOG_POSIX" "$RUN_DIR/server.log" 2>/dev/null || true

echo "killing server pid=$SRV_PID"
kill "$SRV_PID" 2>/dev/null || true
sleep 2
kill -9 "$SRV_PID" 2>/dev/null || true

exit $PYEXIT
