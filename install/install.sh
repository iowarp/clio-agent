#!/usr/bin/env bash
# CLIO installer (Linux / macOS).
#
#   install.sh                 the latest stable release
#   install.sh --dev           the latest release overall, pre-releases included
#   install.sh --source        source at `main`, with what main pins (submodules, uv.lock)
#   install.sh --dev --source  source at `develop`
#   install.sh --desktop       the desktop app, bundled with its agent (Apple Silicon macOS, Linux);
#                              --dev and --version pick the release as above
#
# --source run from inside a clio-agent checkout (`./install/install.sh`) installs that
# checkout in place, on whatever branch it is; streamed (`curl ... | bash`) it clones.
# A release is installed from PyPI, constrained by that release tag's own uv.lock, on
# the Python that release declares (.python-version; 3.13 before the file existed).
#
# Pins for deployments and CI: --version <pypi version> (release) and --ref <git ref>
# (source). Older environment spellings still work: CLIO_VERSION=<v> is --version,
# CLIO_REF=<ref> is --source --ref <ref>.
#
# Honours environment overrides:
#   CLIO_PREFIX        install root         (default: <agent data>/app)
#   CLIO_BIN_DIR       launcher location    (default: $HOME/.local/bin)
#   GACT_VERSION       legacy override for TUI release tag (default: match CLIO)
#   CLIO_INSTALLER_REF pin launcher scripts (default: the installed release's tag)
#   GACT_REF           gact-tui branch for the TUI (source mode only)
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: released 2.11.1)
#   CLIO_GIT_PROTOCOL  https | ssh          (default: https; source mode only)
#
# NOTE: invoke via `bash` (not `sh`). On Debian/Ubuntu `sh` is dash,
# which doesn't support `set -o pipefail`. The one-liner in the README
# pipes to `bash` for this reason.

# Refuse to run under dash / POSIX-sh — we use `set -o pipefail` and a
# couple of bash-only constructs below. Piping into `sh` on Debian/
# Ubuntu (where /bin/sh is dash) trips this. Re-exec via curl isn't
# possible here because the script is being streamed from stdin, so
# just bail with the right next-step.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "clio installer requires bash, not sh/dash." >&2
  echo "Re-run with bash, e.g.:" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash" >&2
  exit 1
fi
set -euo pipefail

usage() { sed -n '2,20p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; }

OPT_SOURCE=0
OPT_DEV=0
OPT_VERSION=""
OPT_REF=""
OPT_DESKTOP=0
OPT_DOWNLOAD_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --source|-Source) OPT_SOURCE=1 ;;
    --dev|-Dev) OPT_DEV=1 ;;
    --version|-Version) OPT_VERSION="${2:?--version needs a value}"; shift ;;
    --version=*) OPT_VERSION="${1#*=}" ;;
    --ref|-Ref) OPT_REF="${2:?--ref needs a value}"; shift ;;
    --ref=*) OPT_REF="${1#*=}" ;;
    --desktop|-Desktop) OPT_DESKTOP=1 ;;
    --download-only|-DownloadOnly) OPT_DOWNLOAD_ONLY="${2:?--download-only needs a directory}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "clio installer: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; RESET='\033[0m'
say()  { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!! ${RESET} %s\n" "$*" >&2; }
die()  { printf "${RED}xx ${RESET} %s\n" "$*" >&2; exit 1; }
# die_with CODE MESSAGE: die, keeping the failed command's exit status.
die_with() { local code="$1"; shift; printf "${RED}xx ${RESET} %s\n" "$*" >&2; exit "$code"; }

have() { command -v "$1" >/dev/null 2>&1; }

# PyPI package spelling and GitHub tag spelling differ for beta releases.
release_tag() {
  local version="${1#v}"
  if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)b([0-9]+)\.post([0-9]+)$ ]]; then
    printf 'v%s-beta.%s.%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
  elif [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?)b([0-9]+)$ ]]; then
    printf 'v%s-beta.%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}"
  elif [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?(-beta\.[0-9]+(\.[0-9]+)?|(a|rc)[0-9]+)?$ ]]; then
    # Only betas have their own tag spelling; alphas and release candidates
    # are tagged as their package version (0.10.0a1 is v0.10.0a1).
    printf 'v%s' "$version"
  else
    die "no GitHub release tag for package version: $version"
  fi
}

# ---------- desktop app (--desktop) -----------------------------------
# The released desktop installer carries its own agent, so --desktop needs no Python,
# uv or git: it picks the release, downloads the bundled installer for this OS and
# CPU, verifies it against the release's checksum manifest, and installs it.
# --download-only DIR stops after the verified download.
REPO_URL=https://github.com/iowarp/clio-agent

desktop_tag() {
  if [ -n "${OPT_VERSION:-${CLIO_VERSION:-}}" ]; then
    release_tag "${OPT_VERSION:-$CLIO_VERSION}"
  elif [ "$OPT_DEV" = 1 ]; then
    # The newest release, pre-releases included, never a draft (an authenticated
    # listing includes drafts; tag_name precedes draft in each release object).
    curl --proto '=https' --tlsv1.2 -fsSL "https://api.github.com/repos/iowarp/clio-agent/releases?per_page=30" \
      | awk '/"tag_name": *"v/ { tag=$0; sub(/.*"tag_name": *"/, "", tag); sub(/".*/, "", tag) }
             /"draft": *false/ && tag != "" && !found { print tag; found=1 }
             /"draft": *true/ { tag="" }'
  else
    local resolved
    resolved="$(curl --proto '=https' --tlsv1.2 -fsSL -o /dev/null -w '%{url_effective}' "$REPO_URL/releases/latest")"
    printf '%s' "${resolved##*/}"
  fi
}

