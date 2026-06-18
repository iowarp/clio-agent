#!/usr/bin/env bash
# CLIO uninstaller (Linux / macOS).
#
# Undoes install.sh: stops the server, removes the launcher, and
# deletes the install prefix. Pass --purge to also remove CLIO's user
# state + config (~/.config/clio-agent and ~/.config/gact).
#
#   Flags:
#     --yes     skip the confirmation prompt (non-interactive)
#     --purge   also remove ~/.config/clio-agent (sessions, workspaces,
#               blueprints, ARC) AND ~/.config/gact (TUI config/themes)
#
#   Environment overrides (must match the install):
#     CLIO_PREFIX   install root      (default: $HOME/.local/share/clio)
#     CLIO_PORT     server port       (default: 17800)
#     CLIO_BIN_DIR  launcher location (default: $HOME/.local/bin)
set -euo pipefail

CLIO_PREFIX="${CLIO_PREFIX:-$HOME/.local/share/clio}"
CLIO_PORT="${CLIO_PORT:-17800}"
CLIO_BIN_DIR="${CLIO_BIN_DIR:-$HOME/.local/bin}"
PIDFILE="$CLIO_PREFIX/clio-server.pid"
CLIO_CONFIG="$HOME/.config/clio-agent"
GACT_CONFIG="$HOME/.config/gact"
LAUNCHER="$CLIO_BIN_DIR/clio"

ASSUME_YES=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --purge)  PURGE=1 ;;
    *) echo "uninstall: unknown flag '$arg' (want --yes, --purge)" >&2; exit 2 ;;
  esac
done

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RESET='\033[0m'
say()  { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!!${RESET} %s\n" "$*" >&2; }

echo ""
echo "CLIO uninstall — the following will be removed:"
echo "  install prefix:  $CLIO_PREFIX"
echo "  launcher:        $LAUNCHER"
if [[ "$PURGE" -eq 1 ]]; then
  echo "  clio config:     $CLIO_CONFIG  (--purge)"
  echo "  gact config:     $GACT_CONFIG  (--purge)"
else
  echo "  clio config:     $CLIO_CONFIG  (KEPT — pass --purge to remove)"
  echo "  gact config:     $GACT_CONFIG  (KEPT — pass --purge to remove)"
fi
echo ""

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Proceed? [y/N] " ans
  case "$ans" in
    y|Y) ;;
    *) warn "aborted"; exit 1 ;;
  esac
fi

# ---- stop the server -------------------------------------------------
server_pid=""
if [[ -f "$PIDFILE" ]]; then
  p="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then server_pid="$p"; fi
fi
if [[ -z "$server_pid" ]] && command -v lsof >/dev/null 2>&1; then
  server_pid="$(lsof -ti "tcp:$CLIO_PORT" -sTCP:LISTEN 2>/dev/null | head -n1 || true)"
fi
if [[ -n "$server_pid" ]]; then
  say "Stopping CLIO server (pid $server_pid)"
  kill "$server_pid" 2>/dev/null || true
  sleep 1
  kill -0 "$server_pid" 2>/dev/null && kill -9 "$server_pid" 2>/dev/null || true
else
  say "No running CLIO server found"
fi

# Sweep leftover server processes started from this prefix.
if command -v pkill >/dev/null 2>&1; then
  pkill -f "$CLIO_PREFIX/clio-agent/.venv/bin/clio-agent-gact" 2>/dev/null || true
fi

# ---- remove files ----------------------------------------------------
if [[ -e "$LAUNCHER" ]]; then
  say "Removing $LAUNCHER"
  rm -f "$LAUNCHER"
fi
if [[ -d "$CLIO_PREFIX" ]]; then
  say "Removing $CLIO_PREFIX"
  rm -rf "$CLIO_PREFIX"
fi
if [[ "$PURGE" -eq 1 ]]; then
  [[ -d "$CLIO_CONFIG" ]] && { say "Removing $CLIO_CONFIG"; rm -rf "$CLIO_CONFIG"; }
  [[ -d "$GACT_CONFIG" ]] && { say "Removing $GACT_CONFIG"; rm -rf "$GACT_CONFIG"; }
fi

say "CLIO uninstalled."
if [[ "$PURGE" -ne 1 ]] && { [[ -d "$CLIO_CONFIG" ]] || [[ -d "$GACT_CONFIG" ]]; }; then
  echo "  config kept ($CLIO_CONFIG, $GACT_CONFIG) — re-run with --purge to remove"
fi
