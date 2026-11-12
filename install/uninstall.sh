#!/usr/bin/env bash
# CLIO uninstaller (Linux / macOS).
#
# Undoes install.sh: stops the server, removes the launcher, the tools the installer
# added (the clio-kit tool, and uv and uvx when it installed them), and
# removes known application payloads. Pass --purge to also remove the
# resolved Agent config directory. Data, state, and other products are retained.
#
#   Flags:
#     --yes           skip the confirmation prompt (non-interactive)
#     --purge         also remove Agent configuration (including stored credentials)
#                     and clio-core's unedited ~/.clio seed and CLIO perf stats
#     --purge-caches  also remove the Agent cache root (document runtime, LibreOffice,
#                     tool caches; macOS: ~/Library/Caches/clio-agent). It is rebuilt
#                     on the next launch.
#     --purge-data    also remove the Agent data and state roots: conversations,
#                     workspaces, memory, the clio-core store, service environments
#                     and a source install's clone. This cannot be undone.
#     --desktop       macOS: also remove CLIO Desktop.app from ~/Applications and
#                     /Applications plus its caches and logs (with --purge or
#                     --purge-data, also its Application Support folder). Quit the app
#                     first.
#
#   Remove everything:  --purge --purge-caches --purge-data (and --desktop on macOS)
#
#   Environment overrides (must match the install):
#     CLIO_PREFIX   install root      (default: <agent data>/app, as install.sh; macOS:
#                   ~/Library/Application Support/clio-agent/data/app)
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
if [[ "$(uname -s)" = Darwin* ]]; then
  default_config="$HOME/Library/Application Support/clio-agent/config"
  CLIO_CACHE="$HOME/Library/Caches/clio-agent"
  # Host-wide Core supervision records, shared by the CLI and Desktop.
  CLIO_HOST_STATE="$HOME/Library/Application Support/clio-agent/state"
else
  xdg_config="${XDG_CONFIG_HOME:-}"
  case "$xdg_config" in /*) ;; *) xdg_config="$HOME/.config" ;; esac
  default_config="$xdg_config/clio-agent"
  xdg_state="${XDG_STATE_HOME:-}"
  case "$xdg_state" in /*) ;; *) xdg_state="$HOME/.local/state" ;; esac
  CLIO_HOST_STATE="$xdg_state/clio-agent"
  xdg_cache="${XDG_CACHE_HOME:-}"
  case "$xdg_cache" in /*) ;; *) xdg_cache="$HOME/.cache" ;; esac
  CLIO_CACHE="$xdg_cache/clio-agent"
fi
CLIO_CONFIG="${CLIO_AGENT_CONFIG_DIR:-${CLIO_AGENT_HOME:+$CLIO_AGENT_HOME/config}}"
CLIO_CONFIG="${CLIO_CONFIG:-$default_config}"
CLIO_HOST_STATE="${CLIO_AGENT_STATE_DIR:-$CLIO_HOST_STATE}"
AGENT_PYTHON="$CLIO_PREFIX/clio-agent/.venv/bin/python"
if [[ -x "$AGENT_PYTHON" ]]; then
  # Resolve before deleting the installed interpreter. Older releases retain
  # the legacy paths above if they do not expose the namespace helpers.
  resolved_state="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_state_dir; print(user_state_dir())' 2>/dev/null)" && CLIO_STATE="$resolved_state"
  resolved_config="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_config_dir; print(user_config_dir())' 2>/dev/null)" && CLIO_CONFIG="$resolved_config"
  resolved_cache="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_cache_dir; print(user_cache_dir())' 2>/dev/null)" && CLIO_CACHE="$resolved_cache"
  resolved_host_state="$("$AGENT_PYTHON" -c 'from clio_agent.paths import host_state_dir; print(host_state_dir())' 2>/dev/null)" && CLIO_HOST_STATE="$resolved_host_state"
  resolved_data="$("$AGENT_PYTHON" -c 'from clio_agent.paths import user_data_dir; print(user_data_dir())' 2>/dev/null)" && agent_data="$resolved_data"
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
PURGE_DATA=0
DESKTOP=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y)       ASSUME_YES=1 ;;
    --purge)        PURGE=1 ;;
    --purge-caches) PURGE_CACHES=1 ;;
    --purge-data)   PURGE_DATA=1 ;;
    --desktop)      DESKTOP=1 ;;
    *) echo "uninstall: unknown flag '$arg' (want --yes, --purge, --purge-caches, --purge-data, --desktop)" >&2; exit 2 ;;
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
# Without an installed Agent to ask, CLIO_STATE is still the legacy prefix; the
# state root is then the platform's (the host-wide one, by default the same folder).
data_state="$CLIO_STATE"
[[ "$data_state" != "$CLIO_PREFIX" ]] || data_state="$CLIO_HOST_STATE"
PURGE_DATA_DIRS=()
if [[ "$PURGE_DATA" -eq 1 ]]; then
  for dir in "$agent_data" "$data_state"; do
    case "$dir" in
      /*/clio-agent|/*/clio-agent/data|/*/clio-agent/state) ;;
      *) echo "Refusing to purge a custom data or state root: $dir. Remove it explicitly after review." >&2; exit 2 ;;
    esac
    PURGE_DATA_DIRS+=("$dir")
  done