# The asset's SHA-256 from a release manifest. GitHub turns spaces in asset names into
# dots and older manifests keep the original name; text and binary markers both occur.
manifest_sha() {
  awk -v name="$2" '
    substr($0,65,2) == "  " || substr($0,65,2) == " *" {
      file=substr($0,67); sub(/^CLIO Desktop/, "CLIO.Desktop", file)
      if (file == name) print substr($0,1,64)
    }' "$1"
}

file_sha() {
  if have sha256sum; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

install_desktop() {
  [ "$OPT_SOURCE" = 0 ] || die "--desktop installs a released app; --source builds the agent only (build the app from gact-tui)"
  have curl || die "curl is required"
  local os cpu tag version asset manifest scratch
  os="$(uname -s)"; cpu="$(uname -m)"
  tag="$(desktop_tag)"
  [ -n "$tag" ] || die "could not find a CLIO release on GitHub"
  version="${tag#v}"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+|-beta\.[0-9]+(\.[0-9]+)?|(a|rc)[0-9]+)?$ ]] \
    || die "invalid release version: $tag"
  case "$os" in
    Darwin)
      [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 14 ] || die "CLIO Desktop requires macOS 14 or newer"
      # uname can describe the Rosetta shell rather than the physical machine.
      [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ] || [ "$cpu" = arm64 ] \
        || die "CLIO Desktop is not available for Intel Macs yet; install CLIO without --desktop instead"
      asset="CLIO.Desktop_${version}_aarch64-bundled.dmg"; manifest="SHA256SUMS.aarch64-apple-darwin.bundled.txt" ;;
    Linux)
      local triple deb_arch rpm_arch
      case "$cpu" in
        x86_64|amd64) triple=x86_64-unknown-linux-gnu; deb_arch=amd64; rpm_arch=x86_64 ;;
        aarch64|arm64) triple=aarch64-unknown-linux-gnu; deb_arch=arm64; rpm_arch=aarch64 ;;
        *) die "unsupported processor: $cpu" ;;
      esac
      manifest="SHA256SUMS.${triple}.bundled.txt"
      if have dpkg; then
        asset="CLIO.Desktop_${version}_${deb_arch}-bundled.deb"
      elif have rpm; then
        asset="CLIO.Desktop-${version}-1.${rpm_arch}-bundled.rpm"
      else
        die "this Linux has neither dpkg nor rpm; download a desktop package from $REPO_URL/releases/tag/$tag"
      fi ;;
    *) die "unsupported OS for --desktop: $os (use install.ps1 -Desktop on Windows)" ;;
  esac
  scratch="$(mktemp -d "${TMPDIR:-/tmp}/clio-desktop.XXXXXXXX")"
  # shellcheck disable=SC2064 # expand now: the trap must remove this run's directory
  trap "[ -d '$scratch/mount' ] && hdiutil detach '$scratch/mount' -quiet 2>/dev/null; rm -rf '$scratch'" EXIT
  say "Downloading CLIO Desktop $tag ($asset)"
  curl --proto '=https' --tlsv1.2 -fL --progress-bar --retry 3 "$REPO_URL/releases/download/$tag/$asset" -o "$scratch/$asset" \
    || die "could not download $asset from release $tag"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$REPO_URL/releases/download/$tag/$manifest" -o "$scratch/checksums" \
    || die "could not download the release checksums ($manifest)"
  local expected
  expected="$(manifest_sha "$scratch/checksums" "$asset")"
  [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || die "release checksum missing or ambiguous for $asset; nothing installed"
  [ "$(file_sha "$scratch/$asset")" = "$expected" ] || die "download checksum mismatch; nothing installed"
  if [ -n "$OPT_DOWNLOAD_ONLY" ]; then
    mkdir -p "$OPT_DOWNLOAD_ONLY"
    cp "$scratch/$asset" "$OPT_DOWNLOAD_ONLY/$asset"
    say "Verified download: $OPT_DOWNLOAD_ONLY/$asset"
    return 0
  fi
  case "$os" in
    Darwin) install_desktop_mac "$scratch" "$asset"
      prepare_desktop "$tag" "${CLIO_DESKTOP_DIR:-$HOME/Applications}/CLIO Desktop.app/Contents/MacOS/clio-desktop" ;;
    Linux) install_desktop_linux "$scratch/$asset"
      prepare_desktop "$tag" /usr/bin/clio-desktop ;;
  esac
}

# Releases before 0.9.5 have no --prepare-runtime on macOS and Linux; there an unknown
# flag would open the app instead.
desktop_prepares() {
  local version="${1#v}" base
  base="${version%%-*}"
  [ "$base" = 0.9.5 ] && [ "$base" != "$version" ] && return 1
  [ "$(printf '%s\n%s\n' 0.9.5 "$base" | sort -V | head -n 1)" = 0.9.5 ]
}

