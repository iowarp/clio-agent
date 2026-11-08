#!/usr/bin/env bash
# CLIO uninstaller (Linux / macOS).
#
# Undoes install.sh: stops the server, removes the launcher, and
# removes known application payloads. Pass --purge to also remove the
# resolved Agent config directory. Data, state, and other products are retained.
#
#   Flags:
#     --yes           skip the confirmation prompt (non-interactive)
#     --purge         also remove Agent configuration (including stored credentials)
#     --purge-caches  also remove the Agent cache root (document runtime, LibreOffice,
#                     tool caches; macOS: ~/Library/Caches/clio-agent). It is rebuilt
#                     on the next launch.
#     --desktop       macOS: also remove CLIO Desktop.app from ~/Applications and
#                     /Applications plus its caches and logs (with --purge, also its
#                     Application Support folder). Quit the app first.
#
#   Environment overrides (must match the install):
#     CLIO_PREFIX   install root      (default: $HOME/.local/share/clio)
#     CLIO_PORT     server port       (default: gact.port in config.yaml, else 17800)
#     CLIO_BIN_DIR  launcher location (default: $HOME/.local/bin)
#     CLIO_DESKTOP_APP  the one Desktop app bundle --desktop removes
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
if [[ "$(uname -s)" = Darwin* ]]; then
  CLIO_CACHE="$HOME/Library/Caches/clio-agent"
