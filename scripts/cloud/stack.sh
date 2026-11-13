#!/bin/bash
# Start, stop and inspect the local CLIO stack inside a cloud session:
#   the GACT server (clio-agent serve), the gact-tui web dev server and the
#   browser fixture site (tests/browser/fixtures/site/server.py).
#
#   bash scripts/cloud/stack.sh start     # both, waits until they answer
#   bash scripts/cloud/stack.sh stop      # stops what this script started
#   bash scripts/cloud/stack.sh restart
#   bash scripts/cloud/stack.sh status
#   bash scripts/cloud/stack.sh logs      # last lines of both logs
#
# Ports: AGENT_PORT (default 8787, the UI's default service address) and
# UI_PORT (default 5173); FIXTURE_PORT (default 8790) serves the browser fixture
# site as a.fixture.localhost and b.fixture.localhost, and FIXTURE_TLS_PORT (default
# 8791) as https://c.fixture.localhost with a self-signed certificate. The model
# provider defaults to claude_code, which uses the Claude CLI that cloud sessions
# ship already signed in; override with
# CLIO_LM_PROVIDER / CLIO_LM_MODEL. Runtime state stays in .cloud-run/.

set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
STATE="$ROOT/.cloud-run"
mkdir -p "$STATE"
AGENT_PORT="${AGENT_PORT:-8787}"
UI_PORT="${UI_PORT:-5173}"
FIXTURE_PORT="${FIXTURE_PORT:-8790}"
FIXTURE_TLS_PORT="${FIXTURE_TLS_PORT:-8791}"
export CLIO_LM_PROVIDER="${CLIO_LM_PROVIDER:-claude_code}"
export CLIO_LM_MODEL="${CLIO_LM_MODEL:-claude-sonnet-5-5}"
# CLIO_CLOUD_ISOLATE=1 gives this stack its own CLIO config, data, state and cache
# folders under .cloud-run/xdg, so several stacks (one per worktree, on their own
# ports) can run side by side without sharing sessions or saved connections.
# HOME is left alone, so the signed-in Claude CLI keeps working.
if [ "${CLIO_CLOUD_ISOLATE:-0}" = "1" ]; then
  for role in CONFIG DATA STATE CACHE; do
    dir="$STATE/xdg/$(echo "$role" | tr '[:upper:]' '[:lower:]')"
    mkdir -p "$dir"
    export "XDG_${role}_HOME=$dir"
  done
fi
# The cloud stack runs as root, where Chromium's sandbox cannot start (browser ledger BR11).
export CLIO_BROWSER_ALLOW_UNSANDBOXED="${CLIO_BROWSER_ALLOW_UNSANDBOXED:-1}"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

launch() {  # launch <pidfile> <logfile> <dir> <command...>
  # setsid makes the command a session and process-group leader. The pid is
  # written from inside that session, so stop_one can signal the whole group.
  local pidf="$1" logf="$2" dir="$3"; shift 3
  (cd "$dir" && setsid bash -c 'echo $$ > "$0"; exec "$@"' "$pidf" "$@" >"$logf" 2>&1 < /dev/null &)
  for _ in $(seq 1 20); do [ -s "$pidf" ] && return 0; sleep 0.1; done
}

wait_http() {  # wait_http <url> <seconds>
  local url="$1" deadline=$((SECONDS + $2))
  until curl -s -o /dev/null "$url"; do
    [ $SECONDS -ge $deadline ] && return 1
    sleep 2
  done
}

start_agent() {
  if alive "$STATE/agent.pid"; then echo "agent: already running"; return 0; fi
  launch "$STATE/agent.pid" "$STATE/agent.log" "$ROOT" \
    uv run --no-sync clio-agent serve --host 127.0.0.1 --port "$AGENT_PORT"
  if wait_http "http://127.0.0.1:$AGENT_PORT/v1/health" 180; then
    echo "agent: http://127.0.0.1:$AGENT_PORT (provider $CLIO_LM_PROVIDER, model $CLIO_LM_MODEL)"
  else
    echo "agent: did not answer within 180s; see $STATE/agent.log"; return 1
  fi
}

start_ui() {
  if alive "$STATE/ui.pid"; then echo "ui: already running"; return 0; fi
  local web="$ROOT/external/gact-tui/web"
  [ -f "$web/package.json" ] || { echo "ui: $web missing (run scripts/cloud/session_start.sh)"; return 1; }
  launch "$STATE/ui.pid" "$STATE/ui.log" "$web" pnpm dev --port "$UI_PORT" --strictPort
  if wait_http "http://127.0.0.1:$UI_PORT/" 120; then
    echo "ui: http://127.0.0.1:$UI_PORT"
  else
    echo "ui: did not answer within 120s; see $STATE/ui.log"; return 1
  fi
}

start_fixture() {
  if alive "$STATE/fixture.pid"; then echo "fixture: already running"; return 0; fi
  launch "$STATE/fixture.pid" "$STATE/fixture.log" "$ROOT" \
    python3 tests/browser/fixtures/site/server.py --port "$FIXTURE_PORT" \
    --tls-port "$FIXTURE_TLS_PORT" --state "$STATE/fixture-site"
  if wait_http "http://127.0.0.1:$FIXTURE_PORT/" 20; then
    echo "fixture: http://a.fixture.localhost:$FIXTURE_PORT and http://b.fixture.localhost:$FIXTURE_PORT"
  else
    echo "fixture: did not answer within 20s; see $STATE/fixture.log"; return 1
  fi
}

stop_one() {  # stop_one <name>
  local pidf="$STATE/$1.pid"
  if alive "$pidf"; then
    local pgid; pgid="$(cat "$pidf")"
    kill -TERM -- "-$pgid" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 -- "-$pgid" 2>/dev/null || break; sleep 0.5; done
    if kill -0 -- "-$pgid" 2>/dev/null; then
      kill -KILL -- "-$pgid" 2>/dev/null
      echo "$1: force-stopped (did not exit within 10s)"
    else
      echo "$1: stopped"
    fi
  else
    echo "$1: not running"
  fi
  rm -f "$pidf"
}

status() {
  for n in agent ui fixture; do
    if alive "$STATE/$n.pid"; then echo "$n: running (pid $(cat "$STATE/$n.pid"))"; else echo "$n: stopped"; fi
  done
}

case "${1:-status}" in
  start) start_fixture && start_agent && start_ui ;;
  stop) stop_one ui; stop_one agent; stop_one fixture ;;
  restart) stop_one ui; stop_one agent; stop_one fixture; start_fixture && start_agent && start_ui ;;
  status) status ;;
  logs) tail -n 40 "$STATE/agent.log" "$STATE/ui.log" "$STATE/fixture.log" 2>/dev/null ;;
  *) echo "usage: $0 start|stop|restart|status|logs"; exit 2 ;;
esac
