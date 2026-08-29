#!/usr/bin/env bash
# Continuation runner: post a follow-up message onto an EXISTING journey session.
# Usage: ./continue_journey.sh <journey> <session_id> <prompt_file>
set -u
JOURNEY="${1:?usage: continue_journey.sh <journey> <session_id> <prompt_file>}"
SESSION="${2:?missing session_id}"
PROMPT_FILE="${3:?missing prompt file}"

. "/d/Libraries/Documents/projects/clio-deployments/bin/deployment-env.sh" \
  "/d/Libraries/Documents/projects/clio-deployments/ares-p5run2"

export CLIO_RELAY_EXE="C:\\Users\\jaime\\AppData\\Local\\clio-deploy\\ares-p5run2\\bin\\clio-relay.exe"
export CLIO_RELAY_MCP_URL='http://127.0.0.1:18795/mcp'
export CLIO_RELAY_HTTP_URL='http://127.0.0.1:50837'
export CLIO_RELAY_CLUSTER='ares-p5run2'

export CLIO_CASE13_WORKSPACE_ROOT='D:\Libraries\Documents\projects\clio-runs\case13-v170'
export CLIO_ALLOWED_ROOTS='D:\Libraries\Documents\projects\clio-runs;D:\Libraries\Documents\projects\clio_develop_workspace\case13-gate;C:\Users\jaime;/tmp'

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
echo "starting gact server on :$PORT for continuation of $JOURNEY ($SESSION)"
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
echo "--- discovery result ---"
grep -E "relay first discovery" "$LOG_POSIX" || echo "(discovery line not yet written -- check later)"
echo "--- TOOLSET-INVENTORY gaps ---"
grep "custom_agent_tool_unavailable" "$LOG_POSIX" || echo "none -- full tool grant"

echo "--- running continuation driver ---"
uv run --no-sync python "benchmark/case13-hpc-cluster-operator/continue_journey.py" "$JOURNEY" "$SESSION" "$PROMPT_FILE"
PYEXIT=$?
echo "driver exit code: $PYEXIT"

echo "--- crash signature after run? ---"
grep -iE "runtime_crash|0xC0000409|3221226505" "$LOG_POSIX" || echo "none after run"

RUN_DIR="/d/Libraries/Documents/projects/clio-runs/case13-v170/${JOURNEY}_barepy"
mkdir -p "$RUN_DIR" 2>/dev/null || true
cp "$LOG_POSIX" "$RUN_DIR/server.continue.log" 2>/dev/null || true

echo "killing server pid=$SRV_PID"
kill "$SRV_PID" 2>/dev/null || true
sleep 2
kill -9 "$SRV_PID" 2>/dev/null || true

exit $PYEXIT
