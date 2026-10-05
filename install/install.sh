#!/usr/bin/env bash
# CLIO installer (Linux / macOS).
#
# Default: pulls clio-agent from PyPI and downloads a prebuilt CLIO-branded
# `clio-tui` binary from clio-agent's GitHub Releases. No `git` or `go` required;
# you only need `uv` or `pip`.
#
# Source-build mode (opt-in for tracking unreleased work): set
# CLIO_REF=<branch> and/or GACT_REF=<branch> to clone-and-build the
# selected component instead. Source mode for clio-agent needs `git`
# + `uv`; source mode for gact-tui needs `git` + `go` 1.26+.
#
# Honours environment overrides:
#   CLIO_PREFIX        install root         (default: $HOME/.local/share/clio)
#   CLIO_BIN_DIR       launcher location    (default: $HOME/.local/bin)
#   CLIO_VERSION       pin clio-agent       (default: latest from PyPI)
#   GACT_VERSION       legacy override for TUI release tag (default: match CLIO)
#   CLIO_INSTALLER_REF pin launcher scripts (default: v<installed clio-agent>)
#   CLIO_REF           clio-agent branch    (default: release mode)
#   GACT_REF           gact-tui branch      (default: release mode)
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: empty;
#                                            released 2.10.6 remains legacy)
#   CLIO_GIT_PROTOCOL  https | ssh          (default: https; only used
#                                            in source-build mode)
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

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; RESET='\033[0m'
say()  { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!! ${RESET} %s\n" "$*" >&2; }
die()  { printf "${RED}xx ${RESET} %s\n" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# PyPI package spelling and GitHub tag spelling differ for beta releases.
release_tag() {
  local version="${1#v}"
  if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?)b([0-9]+)$ ]]; then
    printf 'v%s-beta.%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}"
  elif [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?(-beta\.[0-9]+)?$ ]]; then
    printf 'v%s' "$version"
  else
    die "no GitHub release tag for package version: $version"
  fi
}

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
CLIO_VERSION="${CLIO_VERSION:-}"
GACT_VERSION="${GACT_VERSION:-latest}"
CLIO_INSTALLER_REF="${CLIO_INSTALLER_REF:-}"
CLIO_REF="${CLIO_REF:-}"
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

# Need a Python installer for clio-agent. uv is preferred (handles
# venv + Python toolchain itself). Native core wheels support Python 3.13;
# use the bundle's tested 3.13 interpreter instead of selecting the newest Python.
PYINSTALL=""
if   have uv;   then PYINSTALL=uv
elif have pip3; then PYINSTALL=pip3
elif have pip;  then PYINSTALL=pip
else
  die "need uv or pip to install clio-agent. install uv with: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

if [ "$PYINSTALL" != "uv" ]; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' \
    || die "pip installation requires Python 3.13. Install uv to provision Python 3.13 automatically."
fi

if [ -n "$CLIO_REF" ]; then
  have git || die "git required when CLIO_REF is set (source-build mode)"
  have uv  || die "uv required to build clio-agent from source"
  have go  || die "go (>= 1.26) required when CLIO_REF is set so the CLIO TUI can be built"
fi
if [ -n "$GACT_REF" ]; then
  have git || die "git required when GACT_REF is set (source-build mode)"
  have go  || die "go (>= 1.26) required to build gact from source"
  [ -n "$CLIO_REF" ] || die "GACT_REF source-build mode now requires CLIO_REF so CLIO branding scripts are available"
fi

mkdir -p "$PREFIX" "$BIN_DIR"

# ---------- install clio-agent ----------------------------------------
VENV="$PREFIX/clio-agent/.venv"

if [ -n "$CLIO_REF" ]; then
  say "Cloning clio-agent at $CLIO_REF (source-build mode)"
  if [[ -e "$PREFIX/clio-agent" ]]; then
    die "Source reinstall refused: '$PREFIX/clio-agent' already exists. Choose a new CLIO_PREFIX or explicitly move the existing installation after migrating its user data."
  fi
  git clone --quiet --recurse-submodules --shallow-submodules --branch "$CLIO_REF" --depth 1 "$CLIO_REPO" "$PREFIX/clio-agent"
  say "Installing clio-agent deps (uv sync --python 3.13 --extra argonne)"
  ( cd "$PREFIX/clio-agent" && uv sync --python 3.13 --extra argonne )
else
  pkg_spec="clio-agent[argonne]${CLIO_VERSION:+==$CLIO_VERSION}"
  # On macOS, select an available Rasterio wheel for the user's OS instead
  # of trying to compile a newer release against a missing system GDAL.
  wheel_arg=""
  [ "$OS" != darwin ] || wheel_arg="--only-binary=rasterio"
  say "Installing $pkg_spec from PyPI"
  rm -rf "$VENV"
  mkdir -p "$PREFIX/clio-agent"
  if [ "$PYINSTALL" = "uv" ]; then
    uv venv --python 3.13 "$VENV" >/dev/null
    uv pip install --quiet --python "$VENV/bin/python" ${wheel_arg:+"$wheel_arg"} "$pkg_spec" \
      "dspy==3.4.0" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5" \
      "fastmcp-tasks==4.0.0b5"
  else
    python3 -m venv "$VENV"
    "$VENV/bin/$PYINSTALL" install --quiet --upgrade pip
    "$VENV/bin/$PYINSTALL" install --quiet ${wheel_arg:+"$wheel_arg"} "$pkg_spec"
  fi
