#!/usr/bin/env bash
# Start a verification run: a fresh run directory, the heads under test, and a
# freshly restarted stack that answers on every route the drivers use.
#
#   bash .claude/skills/verifier-clio/scripts/up.sh [--keep]   # --keep: reuse a running stack
#
# Prints the run directory; later scripts find it through .cloud-run/verify/current.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
RUN="$ROOT/.cloud-run/verify/$(date -u +%Y%m%dT%H%M%SZ)"
cd "$ROOT"
mkdir -p "$RUN"
ln -sfn "$RUN" "$ROOT/.cloud-run/verify/current"

{
  for repo in . external/gact-tui external/clio-agent-marketplace; do
    printf '%s %s %s\n' "$repo" "$(git -C "$ROOT/$repo" rev-parse --short HEAD)" \
      "$(git -C "$ROOT/$repo" status --porcelain --untracked-files=no | wc -l | tr -d ' ') uncommitted"
  done
} > "$RUN/heads.txt"
cat "$RUN/heads.txt"

if [ "${1:-}" != "--keep" ]; then
  # stack.sh keeps its children attached to its stdout; detach so this script returns.
  setsid bash "$ROOT/scripts/cloud/stack.sh" restart > "$RUN/stack-restart.log" 2>&1 < /dev/null &
  sleep 5
fi
# /v1/health answers before the session and workspace routes do (they 503 while
# the task runtime starts), so wait on the routes the drivers actually call.
uv run --no-sync python "$ROOT/.claude/skills/verifier-clio/scripts/agent.py" ready
for _ in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5173/)" = 200 ] && break
  sleep 3
done
bash "$ROOT/scripts/cloud/stack.sh" status | tee "$RUN/stack-status.txt"
echo "$RUN"