else
  xdg_cache="${XDG_CACHE_HOME:-}"
  case "$xdg_cache" in /*) ;; *) xdg_cache="$HOME/.cache" ;; esac
  CLIO_CACHE="$xdg_cache/clio-agent"
fi
AGENT_PYTHON="$CLIO_PREFIX/clio-agent/.venv/bin/python"
if [[ -x "$AGENT_PYTHON" ]]; then
  # Resolve before deleting the installed interpreter. Older releases retain
  # the legacy paths above if they do not expose the namespace helpers.
  resolved_state="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_state_dir; print(user_state_dir())' 2>/dev/null)" && CLIO_STATE="$resolved_state"
  resolved_config="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_config_dir; print(user_config_dir())' 2>/dev/null)" && CLIO_CONFIG="$resolved_config"
  resolved_cache="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_cache_dir; print(user_cache_dir())' 2>/dev/null)" && CLIO_CACHE="$resolved_cache"
  # The server listens on gact.port from CLIO's config.yaml when one is set.
  resolved_port="$("$AGENT_PYTHON" -m clio_agent.server_port 2>/dev/null)" && [[ -n "$resolved_port" ]] && CLIO_PORT="$resolved_port"
fi
HOST_ID="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo localhost)"
PIDFILE="$CLIO_STATE/clio-server.$HOST_ID.pid"
for candidate in "$CLIO_STATE/clio-server.pid" "$CLIO_PREFIX/clio-server.$HOST_ID.pid" "$CLIO_PREFIX/clio-server.pid"; do
  [[ -f "$PIDFILE" ]] || PIDFILE="$candidate"
done

LAUNCHER="$CLIO_BIN_DIR/clio"

ASSUME_YES=0
PURGE=0
PURGE_CACHES=0
DESKTOP=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y)       ASSUME_YES=1 ;;
    --purge)        PURGE=1 ;;
    --purge-caches) PURGE_CACHES=1 ;;
    --desktop)      DESKTOP=1 ;;
    *) echo "uninstall: unknown flag '$arg' (want --yes, --purge, --purge-caches, --desktop)" >&2; exit 2 ;;
  esac
done

if [[ "$PURGE" -eq 1 ]]; then
  case "$CLIO_CONFIG" in
    /*/clio-agent|/*/clio-agent/config) ;;
    *) echo "Refusing to purge a custom config root: $CLIO_CONFIG. Remove its contents explicitly after review." >&2; exit 2 ;;
  esac
fi
if [[ "$PURGE_CACHES" -eq 1 ]]; then
  case "$CLIO_CACHE" in
    /*/clio-agent|/*/clio-agent/cache) ;;
    *) echo "Refusing to purge a custom cache root: $CLIO_CACHE. Remove its contents explicitly after review." >&2; exit 2 ;;
  esac
fi

# CLIO Desktop (macOS): the app bundle and the folders macOS keeps under its
# bundle identifier. Application Support holds its settings and logs, so it goes
# only with --purge.
DESKTOP_ID="ai.iowarp.clio.desktop"
DESKTOP_APPS=()
DESKTOP_DIRS=()
if [[ "$DESKTOP" -eq 1 ]]; then
  if [[ "$(uname -s)" != Darwin* ]]; then
    echo "uninstall: --desktop is for macOS; on Linux remove the clio-desktop package or AppImage" >&2
    exit 2
  fi
  if [[ -n "${CLIO_DESKTOP_APP:-}" ]]; then
    DESKTOP_APPS=("$CLIO_DESKTOP_APP")
  else
    DESKTOP_APPS=("$HOME/Applications/CLIO Desktop.app" "/Applications/CLIO Desktop.app")
  fi
  for app in "${DESKTOP_APPS[@]}"; do
    if [[ -d "$app" ]] && pgrep -f "$app/Contents/MacOS/" >/dev/null 2>&1; then
      echo "CLIO Desktop is running from $app. Quit it (Cmd-Q), then run the uninstaller again." >&2
      exit 1
    fi
  done
  DESKTOP_DIRS=(
    "$HOME/Library/Caches/$DESKTOP_ID"
    "$HOME/Library/Logs/$DESKTOP_ID"
    "$HOME/Library/WebKit/$DESKTOP_ID"
  )
  [[ "$PURGE" -eq 1 ]] && DESKTOP_DIRS+=("$HOME/Library/Application Support/$DESKTOP_ID")
fi

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
if [[ "$PURGE_CACHES" -eq 1 ]]; then
  echo "  clio caches:     $CLIO_CACHE  (--purge-caches)"
else
  echo "  clio caches:     $CLIO_CACHE  (KEPT — pass --purge-caches to remove)"
fi
for app in ${DESKTOP_APPS[@]+"${DESKTOP_APPS[@]}"}; do
  [[ -d "$app" ]] && echo "  desktop app:     $app"
done
for dir in ${DESKTOP_DIRS[@]+"${DESKTOP_DIRS[@]}"}; do
  [[ -d "$dir" ]] && echo "  desktop data:    $dir"
done
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
  rm -f -- "$CLIO_PREFIX/gact" "$CLIO_PREFIX/uninstall.sh" "$CLIO_PREFIX/libexec/clio"
  rmdir -- "$CLIO_PREFIX/libexec" "$CLIO_PREFIX/clio-agent" "$CLIO_PREFIX" 2>/dev/null || true
  [[ ! -d "$CLIO_PREFIX" ]] || warn "Retained user data and unknown files in $CLIO_PREFIX."
fi
if [[ "$PURGE" -eq 1 ]]; then
  [[ -d "$CLIO_CONFIG" ]] && { say "Removing $CLIO_CONFIG"; rm -rf "$CLIO_CONFIG"; }
  # gact is a separate product; its configuration is retained.
fi
if [[ "$PURGE_CACHES" -eq 1 && -d "$CLIO_CACHE" ]]; then
  say "Removing $CLIO_CACHE"
  rm -rf -- "$CLIO_CACHE"
fi
for app in ${DESKTOP_APPS[@]+"${DESKTOP_APPS[@]}"}; do
  if [[ -d "$app" ]]; then
    say "Removing $app"
    rm -rf -- "$app" || warn "Could not remove $app; drag it to the Trash from Finder."
  fi
done
for dir in ${DESKTOP_DIRS[@]+"${DESKTOP_DIRS[@]}"}; do
  if [[ -d "$dir" ]]; then
    say "Removing $dir"
    rm -rf -- "$dir"
  fi
done

say "CLIO uninstalled."
if [[ "$PURGE" -ne 1 && -d "$CLIO_CONFIG" ]]; then
  echo "  Agent config kept ($CLIO_CONFIG) — re-run with --purge to remove"
fi