# What a first launch would do before the agent starts (its tool packages, Office),
# done now, as the person who will open the app, as the Windows installer does.
prepare_desktop() {
  local tag="$1" desktop="$2"
  desktop_prepares "$tag" || return 0
  [ -x "$desktop" ] || { say "warning: $desktop not found; the app prepares itself on first launch"; return 0; }
  say "Preparing the desktop's agent and its tools"
  local run=("$desktop" --prepare-runtime)
  if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    run=(sudo -u "$SUDO_USER" -H "$desktop" --prepare-runtime)
  fi
  if "${run[@]}"; then
    say "The desktop is ready to open."
  else
    say "warning: the desktop's preparation did not finish (above); the app finishes it on first launch"
  fi
}

install_desktop_linux() {
  local package="$1" sudo=""
  [ "$(id -u)" = 0 ] || { have sudo || die "installing $package needs root; run as root or install sudo"; sudo=sudo; }
  case "$package" in
    *.deb)
      if have apt-get; then $sudo apt-get install -y "$package" || die "apt-get could not install $package"
      else $sudo dpkg -i "$package" || die "dpkg could not install $package (missing dependencies: install them, then retry)"; fi ;;
    *.rpm)
      if have dnf; then $sudo dnf install -y "$package" || die "dnf could not install $package"
      else $sudo rpm -U --replacepkgs "$package" || die "rpm could not install $package"; fi ;;
  esac
  say "CLIO Desktop installed. Open it from your applications menu, or run clio-desktop."
}

