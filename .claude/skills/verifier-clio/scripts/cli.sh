#!/usr/bin/env bash
# Run one CLI command the way a person would, in its own tmux server, and keep
# the pane as evidence.
#
#   bash .claude/skills/verifier-clio/scripts/cli.sh NAME COMMAND...
#   bash .claude/skills/verifier-clio/scripts/cli.sh plugin-check uv run --no-sync clio-agent plugin check external/clio-agent-marketplace
#
# Prints the pane (with the exit status as its last line) and saves it to
# $VERIFY_DIR/cli-NAME.txt. A command that does not finish in CLI_TIMEOUT
# seconds (default 600) is interrupted with Ctrl-C and reported as such.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
RUN="${VERIFY_DIR:-$ROOT/.cloud-run/verify/current}"
NAME="$1"; shift
SOCK="clio-verify"
SESSION="v-$NAME-$$"
mkdir -p "$RUN"
printf -v CMD '%q ' "$@"
tmux -L "$SOCK" new-session -d -s "$SESSION" -x 220 -y 60 -c "$ROOT" \
  "bash -lc '$CMD; rc=\$?; echo; echo \"[exit \$rc]\"'; sleep 3600"
deadline=$((SECONDS + ${CLI_TIMEOUT:-600}))
while ! tmux -L "$SOCK" capture-pane -p -t "$SESSION" -S - | grep -q '^\[exit [0-9]*\]$'; do
  if [ $SECONDS -ge $deadline ]; then
    tmux -L "$SOCK" send-keys -t "$SESSION" C-c
    sleep 2
    echo "[interrupted after ${CLI_TIMEOUT:-600}s]" >> "$RUN/cli-$NAME.txt"
    break
  fi
  sleep 1
done
tmux -L "$SOCK" capture-pane -p -t "$SESSION" -S - | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}' >> "$RUN/cli-$NAME.txt"
tmux -L "$SOCK" kill-session -t "$SESSION"
tmux -L "$SOCK" list-sessions >/dev/null 2>&1 || true
cat "$RUN/cli-$NAME.txt"
echo "[evidence] $RUN/cli-$NAME.txt"
