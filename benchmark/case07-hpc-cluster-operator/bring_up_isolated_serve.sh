#!/usr/bin/env bash
# case07 (HPC cluster operator) ISOLATED serve bring-up.
#
#   worktree    : D:/clio-cluster-case, branch feat/cluster-operator-case
#   port        : 17970 (the ares-mission serve on :17900 is NEVER touched)
#   door        : the p5local MCP door on :18795 (already running -- see
#                 D:/relay-p5local/door.sh; this script does not start it)
#   owned API   : the p5local owned session's remote API, port sourced live
#                 from session.env (re-minted independently of this case)
#   provider    : claude_code / sonnet (GOAL.md guardrail cell)
#
# This script only PREPARES the environment and STARTS the isolated serve. It
# never stops/restarts the shared door, never touches :17900, and never writes
# outside this worktree. See ENV.md for the full contract this mirrors
# (adapted from D:/relay-p5local/serve-ares-env.sh, the :17900 recipe).
set -u

WORKTREE='/d/clio-cluster-case'
CASE_DIR="$WORKTREE/benchmark/case07-hpc-cluster-operator"
PORT="${CLIO_CASE07_PORT:-17970}"

cd "$WORKTREE" || exit 1

# --- relay door + owned-session identity (sourced live, never hardcoded) ---
if [ ! -f /d/relay-p5local/api-token.txt ]; then
  echo "bring_up_isolated_serve: missing /d/relay-p5local/api-token.txt -- is the p5local relay deployed?" >&2
  exit 1
fi
export CLIO_RELAY_API_TOKEN="$(cat /d/relay-p5local/api-token.txt)"

if [ ! -f /d/relay-p5local/session.env ]; then
  echo "bring_up_isolated_serve: missing /d/relay-p5local/session.env -- no owned session minted yet (run: clio-relay session start)" >&2
  exit 1
fi
. /d/relay-p5local/session.env

export CLIO_RELAY_MCP_URL='http://127.0.0.1:18795/mcp'
export CLIO_RELAY_CLUSTER="${CLIO_RELAY_OWNER_SESSION_CLUSTER:-ares-p5run2}"
export CLIO_RELAY_HTTP_URL="http://127.0.0.1:${CLIO_RELAY_OWNER_SESSION_API_PORT:-8796}"

# --- isolation: port + file-tool access + durable, worktree-local artifacts ---
export CLIO_GACT_FIXTURE_PORT="$PORT"
export CLIO_ALLOWED_ROOTS="D:\\clio-cluster-case;${HOME};/tmp"
export CLIO_CASE07_WORKSPACE_ROOT="$CASE_DIR/runs/workspace"
mkdir -p "$CASE_DIR/runs/workspace" "$CASE_DIR/runs"

# --- guardrail cell (per-cell PUT /v1/providers/lm still wins under pytest;
#     these only matter for THIS script's direct, non-pytest launch) ---
export CLIO_LM_PROVIDER='claude_code'
export CLIO_LM_MODEL='sonnet'

# --- sanity: the shared p5local door must already be reachable. This script
#     does not start it (one door serves one CLIO_RELAY_INPUT_WORKSPACE_ROOT;
#     starting a second one from here would fight the shared deployment). ---
if command -v curl >/dev/null 2>&1; then
  if ! curl -s -o /dev/null -m 5 "http://127.0.0.1:18795/mcp"; then
    echo "bring_up_isolated_serve: WARNING -- p5local door not reachable on :18795." >&2
    echo "  Start it first (a separate terminal, NOT this script): . D:/relay-p5local/door.sh" >&2
  fi
fi

echo "bring_up_isolated_serve: starting isolated clio-agent serve on 127.0.0.1:$PORT"
echo "  workspace root : $CLIO_CASE07_WORKSPACE_ROOT"
echo "  relay cluster  : $CLIO_RELAY_CLUSTER (owned session ${CLIO_RELAY_OWNER_SESSION_ID:-<unset>})"
echo "  provider/model : $CLIO_LM_PROVIDER / $CLIO_LM_MODEL"

exec uv run src/clio_agent/ui/cli.py serve --host 127.0.0.1 --port "$PORT"