fi

# CLIO Desktop (macOS): the app bundle and the folders macOS keeps under its
# bundle identifier. Application Support holds its settings and logs, so it goes
# only with --purge.
DESKTOP_ID="ai.iowarp.clio.desktop"
desktop_root=""
DESKTOP_APPS=()
DESKTOP_DIRS=()
if [[ "$DESKTOP" -eq 1 && "$(uname -s)" != Darwin* ]]; then
  # Linux: the package manager removes the app (it needs root); this removes what the app
  # kept in this account, which no package manager touches: ~2.6 GB after first use.
  # The same mapping as macOS: logs and WebKit caches always, CLIO's cache with
  # --purge-caches, the whole folder (settings, sessions, store) with --purge or
  # --purge-data.
  for proc in /proc/[0-9]*; do
    if [[ "$(readlink "$proc/exe" 2>/dev/null)" == /usr/bin/clio-desktop ]]; then
      echo "CLIO Desktop is running. Quit it, then run the uninstaller again." >&2
      exit 1
    fi
  done
  desktop_root="${XDG_DATA_HOME:-$HOME/.local/share}/$DESKTOP_ID"
  if [[ "$PURGE" -eq 1 || "$PURGE_DATA" -eq 1 ]]; then
    DESKTOP_DIRS=("$desktop_root")
  else
    DESKTOP_DIRS=("$desktop_root/logs" "$desktop_root/WebKitCache" "$desktop_root/CacheStorage")
    [[ "$PURGE_CACHES" -eq 0 ]] || DESKTOP_DIRS+=("$desktop_root/clio-agent/cache" "$desktop_root/workspace/.tmp")
  fi
elif [[ "$DESKTOP" -eq 1 ]]; then
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
  if [[ "$PURGE" -eq 1 || "$PURGE_DATA" -eq 1 ]]; then
    DESKTOP_DIRS+=("$HOME/Library/Application Support/$DESKTOP_ID")
  fi