install_desktop_mac() {
  local scratch="$1" asset="$2" destination app staging backup=""
  pgrep -x clio-desktop >/dev/null && die "Quit CLIO Desktop before installing."
  hdiutil verify "$scratch/$asset" >/dev/null || die "the disk image failed verification"
  mkdir "$scratch/mount"
  hdiutil attach "$scratch/$asset" -readonly -nobrowse -mountpoint "$scratch/mount" -quiet
  app="$scratch/mount/CLIO Desktop.app"
  [ -d "$app" ] || die "the disk image does not contain CLIO Desktop.app"
  codesign --verify --deep --strict "$app" || die "the app's signature is invalid or absent; nothing installed"
  destination="${CLIO_DESKTOP_DIR:-$HOME/Applications}"
  case "$destination" in /*) ;; *) die "CLIO_DESKTOP_DIR must be absolute" ;; esac
  mkdir -p "$destination"
  staging="$(mktemp -d "$destination/.clio-install.XXXXXXXX")"
  ditto "$app" "$staging/CLIO Desktop.app"
  codesign --verify --deep --strict "$staging/CLIO Desktop.app" || die "the copied app failed its signature check"
  if [ -e "$destination/CLIO Desktop.app" ]; then
    backup="$destination/CLIO Desktop.previous.$(date +%Y%m%d%H%M%S).$$.app"
    mv "$destination/CLIO Desktop.app" "$backup"
  fi
  if ! mv "$staging/CLIO Desktop.app" "$destination/CLIO Desktop.app"; then
    [ -z "$backup" ] || mv "$backup" "$destination/CLIO Desktop.app"
    die "could not install the app; the previous app was restored"
  fi
  rmdir "$staging"
  say "Installed: $destination/CLIO Desktop.app"
  [ -z "$backup" ] || say "Previous app kept: $backup"
  say "Open CLIO Desktop from Applications. macOS may ask once: Privacy & Security > Open Anyway."
}

if [ "$OPT_DESKTOP" = 1 ]; then
  install_desktop
  exit 0
fi
[ -z "$OPT_DOWNLOAD_ONLY" ] || die "--download-only goes with --desktop"

# ---------- defaults ---------------------------------------------------
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
PREFIX="${CLIO_PREFIX:-$agent_data/app}"
# An existing legacy install remains addressable until explicit migration.
if [[ -z "${CLIO_PREFIX:-}" && -z "${CLIO_AGENT_HOME:-}${CLIO_AGENT_DATA_DIR:-}${CLIO_USER_DIR:-}" && ! -d "$PREFIX/clio-agent/.venv" && -d "$HOME/.local/share/clio/clio-agent/.venv" ]]; then
  PREFIX="$HOME/.local/share/clio"
fi
BIN_DIR="${CLIO_BIN_DIR:-$HOME/.local/bin}"
OPT_VERSION="${OPT_VERSION:-${CLIO_VERSION:-}}"
if [ -n "${CLIO_REF:-}" ] && [ -z "$OPT_REF" ]; then
  OPT_SOURCE=1
  OPT_REF="$CLIO_REF"
fi
[ "$OPT_SOURCE" = 1 ] && [ -n "$OPT_VERSION" ] && die "--version pins a release; use --ref <tag or commit> with --source"
[ "$OPT_SOURCE" = 0 ] && [ -n "$OPT_REF" ] && die "--ref pins source; add --source"
GACT_VERSION="${GACT_VERSION:-latest}"
CLIO_INSTALLER_REF="${CLIO_INSTALLER_REF:-}"
GACT_REF="${GACT_REF:-}"
CLIO_KIT_PACKAGE="${CLIO_KIT_PACKAGE:-}"
CLIO_GIT_PROTOCOL="${CLIO_GIT_PROTOCOL:-https}"

case "$CLIO_GIT_PROTOCOL" in
  https)
    CLIO_REPO="https://github.com/iowarp/clio-agent.git"
    GACT_REPO="https://github.com/iowarp/gact-tui.git"
    ;;
  ssh)
    CLIO_REPO="git@github.com:iowarp/clio-agent.git"
    GACT_REPO="git@github.com:iowarp/gact-tui.git"
    ;;
  *)
    die "CLIO_GIT_PROTOCOL must be 'https' or 'ssh' (got: $CLIO_GIT_PROTOCOL)"
    ;;
esac

# ---------- platform detection ----------------------------------------
case "$(uname -s)" in
  Linux*)  OS=linux  ;;
  Darwin*) OS=darwin ;;
  *) die "unsupported OS: $(uname -s) (use install.ps1 on Windows)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) die "unsupported arch: $(uname -m)" ;;
esac

# ---------- prerequisite checks ---------------------------------------
have curl || die "curl is required"

# uv installs Python, the environment and the locked dependencies in every mode.
mkdir -p "$PREFIX" "$BIN_DIR"
if ! have uv; then
  say "Installing uv into $BIN_DIR"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH=1 sh >/dev/null \
    || die "could not install uv; install it (https://docs.astral.sh/uv/) and run this installer again"
  export PATH="$BIN_DIR:$PATH"
fi
if [ "$OPT_SOURCE" = 1 ]; then
  have git || die "git is required for --source"
fi
if [ -n "$GACT_REF" ]; then
  have git || die "git required when GACT_REF is set (source-build mode)"
  have go  || die "go (>= 1.26) required to build gact from source"
  [ "$OPT_SOURCE" = 1 ] || die "GACT_REF builds the TUI from source and needs --source"
fi

# The checkout this script runs from, when it runs from one (not when streamed).
CHECKOUT=""
script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  candidate="$(cd "$(dirname "$script_path")/.." && pwd)"
  if [ -f "$candidate/pyproject.toml" ] && grep -q '^name = "clio-agent"' "$candidate/pyproject.toml" \
     && [ -e "$candidate/.git" ]; then
    CHECKOUT="$candidate"
  fi
fi

# A ref's Python: its .python-version, or 3.13 for releases from before that file.
python_of() {
  local value
  value="$(tr -d '[:space:]' 2>/dev/null < "$1" || true)"
  printf '%s' "${value:-3.13}"
}

# ---------- the source clone: upgrade or repair on a re-run -----------
# The clone mode installs into $PREFIX/clio-agent. Running the installer again upgrades
# or repairs that clone in place; it never removes anything a person could have changed.

# Whether $1 holds this installer's clone to update (0), or nothing (1: absent, or what
# an earlier install left that holds no work: a partial clone an interrupted install
# left, a link to a checkout, a release install's environment, all removed here).
# Anything else is refused with its cause and the fix.
source_clone_reusable() {
  local src="$1" origin entry
  if [ -L "$src" ]; then
    say "Replacing the link to the checkout an in-place install used ($(readlink "$src")); the checkout is not touched"
    rm -f "$src"
    return 1
  fi
  [ -e "$src" ] || return 1
  if [ ! -e "$src/.git" ]; then
    for entry in "$src"/* "$src"/.[!.]*; do
      [ -e "$entry" ] || continue
      case "${entry##*/}" in
        .venv|.venv.previous|web) ;;
        *) die "Source reinstall refused: '$src' exists but is not a git clone (it holds '${entry##*/}'). Move it aside, or choose another CLIO_PREFIX, then run this installer again." ;;
      esac
    done
    say "Replacing the release install's environment in '$src' (your data lives elsewhere)"
    rm -rf "$src"
    return 1
  fi
  origin="$(git -C "$src" config --get remote.origin.url 2>/dev/null || true)"
  case "$origin" in
    https://github.com/iowarp/clio-agent|https://github.com/iowarp/clio-agent.git) ;;
    git@github.com:iowarp/clio-agent|git@github.com:iowarp/clio-agent.git) ;;
    "") die "Source reinstall refused: '$src' is a git repository with no 'origin', not a CLIO clone this installer made. Move it aside, or choose another CLIO_PREFIX, then run this installer again." ;;
    *) die "Source reinstall refused: '$src' is a clone of $origin, not of $CLIO_REPO. Move it aside, or choose another CLIO_PREFIX, then run this installer again." ;;
  esac
  # An interrupted clone has no checked-out commit (or no index yet): nothing of yours
  # is in it, so it is cloned again.
  if ! git -C "$src" rev-parse -q --verify 'HEAD^{commit}' >/dev/null 2>&1 \
     || [ ! -f "$(git -C "$src" rev-parse --absolute-git-dir 2>/dev/null)/index" ]; then
    warn "'$src' is a clone an interrupted install left unfinished; cloning it again"
    rm -rf "$src"
    return 1
  fi
  return 0
}

# The clone's changes to tracked files, submodules included, one per line ("XY path").
# A submodule merely at another commit, nothing changed inside it, is what an
# interrupted clone or submodule update leaves, and the update below resets it: it is
# not a change. (Its commits, if any were made there, stay in that submodule.)
clone_changes() {
  git -C "$1" status --porcelain=v2 --untracked-files=no | awk '
    $1 == "1" && $2 == ".M" && $3 ~ /^SC\.[.U]$/ { next }
    $1 == "1" || $1 == "2" || $1 == "u" {
      skip = ($1 == "1") ? 8 : (($1 == "2") ? 9 : 10); path = $0
      for (i = 0; i < skip; i++) sub(/^[^ ]+ /, "", path)
      print $2 " " path
    }'
}

