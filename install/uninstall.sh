#!/usr/bin/env bash
# CLIO uninstaller (Linux / macOS).
#
# Undoes install.sh: stops the server, removes the launcher, and
# removes known application payloads. Pass --purge to also remove the
# resolved Agent config directory. Data, state, and other products are retained.
#
#   Flags:
#     --yes     skip the confirmation prompt (non-interactive)
#     --purge   also remove Agent configuration (including stored credentials)
#
#   Environment overrides (must match the install):
#     CLIO_PREFIX   install root      (default: $HOME/.local/share/clio)
#     CLIO_PORT     server port       (default: 17800)
#     CLIO_BIN_DIR  launcher location (default: $HOME/.local/bin)
set -euo pipefail

if [[ "$(uname -s)" = Darwin* ]]; then
  agent_data_default="$HOME/Library/Application Support/clio-agent/data"
else
  xdg_data="${XDG_DATA_HOME:-}"
  case "$xdg_data" in /*) ;; *) xdg_data="$HOME/.local/share" ;; esac
  agent_data_default="$xdg_data/clio-agent"
fi
agent_data="${CLIO_AGENT_DATA_DIR:-${CLIO_AGENT_HOME:+$CLIO_AGENT_HOME/data}}"
agent_data="${agent_data:-${CLIO_USER_DIR:+$CLIO_USER_DIR/data}}"
agent_data="${agent_data:-$agent_data_default}"
case "$agent_data" in /*) ;; *) echo "Agent data root must be absolute" >&2; exit 2 ;; esac
requested_prefix="${CLIO_PREFIX:-}"
CLIO_PREFIX="${CLIO_PREFIX:-$agent_data/app}"
if [[ -z "$requested_prefix" && -z "${CLIO_AGENT_HOME:-}${CLIO_AGENT_DATA_DIR:-}${CLIO_USER_DIR:-}" && ! -d "$CLIO_PREFIX/clio-agent/.venv" && -d "$HOME/.local/share/clio/clio-agent/.venv" ]]; then
  CLIO_PREFIX="$HOME/.local/share/clio"
fi
CLIO_PORT="${CLIO_PORT:-17800}"
CLIO_BIN_DIR="${CLIO_BIN_DIR:-$HOME/.local/bin}"
CLIO_STATE="$CLIO_PREFIX"
CLIO_CONFIG="$HOME/.config/clio-agent"
AGENT_PYTHON="$CLIO_PREFIX/clio-agent/.venv/bin/python"
if [[ -x "$AGENT_PYTHON" ]]; then
  # Resolve before deleting the installed interpreter. Older releases retain
  # the legacy paths above if they do not expose the namespace helpers.
  resolved_state="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_state_dir; print(user_state_dir())' 2>/dev/null)" && CLIO_STATE="$resolved_state"
  resolved_config="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_config_dir; print(user_config_dir())' 2>/dev/null)" && CLIO_CONFIG="$resolved_config"
fi
HOST_ID="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo localhost)"
PIDFILE="$CLIO_STATE/clio-server.$HOST_ID.pid"
for candidate in "$CLIO_STATE/clio-server.pid" "$CLIO_PREFIX/clio-server.$HOST_ID.pid" "$CLIO_PREFIX/clio-server.pid"; do
  [[ -f "$PIDFILE" ]] || PIDFILE="$candidate"
done

if [[ "$PURGE" -eq 1 ]]; then
  case "$CLIO_CONFIG" in
    /*/clio-agent|/*/clio-agent/config) ;;
    *) echo "Refusing to purge a custom config root: $CLIO_CONFIG. Remove its contents explicitly after review." >&2; exit 2 ;;
  esac
fi
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
echo "  application payloads in: $CLIO_PREFIX (unknown files retained)"
echo "  launcher:        $LAUNCHER"
if [[ "$PURGE" -eq 1 ]]; then
  echo "  clio config:     $CLIO_CONFIG  (--purge)"
else
  echo "  clio config:     $CLIO_CONFIG  (KEPT — pass --purge to remove)"
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

# Sweep leftover server processes started from this prefix. Match both the
# current entrypoint (clio-agent serve) and the retired pre-upgrade one
# (clio-agent-gact) so an old server left running is still cleaned up.
if command -v pkill >/dev/null 2>&1; then
  pkill -f "$CLIO_PREFIX/clio-agent/.venv/bin/clio-agent(-gact)?" 2>/dev/null || true
fi

# ---- remove files ----------------------------------------------------
if [[ -e "$LAUNCHER" ]]; then
  say "Removing $LAUNCHER"
  rm -f "$LAUNCHER"
fi
if [[ -d "$CLIO_PREFIX" ]]; then
  say "Removing installed application payloads in $CLIO_PREFIX"
  rm -rf -- "$CLIO_PREFIX/clio-agent/.venv" "$CLIO_PREFIX/clio-agent/web"
  rm -f -- "$CLIO_PREFIX/gact" "$CLIO_PREFIX/uninstall.sh"
  rmdir -- "$CLIO_PREFIX/clio-agent" "$CLIO_PREFIX" 2>/dev/null || true
  [[ ! -d "$CLIO_PREFIX" ]] || warn "Retained user data and unknown files in $CLIO_PREFIX."
fi
if [[ "$PURGE" -eq 1 ]]; then
  [[ -d "$CLIO_CONFIG" ]] && { say "Removing $CLIO_CONFIG"; rm -rf "$CLIO_CONFIG"; }
  # gact is a separate product; its configuration is retained.
fi

say "CLIO uninstalled."
if [[ "$PURGE" -ne 1 && -d "$CLIO_CONFIG" ]]; then
  echo "  Agent config kept ($CLIO_CONFIG) — re-run with --purge to remove"
fi
