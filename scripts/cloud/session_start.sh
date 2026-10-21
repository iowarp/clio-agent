#!/bin/bash
# SessionStart hook for Claude Code cloud sessions (wired in .claude/settings.json).
#
# Installs the Python environment and the gact-tui workspace the first time, then
# skips both while their lockfiles are unchanged. Outside a cloud session it does
# nothing, so local developers keep their own environments.

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
cd "$ROOT" || exit 0
STATE="$ROOT/.cloud-run"
mkdir -p "$STATE"
LOG="$STATE/session_start.log"
: > "$LOG"

stamp_ok() {  # stamp_ok <stamp file> <inputs...>
  local stamp="$1"; shift
  [ -f "$stamp" ] && sha256sum --check --status "$stamp" 2>/dev/null
}

stamp_write() {  # stamp_write <stamp file> <inputs...>
  local stamp="$1"; shift
  sha256sum "$@" > "$stamp" 2>/dev/null || rm -f "$stamp"
}

# The UI lives in the gact-tui submodule pinned by this branch.
if [ ! -f external/gact-tui/package.json ]; then
  git submodule update --init --depth 1 external/gact-tui >>"$LOG" 2>&1 \
    || echo "session_start: could not fetch external/gact-tui" >>"$LOG"
fi

python_env() {
  local stamp="$STATE/uv.sha256"
  if ! stamp_ok "$stamp"; then
    rm -f "$stamp"
    if uv sync --extra dev --extra claude-code >>"$LOG" 2>&1; then
      stamp_write "$stamp" pyproject.toml uv.lock scripts/cloud/session_start.sh
    else
      echo "session_start: uv sync failed (see $LOG)" >&2
    fi
  fi
}

ui_env() {
  [ -f external/gact-tui/package.json ] || return 0
  local stamp="$STATE/pnpm.sha256"
  if ! stamp_ok "$stamp"; then
    rm -f "$stamp"
    if (cd external/gact-tui && pnpm install --frozen-lockfile) >>"$LOG" 2>&1; then
      stamp_write "$stamp" external/gact-tui/package.json external/gact-tui/pnpm-lock.yaml \
        scripts/cloud/session_start.sh
    else
      echo "session_start: pnpm install failed (see $LOG)" >&2
    fi
  fi
}

python_env &
ui_env &
wait

echo "Cloud session ready. Start the stack with: bash scripts/cloud/stack.sh start"
exit 0