# Bring this installer's clone at $1 to ref $2: fetch it, check it out detached, update
# the submodules. Local changes to tracked files (submodules included) refuse it.
update_source_clone() {
  local src="$1" ref="$2" dirty previous
  dirty="$(clone_changes "$src" 2>&1)" \
    || die "could not read the state of the clone '$src': $dirty"
  if [ -n "$dirty" ]; then
    die "Source reinstall refused: the clone '$src' has local changes to tracked files:
$dirty
Commit them on a branch or stash them (git -C '$src' stash), or move the clone aside, then run this installer again."
  fi
  previous="$(git -C "$src" rev-parse --short HEAD)"
  say "Updating the clio-agent clone in '$src' to $ref (was $previous)"
  git -C "$src" fetch -q --depth 1 origin "$ref" \
    || die "could not fetch '$ref' into '$src' (git's message is above): check that the branch, tag or commit exists and that this machine reaches GitHub, then run this installer again"
  git -C "$src" checkout -q --detach FETCH_HEAD \
    || die "could not check out '$ref' in '$src' (git's message is above; untracked files in the way are never removed: move them, then run this installer again)"
  git -C "$src" submodule update --init --recursive --depth 1 \
    || die "could not update the submodules of '$src' to '$ref' (git's message is above); run this installer again"
}

# ---------- the TUI build, beside the slow stages ---------------------
# A source install builds the TUI with go. It runs in the background from the moment the
# source is in place, alongside the dependency, store, managed-package and clio-kit
# stages; its output goes to a temporary log, printed (and kept) if it fails. The binary
# is built beside the installed one and moved into place only when it built.
GACT_BIN="$PREFIX/gact"
TUI_PID=""
TUI_LOG=""
TUI_OUT="$GACT_BIN.build.$$"
TUI_STARTED=0

start_tui_build() {
  if [ -n "$GACT_REF" ]; then
    say "Cloning gact-tui at $GACT_REF (source-build mode)"
    rm -rf "$PREFIX/gact-tui"
    git clone --quiet --branch "$GACT_REF" --depth 1 "$GACT_REPO" "$PREFIX/gact-tui" \
      || die "could not clone gact-tui at '$GACT_REF' from $GACT_REPO (git's message is above)"
  elif ! have go; then
    warn "TUI not built: go (>= 1.26) is not installed. Install go and run this installer again for the terminal UI; the desktop app and 'clio --web --dev' work without it."
    return 0
  fi
  TUI_LOG="$(mktemp "${TMPDIR:-/tmp}/clio-tui-build.XXXXXXXX")"
  say "Building the CLIO-branded TUI in the background (log: $TUI_LOG)"
  TUI_STARTED=$SECONDS
  # At a lower CPU priority: the stages on the install's critical path go first, the
  # compiler takes what they leave.
  local niceness="" root="$SRC/external/gact-tui"
  have nice && niceness="nice -n 10"
  [ -z "$GACT_REF" ] || root="$PREFIX/gact-tui"
  # Its own process group (job control on for this one job), so an exit stops all of it.
  set -m
  # shellcheck disable=SC2086 # $niceness is a command prefix, split on purpose
  ( cd "$SRC" && GACT_TUI_ROOT="$root" exec $niceness ./scripts/build_clio_tui.sh "$TUI_OUT" ) >"$TUI_LOG" 2>&1 &
  TUI_PID=$!
  set +m
}

finish_tui_build() {
  [ -n "$TUI_PID" ] || return 0
  local status=0
  say "Waiting for the TUI build"
  wait "$TUI_PID" || status=$?
  TUI_PID=""
  if [ "$status" != 0 ]; then
    rm -f "$TUI_OUT"
    printf '%s\n' "---- TUI build output ($TUI_LOG) ----" >&2
    tail -n 80 "$TUI_LOG" >&2 || true
    printf '%s\n' "---- end of TUI build output ----" >&2
    die "the TUI build failed (exit $status; full log: $TUI_LOG); its cause is in the output above. Fix it (often: go >= 1.26 cannot be downloaded, or the gact-tui submodule is incomplete: git -C '$SRC' submodule update --init --recursive) and run this installer again."
  fi
  mv -f "$TUI_OUT" "$GACT_BIN" || die "could not move the built TUI into place at $GACT_BIN"
  rm -f "$TUI_LOG"
  say "TUI built in $((SECONDS - TUI_STARTED)) s: $GACT_BIN"
}

# Leaving early (a failed stage, Ctrl-C) stops a TUI build still running.
on_exit() {
  if [ -n "$TUI_PID" ]; then
    kill -TERM -- "-$TUI_PID" 2>/dev/null || kill -TERM "$TUI_PID" 2>/dev/null || true
    rm -f "$TUI_OUT" "$TUI_LOG"
  fi
  [ -z "${lockdir:-}" ] || rm -rf "$lockdir"
}
trap on_exit EXIT

# ---------- install clio-agent ----------------------------------------
VENV="$PREFIX/clio-agent/.venv"
SRC=""