fi

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RESET='\033[0m'
say()  { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!!${RESET} %s\n" "$*" >&2; }

echo ""
echo "CLIO uninstall — the following will be removed:"
echo "  application payloads in: $CLIO_PREFIX (unknown files retained)"
echo "  launcher:        $LAUNCHER"
if [[ -f "$CLIO_PREFIX/installed-tools" ]]; then
  echo "  installer tools: $(awk '{printf "%s%s", (NR > 1 ? ", " : ""), $2}' "$CLIO_PREFIX/installed-tools")"
elif [[ -d "$CLIO_PREFIX/clio-agent" ]]; then
  echo "  installer tools: clio-kit"
fi
if [[ "$PURGE" -eq 1 ]]; then
  echo "  clio config:     $CLIO_CONFIG  (--purge)"
else
  echo "  clio config:     $CLIO_CONFIG  (KEPT — pass --purge to remove)"
fi
if [[ "$PURGE" -eq 1 && -d "$CLIO_HOST_STATE/core-hosts" ]]; then
  echo "  core records:    $CLIO_HOST_STATE/core-hosts  (--purge)"
fi
if [[ "$PURGE_CACHES" -eq 1 ]]; then
  echo "  clio caches:     $CLIO_CACHE  (--purge-caches)"
else
  echo "  clio caches:     $CLIO_CACHE  (KEPT — pass --purge-caches to remove)"
fi
if [[ "$PURGE_DATA" -eq 1 ]]; then
  for dir in "${PURGE_DATA_DIRS[@]}"; do
    echo "  clio data:       $dir  (--purge-data: conversations, workspaces, memory)"
  done
else
  echo "  clio data:       $agent_data  (KEPT — pass --purge-data to remove)"
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
# A pid counts as this installation's only if its command line, executable or
# working directory is under the install prefix. The pidfile can name a recycled
# pid, and the port can be held by any program, so neither is trusted alone.
prefix_real="$(cd -P -- "$CLIO_PREFIX" 2>/dev/null && pwd || true)"
under_prefix() {
  local path="$1" root
  for root in "$CLIO_PREFIX" "$prefix_real"; do
    [[ -n "$root" ]] || continue
    case "$path" in "$root"/*) return 0 ;; esac
  done
  return 1
}
process_paths() {
  local pid="$1"
  if [[ -d "/proc/$pid" ]]; then
    readlink "/proc/$pid/exe" 2>/dev/null || true
    readlink "/proc/$pid/cwd" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    # The first txt entry is the executable.
    lsof -a -p "$pid" -d txt -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1 || true
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true
  fi
}
install_process() {
  local pid="$1" args root path
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  for root in "$CLIO_PREFIX" "$prefix_real"; do
    [[ -n "$root" ]] || continue
    case "$args" in *"$root"/*) return 0 ;; esac
  done
  while IFS= read -r path; do
    [[ -n "$path" ]] && under_prefix "$path" && return 0
  done < <(process_paths "$pid")
  return 1
}
describe_process() {
  ps -p "$1" -o args= 2>/dev/null | cut -c1-160 || true
}

server_pid=""
if [[ -f "$PIDFILE" ]]; then
  p="$(awk 'NR == 1 { print $1 }' "$PIDFILE" 2>/dev/null || true)"
  if install_process "$p"; then
    server_pid="$p"
  elif [[ "$p" =~ ^[0-9]+$ ]] && kill -0 "$p" 2>/dev/null; then
    warn "The server pidfile names pid $p ($(describe_process "$p")), which is not part of this installation; leaving it running"
  fi
fi
if [[ -z "$server_pid" ]] && command -v lsof >/dev/null 2>&1; then
  while read -r p; do
    [[ "$p" =~ ^[0-9]+$ ]] || continue
    if install_process "$p"; then
      server_pid="$p"
      break
    fi
    warn "Port $CLIO_PORT is held by pid $p ($(describe_process "$p")), which is not part of this installation; leaving it running"
  done < <(lsof -ti "tcp:$CLIO_PORT" -sTCP:LISTEN 2>/dev/null || true)
fi
STOPPED_PIDS=()
if [[ -n "$server_pid" ]]; then
  say "Stopping CLIO server (pid $server_pid)"
  STOPPED_PIDS+=("$server_pid")
  kill "$server_pid" 2>/dev/null || true
  sleep 1
  kill -0 "$server_pid" 2>/dev/null && kill -9 "$server_pid" 2>/dev/null || true
else
  say "No running CLIO server found"
fi

# Sweep leftover server processes started from this prefix. Match both the
# current entrypoint (clio-agent serve) and the retired pre-upgrade one
# (clio-agent-gact) so an old server left running is still cleaned up.
leftover_pattern="$CLIO_PREFIX/clio-agent/.venv/bin/clio-agent(-gact)?"
if command -v pgrep >/dev/null 2>&1; then
  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] && STOPPED_PIDS+=("$pid")
  done < <(pgrep -f "$leftover_pattern" 2>/dev/null || true)
fi
if command -v pkill >/dev/null 2>&1; then
  pkill -f "$leftover_pattern" 2>/dev/null || true
fi

# ---- clio-core's files in ~/.clio ------------------------------------
# Importing iowarp_core seeds ~/.clio/clio.yaml, and older CLIO runtimes kept
# clio-core's bdev perf stats in ~/.clio/bdev_perf. --purge removes the seed only
# while it is byte-identical to a bundled default (an edited one is someone's
# config), and only the perf files named after CLIO's own data paths. Find the
# defaults now, before the payloads holding them are removed.
CORE_HOME="$HOME/.clio"
CORE_SEEDS=()
# A plain install from a release's prebuilt agent keeps its Python under .venv/python.
core_seed_dirs=("$CLIO_PREFIX/clio-agent/.venv/lib" "$CLIO_PREFIX/clio-agent/.venv/python/lib")
for app in ${DESKTOP_APPS[@]+"${DESKTOP_APPS[@]}"}; do
  core_seed_dirs+=("$app/Contents/Resources/gact-runtime/python/lib")
done
# Linux CLIO Desktop's runtime, while its package is still installed.
[[ -z "$desktop_root" ]] || core_seed_dirs+=("/usr/lib/CLIO Desktop/gact-runtime/python/lib")
for dir in "${core_seed_dirs[@]}"; do
  for seed in "$dir"/python3*/site-packages/iowarp_core/data/clio_default.yaml; do
    [[ -f "$seed" ]] && CORE_SEEDS+=("$seed")
  done
done
# clio-core names a perf file after its bdev path with "/" and " " as "_".
core_perf_name() { local name="${1//\//_}"; printf "%s_" "${name// /_}"; }
core_perf_prefixes=(
  "$(core_perf_name "$agent_data")"
  "$(core_perf_name "$HOME/Library/Application Support/$DESKTOP_ID")"
)

# Byte-identical files. cmp is diffutils, which minimal Fedora, Rocky and Arch lack; there
# the unedited seed read as edited and stayed. sha256sum is coreutils (macOS has cmp).
same_file() {
  if command -v cmp >/dev/null 2>&1; then cmp -s "$1" "$2"
  else [[ "$(sha256sum < "$1")" == "$(sha256sum < "$2")" ]]; fi
}
purge_core_seed() {
  [[ -d "$CORE_HOME" ]] || return 0
  local seed perf prefix
  if [[ -f "$CORE_HOME/clio.yaml" ]]; then
    for seed in ${CORE_SEEDS[@]+"${CORE_SEEDS[@]}"}; do
      if same_file "$seed" "$CORE_HOME/clio.yaml"; then
        say "Removing clio-core default config $CORE_HOME/clio.yaml"
        rm -f -- "$CORE_HOME/clio.yaml"
        break
      fi
    done
  fi
  for perf in "$CORE_HOME"/bdev_perf/*.perf; do
    [[ -f "$perf" ]] || continue
    for prefix in "${core_perf_prefixes[@]}"; do
      [[ "$(basename "$perf")" == "$prefix"* ]] && { rm -f -- "$perf"; break; }
    done
  done
  rmdir -- "$CORE_HOME/bdev_perf" "$CORE_HOME" 2>/dev/null || true
  [[ ! -d "$CORE_HOME" ]] || warn "Retained other clio-core files in $CORE_HOME."
}
# The host-wide Core supervision records (lock, log, client registry), shared by
# the CLI and Desktop. Kept while a clio-core daemon still runs.
# Print the pid of a live clio-core daemon recorded under the host state, if any.
core_daemon_pid() {
  local pidfile pid
  for pidfile in "$CLIO_HOST_STATE"/core-hosts/*/clio-runtime.pid; do
    [[ -f "$pidfile" ]] || continue
    # The record is "<pid> <start time>"; older ones hold the pid alone.
    pid="$(awk 'NR == 1 { print $1 }' "$pidfile" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  done
  return 1
}
purge_host_state() {
  local core="$CLIO_HOST_STATE/core-hosts"
  [[ -d "$core" ]] || return 0
  local pid
  if pid="$(core_daemon_pid)"; then
    warn "The clio-core daemon (pid $pid) is running; kept $core."
    return 0
  fi
  say "Removing $core"
  rm -rf -- "$core"
  rmdir -- "$CLIO_HOST_STATE" 2>/dev/null || true
  # macOS keeps state inside the clio-agent folder; drop it once empty.
  local parent
  parent="$(dirname "$CLIO_HOST_STATE")"
  if [[ "$(basename "$parent")" == clio-agent ]]; then rmdir -- "$parent" 2>/dev/null || true; fi
}
# --purge-data: the data and state roots, never while clio-core still has its store open.
purge_data() {
  local pid dir parent
  if pid="$(core_daemon_pid)"; then
    warn "The clio-core daemon (pid $pid) is running; kept ${PURGE_DATA_DIRS[*]}."
    return 0
  fi
  for dir in "${PURGE_DATA_DIRS[@]}"; do
    [[ -d "$dir" ]] || continue
    say "Removing $dir"
    rm -rf -- "$dir"
    parent="$(dirname "$dir")"
    if [[ "$(basename "$parent")" == clio-agent ]]; then rmdir -- "$parent" 2>/dev/null || true; fi
  done
}

# clio-core's file-backed shared memory lives in /tmp/clio_<user> (macOS: regular
# files, up to ~1.5 GB per daemon; Linux: memfd symlinks). A process that is killed
# before it unmaps leaves its files behind, and nothing cleans them once CLIO is
# gone. Remove only what belongs to this install: the per-process files of the
# processes stopped above, and the port-keyed segments of the daemon this install
# recorded, once that daemon is dead. Any other runtime's files are kept.
remove_core_memory_files() {
  local user dir pid file name port record kb count=0 total_kb=0 waited=0 alive
  local daemons=() owned=() dirs=()
  user="${USER:-$(id -un 2>/dev/null || true)}"
  [[ -n "$user" ]] || return 0
  dirs=("/tmp/clio_$user")
  if [[ -n "${TMPDIR:-}" && "${TMPDIR%/}/clio_$user" != "/tmp/clio_$user" ]]; then
    dirs+=("${TMPDIR%/}/clio_$user")
  fi
  # The leftovers got SIGTERM; give them a moment to exit before their files go.
  while ((waited < 20)); do
    alive=0
    for pid in ${STOPPED_PIDS[@]+"${STOPPED_PIDS[@]}"}; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    ((alive)) || break
    sleep 0.1
    waited=$((waited + 1))
  done
  for pid in ${STOPPED_PIDS[@]+"${STOPPED_PIDS[@]}"}; do
    kill -0 "$pid" 2>/dev/null || owned+=("$pid")
  done
  for record in "$CLIO_HOST_STATE"/core-hosts/*/clio-runtime.pid; do
    [[ -f "$record" ]] || continue
    pid="$(awk 'NR == 1 { print $1 }' "$record" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] && ! kill -0 "$pid" 2>/dev/null && daemons+=("$pid")
  done
  for dir in "${dirs[@]}"; do
    [[ -d "$dir" ]] || continue
    local files=()
    for pid in ${daemons[@]+"${daemons[@]}"}; do
      for record in "$dir/chi_runtime_pid_${user}_"*; do
        [[ -f "$record" && "$(cat "$record" 2>/dev/null || true)" == "$pid" ]] || continue
        port="${record##*_}"
        [[ "$port" =~ ^[0-9]+$ ]] || continue
        files+=("$record" "$dir/clio_$port.ipc")
        for name in main metadata queue; do files+=("$dir/chi_${name}_segment_${user}_$port"); done
        owned+=("$pid")
      done
    done
    for pid in ${owned[@]+"${owned[@]}"}; do
      files+=("$dir/clio-$pid-shm-"* "$dir/clio_${pid}_"* "$dir/clio_rambdev_${pid}_"*)
    done
    for file in ${files[@]+"${files[@]}"}; do
      [[ -e "$file" || -L "$file" ]] || continue
      kb="$(du -k "$file" 2>/dev/null | cut -f1)"
      rm -f -- "$file" && count=$((count + 1)) && total_kb=$((total_kb + ${kb:-0}))
    done
    rmdir -- "$dir" 2>/dev/null || true
  done
  ((count == 0)) || say "Removed $count clio-core memory files ($((total_kb / 1024)) MB) from ${dirs[*]}"
}
remove_core_memory_files

# Compare against the seeds before the payloads holding them go.
if [[ "$PURGE" -eq 1 ]]; then purge_core_seed; fi

# ---- tools the installer added (install.sh's installed-tools) ---------
# clio-kit first, with the uv the installer used, then uv and uvx if it installed them.
# Before 0.9.6 nothing was recorded; clio-kit was always installed, so it goes too.
INSTALLED_TOOLS="$CLIO_PREFIX/installed-tools"
# uv as install.sh installed it leaves more than its two binaries: its cache (~500 MB in
# ~/.cache/uv) and its installer's receipt go with it. Its managed Pythons and tool folder
# stay: a person's own environments made with this uv run on those Pythons, and removing
# them would break those environments. Where they are is said, to remove by hand.
remove_uv_data() {  # UV
  local uv="$1" cache python config="${XDG_CONFIG_HOME:-$HOME/.config}/uv"
  # Every folder is asked for first: any uv command recreates its cache's skeleton.
  cache="$("$uv" cache dir 2>/dev/null || true)"
  python="$("$uv" python dir 2>/dev/null || true)"
  if [[ "$cache" == /*/uv && -d "$cache" ]]; then
    say "Removing uv's cache $cache"
    rm -rf -- "$cache"
  fi
  rm -f -- "$config/uv-receipt.json"
  rmdir -- "$config" 2>/dev/null || true
  if [[ "$python" == /*/uv/* && -d "$python" ]]; then
    say "Kept uv's Pythons in $python (environments you made with uv may use them); remove that folder if nothing does."
  fi
}
remove_installed_tools() {
  local uv_bin="" kind value tools=() files=()
  if [[ -f "$INSTALLED_TOOLS" ]]; then
    while read -r kind value; do
      case "$kind" in
        uv-tool) [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] && tools+=("$value") ;;
        file) [[ "$value" == /* && "$(basename "$value")" =~ ^uvx?$ ]] && files+=("$value") ;;
      esac
    done < "$INSTALLED_TOOLS"
  elif [[ -d "$CLIO_PREFIX/clio-agent" ]]; then
    tools=(clio-kit)
  fi
  for value in ${files[@]+"${files[@]}"}; do
    [[ "$(basename "$value")" == uv && -x "$value" ]] && uv_bin="$value"
  done
  [[ -n "$uv_bin" ]] || uv_bin="$(command -v uv 2>/dev/null || true)"
  [[ -n "$uv_bin" ]] || [[ ! -x "$CLIO_PREFIX/clio-agent/.venv/bin/uv" ]] || uv_bin="$CLIO_PREFIX/clio-agent/.venv/bin/uv"
  for value in ${tools[@]+"${tools[@]}"}; do
    [[ -n "$uv_bin" ]] && "$uv_bin" tool list 2>/dev/null | grep -q "^$value " || continue
    say "Removing the $value tool"
    "$uv_bin" tool uninstall "$value" >/dev/null 2>&1 \
      || warn "Could not remove the $value tool; run 'uv tool uninstall $value'."
  done
  for value in ${files[@]+"${files[@]}"}; do
    [[ "$value" == "$uv_bin" && -x "$value" ]] && remove_uv_data "$value"
  done
  for value in ${files[@]+"${files[@]}"}; do
    if [[ -f "$value" ]]; then
      say "Removing $value"
      rm -f -- "$value"
    fi
  done
  rm -f -- "$INSTALLED_TOOLS"
}
remove_installed_tools

# codex keeps launcher links (apply_patch, codex-linux-sandbox) to the codex it runs in
# ~/.codex/tmp/arg0-linux/codex-arg0*. Those leading into CLIO's own payload lead nowhere
# once it is removed; codex folders holding anything else are the person's own and stay.
remove_codex_links() {  # ROOT...
  local codex="${CODEX_HOME:-$HOME/.codex}" dir link root ours
  for dir in "$codex"/tmp/arg0-linux/codex-arg0*; do
    [[ -d "$dir" ]] || continue
    ours=0
    for link in "$dir"/*; do
      [[ -L "$link" ]] || continue
      for root in "$@"; do [[ "$(readlink "$link")" == "$root"/* ]] && ours=1; done
    done
    [[ "$ours" -eq 1 ]] && rm -rf -- "$dir"
  done
  rmdir -- "$codex/tmp/arg0-linux" "$codex/tmp" "$codex" 2>/dev/null || true
}
codex_roots=("$CLIO_PREFIX")
[[ -z "$desktop_root" ]] || codex_roots+=("/usr/lib/CLIO Desktop")
remove_codex_links "${codex_roots[@]}"

# ---- remove files ----------------------------------------------------
if [[ -e "$LAUNCHER" ]]; then
  say "Removing $LAUNCHER"
  rm -f "$LAUNCHER"
fi
if [[ -d "$CLIO_PREFIX" ]]; then
  say "Removing installed application payloads in $CLIO_PREFIX"
  # uv-tools: a scoped installation's own clio-kit (install.sh).
  rm -rf -- "$CLIO_PREFIX/clio-agent/.venv" "$CLIO_PREFIX/clio-agent/web" "$CLIO_PREFIX/uv-tools"
  rm -f -- "$CLIO_PREFIX/gact" "$CLIO_PREFIX/uninstall.sh" "$CLIO_PREFIX/libexec/clio"
  rmdir -- "$CLIO_PREFIX/libexec" "$CLIO_PREFIX/clio-agent" "$CLIO_PREFIX" 2>/dev/null || true
  if [[ -d "$CLIO_PREFIX" && ! ( "$PURGE_DATA" -eq 1 && "$CLIO_PREFIX" == "$agent_data"/* ) ]]; then
    warn "Retained user data and unknown files in $CLIO_PREFIX."
  fi
fi
if [[ "$PURGE" -eq 1 ]]; then
  [[ -d "$CLIO_CONFIG" ]] && { say "Removing $CLIO_CONFIG"; rm -rf "$CLIO_CONFIG"; }
  # CLIO's sandbox profile layers in codex's home (runtime/sandbox_codex.py), named for CLIO.
  for layer in "${CODEX_HOME:-$HOME/.codex}"/clio-sb-*.config.toml; do
    [[ -f "$layer" ]] && { say "Removing $layer"; rm -f -- "$layer"; }
  done
  rmdir -- "${CODEX_HOME:-$HOME/.codex}" 2>/dev/null || true
  # gact is a separate product; its configuration is retained.
  purge_host_state
fi
if [[ "$PURGE_DATA" -eq 1 ]]; then purge_data; fi
# LibreOffice's fontconfig cache from before it moved inside CLIO's cache folder.
purge_cache_extras() {
  local legacy
  legacy="$(dirname "$CLIO_CACHE")/clio-office-fontconfig"
  if [[ -d "$legacy" ]]; then say "Removing $legacy"; rm -rf -- "$legacy"; fi
}
if [[ "$PURGE_CACHES" -eq 1 ]]; then
  if [[ -d "$CLIO_CACHE" ]]; then
    say "Removing $CLIO_CACHE"
    rm -rf -- "$CLIO_CACHE"
  fi
  purge_cache_extras
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

if [[ -n "$desktop_root" ]] && command -v clio-desktop >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then remove="sudo apt-get remove clio-desktop"
  elif command -v dnf >/dev/null 2>&1; then remove="sudo dnf remove clio-desktop"
  elif command -v zypper >/dev/null 2>&1; then remove="sudo zypper remove clio-desktop"
  elif command -v pacman >/dev/null 2>&1; then remove="sudo pacman -R clio-desktop"
  else remove="your package manager"; fi
  say "CLIO Desktop's files in this account are removed; remove the app itself with: $remove"
fi
say "CLIO uninstalled."
if [[ "$PURGE" -ne 1 && -d "$CLIO_CONFIG" ]]; then
  echo "  Agent config kept ($CLIO_CONFIG) — re-run with --purge to remove"
fi