fi

CLIO_INSTALLED_VERSION=""
if [ -x "$VENV/bin/python" ]; then
  CLIO_INSTALLED_VERSION="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("clio-agent"))' 2>/dev/null || true)"
fi

say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
"$VENV/bin/python" -m clio_agent.runtime.document_install

# ---------- provision clio-kit MCP runtime ----------------------------
# Marketplace packs launch their MCP servers via the installed `clio-kit
# mcp-server <name>` launcher. The released default retains legacy behavior; an
# explicit candidate selects the compatible science dependency union once.
# Installed-tool launchers (not per-server `uvx` calls)
# avoid the concurrent cold-cache ephemeral-env race and `uv cache prune`
# deleting envs under running servers (astral-sh/uv#11694). `uv tool install`
# is idempotent (re-run is a no-op / version pin). Needs uv; without it the
# packs' tools simply stay unprovisioned until uv is installed.
if have uv; then
  if [ -z "$CLIO_KIT_PACKAGE" ]; then
    clio_kit_spec="clio-kit==2.10.6"
    say "Provisioning released clio-kit launcher ($clio_kit_spec; legacy runtime semantics)"
  elif [[ "$CLIO_KIT_PACKAGE" == *"=="* ]]; then
    clio_kit_spec="${CLIO_KIT_PACKAGE%%==*}[science]==${CLIO_KIT_PACKAGE#*==}"
    say "Provisioning candidate shared clio-kit science runtime ($clio_kit_spec)"
  else
    clio_kit_spec="${CLIO_KIT_PACKAGE}[science]"
    say "Provisioning candidate shared clio-kit science runtime ($clio_kit_spec)"
  fi
  uv tool install --python 3.13 "$clio_kit_spec" || warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until 'uv tool install \"$clio_kit_spec\"' succeeds and '\$(uv tool dir --bin)' is on PATH"
else
  warn "uv not found — skipping clio-kit MCP runtime provisioning; install uv and rerun this installer"
fi

# ---------- install gact ----------------------------------------------
GACT_BIN="$PREFIX/gact"

if [ -n "$CLIO_REF" ] && [ -z "$GACT_REF" ]; then
  say "Building CLIO-branded TUI from clio-agent submodule"
  ( cd "$PREFIX/clio-agent" && ./scripts/build_clio_tui.sh "$GACT_BIN" )
elif [ -n "$GACT_REF" ]; then
  say "Cloning gact-tui at $GACT_REF (source-build mode)"
  rm -rf "$PREFIX/gact-tui"
  git clone --quiet --branch "$GACT_REF" --depth 1 "$GACT_REPO" "$PREFIX/gact-tui"
  say "Building CLIO-branded TUI"
  GACT_TUI_ROOT="$PREFIX/gact-tui" "$PREFIX/clio-agent/scripts/build_clio_tui.sh" "$GACT_BIN"
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
launcher_ref="${CLIO_REF:-${CLIO_INSTALLER_REF:-}}"
if [ -z "$launcher_ref" ] && [ -n "$CLIO_INSTALLED_VERSION" ]; then
  launcher_ref="$(release_tag "$CLIO_INSTALLED_VERSION")"
fi
launcher_ref="${launcher_ref:-main}"
RAW="https://raw.githubusercontent.com/iowarp/clio-agent/${launcher_ref}/install"

LAUNCHER="$BIN_DIR/clio"
say "Installing launcher: $LAUNCHER"
if [ -n "$CLIO_REF" ]; then
  cp "$PREFIX/clio-agent/install/clio" "$LAUNCHER"
else
  curl -fsSL "$RAW/clio" -o "$LAUNCHER"
fi
chmod +x "$LAUNCHER"

say "Installing uninstaller: $PREFIX/uninstall.sh"
if [ -n "$CLIO_REF" ]; then
  cp "$PREFIX/clio-agent/install/uninstall.sh" "$PREFIX/uninstall.sh"
else
  curl -fsSL "$RAW/uninstall.sh" -o "$PREFIX/uninstall.sh"
fi
chmod +x "$PREFIX/uninstall.sh"

# ---------- finishing notes -------------------------------------------
say "Done."
clio_src="$(if [ -n "$CLIO_REF" ]; then echo "source: $CLIO_REF"; else echo "PyPI: ${CLIO_VERSION:-latest}"; fi)"
gact_src="$(if [ -n "$GACT_REF" ]; then echo "source: $GACT_REF"; else echo "release: $tag"; fi)"
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