if [ "$OPT_SOURCE" = 1 ]; then
  if [ -n "$CHECKOUT" ] && [ -z "$OPT_REF" ]; then
    SRC="$CHECKOUT"
    say "Installing the checkout in place: $SRC ($(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached))"
    if [ -L "$PREFIX/clio-agent" ] || [ ! -e "$PREFIX/clio-agent" ]; then
      :
    elif [ -e "$PREFIX/clio-agent/.git" ]; then
      die "Source reinstall refused: '$PREFIX/clio-agent' is a clone that may hold your changes. Move it, or choose another CLIO_PREFIX."
    else
      rm -rf "$PREFIX/clio-agent"  # a release install's environment; data lives elsewhere
    fi
    ln -sfn "$SRC" "$PREFIX/clio-agent"
    git -C "$SRC" submodule update --init --recursive
    py="$(python_of "$SRC/.python-version")"
    sync_args=(sync --python "$py" --extra argonne --extra flowcept)
  else
    ref="${OPT_REF:-$([ "$OPT_DEV" = 1 ] && echo develop || echo main)}"
    SRC="$PREFIX/clio-agent"
    if source_clone_reusable "$SRC"; then
      update_source_clone "$SRC" "$ref"
    else
      say "Cloning clio-agent at $ref"
      if ! git clone --quiet --recurse-submodules --shallow-submodules --branch "$ref" --depth 1 "$CLIO_REPO" "$SRC" 2>/dev/null; then
        # A commit is not a branch or tag: fetch it by id.
        rm -rf "$SRC"
        git init -q "$SRC" && git -C "$SRC" remote add origin "$CLIO_REPO" \
          && git -C "$SRC" fetch -q --depth 1 origin "$ref" && git -C "$SRC" checkout -q FETCH_HEAD \
          && git -C "$SRC" submodule update --init --recursive --depth 1 \
          || die "could not fetch clio-agent at '$ref' from $CLIO_REPO: check that the branch, tag or commit exists and that this machine reaches GitHub, then run this installer again"
      fi
    fi
    py="$(python_of "$SRC/.python-version")"
    sync_args=(sync --locked --python "$py" --extra argonne --extra flowcept)
  fi
  # The TUI builds from the source now, beside the slow stages below.
  start_tui_build
  say "Installing clio-agent deps (uv ${sync_args[*]})"
  sync_status=0
  ( cd "$SRC" && uv "${sync_args[@]}" ) || sync_status=$?
  [ "$sync_status" = 0 ] \
    || die_with "$sync_status" "uv could not install clio-agent's dependencies (exit $sync_status; uv's message above names the cause). Fix it and run this installer again."
else
  if [ -n "$OPT_VERSION" ]; then
    package_version="$OPT_VERSION"
    if [[ "$package_version" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)\.([0-9]+)$ ]]; then
      package_version="${BASH_REMATCH[1]}b${BASH_REMATCH[2]}.post${BASH_REMATCH[3]}"
    elif [[ "$package_version" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)$ ]]; then
      package_version="${BASH_REMATCH[1]}b${BASH_REMATCH[2]}"
    fi
    package_version="${package_version#v}"
  else
    # The published GitHub Releases are the release list (PyPI can hold strays that
    # never became a release). The default takes the highest stable version; --dev the
    # most recently published release, pre-releases included.
    which="stable"; [ "$OPT_DEV" = 1 ] && which="any"
    say "Resolving the latest $([ "$which" = any ] && echo release || echo stable release)"
    package_version="$(curl -fsSL "https://api.github.com/repos/iowarp/clio-agent/releases?per_page=100" \
      | uv run --quiet --no-project --python 3.13 --with packaging python -c '
import json, re, sys
from packaging.version import InvalidVersion, Version
found = []
for release in json.load(sys.stdin):
    if release.get("draft"):
        continue
    tag = release.get("tag_name", "")
    match = re.fullmatch(r"v(\d+\.\d+\.\d+(?:\.\d+)?)-beta\.(\d+)(?:\.(\d+))?", tag)
    text = (f"{match[1]}b{match[2]}" + (f".post{match[3]}" if match[3] else "")) if match else tag[1:]
    try:
        version = Version(text)
    except InvalidVersion:
        continue  # not a clio-agent release tag
    if sys.argv[1] == "stable" and version.is_prerelease:
        continue
    found.append((release.get("published_at") or "", version))
# Stable: the highest stable version. --dev: the most recently published release,
# whatever its version says (an old alpha can sort above the newest beta).
print(max(v for _, v in found) if sys.argv[1] == "stable" else max(found)[1])
' "$which")" || die "could not read the clio-agent releases from GitHub"
  fi
  tag="$(release_tag "$package_version")"
  lockdir="$(mktemp -d)"  # removed on exit (on_exit)
  for f in pyproject.toml uv.lock .python-version; do
    curl -fsSL "https://raw.githubusercontent.com/iowarp/clio-agent/$tag/$f" -o "$lockdir/$f" 2>/dev/null \
      || { [ "$f" = .python-version ] || die "release $tag has no $f on GitHub"; }
  done
  py="$(python_of "$lockdir/.python-version")"
  ( cd "$lockdir" && uv export --quiet --frozen --no-hashes --no-emit-project \
      --extra argonne --extra flowcept -o constraints.txt ) \
    || die "could not read the dependency lock of release $tag"
  pkg_spec="clio-agent[argonne,flowcept]==$package_version"
  # On macOS, select an available Rasterio wheel for the user's OS instead
  # of trying to compile a newer release against a missing system GDAL.
  wheel_arg=""
  [ "$OS" != darwin ] || wheel_arg="--only-binary=rasterio"
  say "Installing $pkg_spec from PyPI (Python $py, locked by $tag)"
  # The installed environment is set aside, not deleted, until the new one is in
  # place: an install that fails or is interrupted puts it back.
  previous=""
  if [ -d "$VENV" ]; then
    previous="$VENV.previous"
    rm -rf "$previous"
    mv "$VENV" "$previous"
  fi
  mkdir -p "$PREFIX/clio-agent"
  if ! { uv venv --python "$py" "$VENV" >/dev/null \
      && uv pip install --quiet --python "$VENV/bin/python" ${wheel_arg:+"$wheel_arg"} \
        --constraint "$lockdir/constraints.txt" "$pkg_spec"; }; then
    rm -rf "$VENV"
    [ -z "$previous" ] || mv "$previous" "$VENV"
    die "could not install $pkg_spec${previous:+; the previous installation is unchanged}"
  fi
  [ -z "$previous" ] || rm -rf "$previous"
  CLIO_VERSION="$package_version"
fi

CLIO_INSTALLED_VERSION=""
if [ -x "$VENV/bin/python" ]; then
  CLIO_INSTALLED_VERSION="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("clio-agent"))' 2>/dev/null || true)"
fi

# ---------- upgrade the store: migrations run here, never at boot ------
# Idempotent; a second run is a no-op. A running CLIO refuses it (store_in_use): its
# next boot runs the owed migrations once and reports them in the health rows.
doc_flags=()
if "$VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.gact.store_migrate_command") else 3)' 2>/dev/null; then
  say "Migrating the CLIO store (clio-agent migrate)"
  migrate_status=0
  "$VENV/bin/python" -m clio_agent.gact.store_migrate_command || migrate_status=$?
  case "$migrate_status" in
    0) ;;
    3) warn "store in use by a running CLIO: its next start runs the owed migrations once (or run 'clio stop' then 'clio-agent migrate')" ;;
    *) warn "clio-agent migrate failed (exit $migrate_status, see the lines above): CLIO's next start retries the owed migrations once and reports them in 'clio doctor'" ;;
  esac
  doc_flags=(--skip-store-migrations)
else
  say "clio-agent $CLIO_INSTALLED_VERSION has no 'clio-agent migrate'; its boot migrates the store"
fi

if "$VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.runtime.document_install") else 3)' 2>/dev/null; then
  say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
  docs_status=0
  "$VENV/bin/python" -m clio_agent.runtime.document_install ${doc_flags[@]+"${doc_flags[@]}"} || docs_status=$?
  [ "$docs_status" = 0 ] \
    || die_with "$docs_status" "installing the managed packages and Office rendering failed (exit $docs_status); the cause is in the lines above. Fix it and run this installer again: it resumes from what is already installed."
else
  # Releases before the managed document runtime set it up on their own.
  say "clio-agent $CLIO_INSTALLED_VERSION has no managed document runtime step; skipping it"
fi

# ---------- provision clio-kit MCP runtime ----------------------------
# Marketplace packs launch their MCP servers via the installed `clio-kit
# mcp-server <name>` launcher. The default matches this CLIO release's Kit pin;
# explicit legacy shared-runtime candidates select their science dependency union.
# Installed-tool launchers (not per-server `uvx` calls)
# avoid the concurrent cold-cache ephemeral-env race and `uv cache prune`
# deleting envs under running servers (astral-sh/uv#11694). `uv tool install`
# is idempotent (re-run is a no-op / version pin). Needs uv; without it the
# packs' tools simply stay unprovisioned until uv is installed.
if have uv; then
  if [ -z "$CLIO_KIT_PACKAGE" ]; then
    clio_kit_spec="clio-kit==2.11.1"
    say "Provisioning released clio-kit launcher ($clio_kit_spec)"
  elif [[ "$CLIO_KIT_PACKAGE" == *"=="* ]]; then
    clio_kit_spec="${CLIO_KIT_PACKAGE%%==*}[science]==${CLIO_KIT_PACKAGE#*==}"
    say "Provisioning candidate shared clio-kit science runtime ($clio_kit_spec)"
  else
    clio_kit_spec="${CLIO_KIT_PACKAGE}[science]"
    say "Provisioning candidate shared clio-kit science runtime ($clio_kit_spec)"
  fi
  uv tool install --python "$py" "$clio_kit_spec" || warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until 'uv tool install \"$clio_kit_spec\"' succeeds and '\$(uv tool dir --bin)' is on PATH"
else
  warn "uv not found — skipping clio-kit MCP runtime provisioning; install uv and rerun this installer"
fi

# ---------- install gact ----------------------------------------------
if [ "$OPT_SOURCE" = 1 ]; then
  finish_tui_build  # started with the source, above
else
  tag="$GACT_VERSION"
  if [ "$tag" = "latest" ]; then
    if [ -n "${CLIO_INSTALLED_VERSION:-$CLIO_VERSION}" ]; then
      tag="$(release_tag "${CLIO_INSTALLED_VERSION:-$CLIO_VERSION}")"
    else
      say "Resolving latest clio-agent release"
      tag="$(curl -fsSL https://api.github.com/repos/iowarp/clio-agent/releases/latest \
            | sed -nE 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/p' \
            | head -n1 || true)"
      [ -n "$tag" ] || die "couldn't resolve clio-agent latest release tag"
    fi
  fi
  asset="clio-tui-${OS}-${ARCH}"
  url="https://github.com/iowarp/clio-agent/releases/download/${tag}/${asset}"
  say "Downloading $asset from clio-agent $tag"
  curl -fsSL "$url" -o "$GACT_BIN" || die "failed to download $url"
  chmod +x "$GACT_BIN"

  # Web UI bundle (powers `clio --web`): optional, best-effort. The release
  # ships clio-web-<version>.zip containing a clio-web-<version>/ dist dir; we
  # unpack it into $PREFIX/clio-agent/web, which the launcher serves same-origin.
  webver="${tag#v}"
  web_url="https://github.com/iowarp/clio-agent/releases/download/${tag}/clio-web-${webver}.zip"
  if command -v unzip >/dev/null 2>&1 && curl -fsSL "$web_url" -o "$PREFIX/clio-web.zip" 2>/dev/null; then
    rm -rf "$PREFIX/_webtmp" "$PREFIX/clio-agent/web"
    unzip -q -o "$PREFIX/clio-web.zip" -d "$PREFIX/_webtmp"
    mkdir -p "$PREFIX/clio-agent/web"
    cp -r "$PREFIX/_webtmp/clio-web-${webver}/." "$PREFIX/clio-agent/web/" 2>/dev/null \
      || cp -r "$PREFIX/_webtmp/." "$PREFIX/clio-agent/web/"
    rm -rf "$PREFIX/_webtmp" "$PREFIX/clio-web.zip"
    say "Web UI bundle installed (run: clio --web)"
  else
    warn "web UI bundle unavailable for $tag — 'clio --web' disabled until reinstalled with it"
  fi
fi

# ---------- launcher + uninstaller ------------------------------------
# When we cloned clio-agent (source mode), the scripts are already on
# disk. In release mode, fetch them from the ref that matches the
# installed PyPI version, unless an explicit installer ref is provided.
launcher_ref="${CLIO_INSTALLER_REF:-}"
if [ -z "$launcher_ref" ] && [ -n "$CLIO_INSTALLED_VERSION" ]; then
  launcher_ref="$(release_tag "$CLIO_INSTALLED_VERSION")"
fi
launcher_ref="${launcher_ref:-main}"
RAW="https://raw.githubusercontent.com/iowarp/clio-agent/${launcher_ref}/install"

# The installed release's own launcher lives in the install; `clio` on PATH pins
# this install's location, so a launcher from any release (whose built-in default
# location may differ) finds it.
LAUNCHER="$BIN_DIR/clio"
RELEASE_LAUNCHER="$PREFIX/libexec/clio"
say "Installing launcher: $LAUNCHER"
mkdir -p "$PREFIX/libexec"
if [ "$OPT_SOURCE" = 1 ]; then
  cp "$SRC/install/clio" "$RELEASE_LAUNCHER"
else
  curl -fsSL "$RAW/clio" -o "$RELEASE_LAUNCHER"
fi
chmod +x "$RELEASE_LAUNCHER"
# An install made for its own data home (an SSH deployment's <root>/user) keeps it,
# so starting it by hand uses the same conversations as the deployment does.
agent_home_line=""
[ -n "${CLIO_AGENT_HOME:-}" ] && agent_home_line="export CLIO_AGENT_HOME=\"\${CLIO_AGENT_HOME:-$CLIO_AGENT_HOME}\""
cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
# CLIO: the installation in $PREFIX, through that installation's own launcher.
export CLIO_PREFIX="\${CLIO_PREFIX:-$PREFIX}"
export CLIO_BIN_DIR="\${CLIO_BIN_DIR:-$BIN_DIR}"
$agent_home_line
exec "$RELEASE_LAUNCHER" "\$@"
LAUNCH
chmod +x "$LAUNCHER"

say "Installing uninstaller: $PREFIX/uninstall.sh"
if [ "$OPT_SOURCE" = 1 ]; then
  cp "$SRC/install/uninstall.sh" "$PREFIX/uninstall.sh"
else
  curl -fsSL "$RAW/uninstall.sh" -o "$PREFIX/uninstall.sh"
fi
chmod +x "$PREFIX/uninstall.sh"

# ---------- finishing notes -------------------------------------------
say "Done."
clio_src="$(if [ "$OPT_SOURCE" = 1 ]; then echo "source: $SRC ($(git -C "$SRC" rev-parse --short HEAD 2>/dev/null))"; else echo "PyPI: $CLIO_VERSION"; fi)"
gact_src="$(if [ -n "$GACT_REF" ]; then echo "source: $GACT_REF"; elif [ "$OPT_SOURCE" = 1 ]; then echo "built from the checkout's submodule"; else echo "release: $tag"; fi)"
cat <<EOF

Installed to:        $PREFIX
Launcher:            $LAUNCHER
clio-agent:          $clio_src
gact:                $gact_src

Next steps:
  1. Make sure $BIN_DIR is on your PATH.
  2. Run:   clio
  3. Manage the server: clio status | clio restart | clio logs
  4. Tab-completion:    clio completion bash >> ~/.bashrc   (or zsh)
  5. Uninstall:         clio uninstall   (add --purge to drop config)

EOF
