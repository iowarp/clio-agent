#!/usr/bin/env bash
# CLIO installer (Linux / macOS).
#
# Default: pulls clio-agent from PyPI and downloads a prebuilt `gact`
# binary from gact-tui's GitHub Releases. No `git` or `go` required;
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
#   GACT_VERSION       pin gact release tag (default: latest)
#   CLIO_INSTALLER_REF pin launcher scripts (default: v<installed clio-agent>)
#   CLIO_REF           clio-agent branch    (default: release mode)
#   GACT_REF           gact-tui branch      (default: release mode)
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

# ---------- defaults ---------------------------------------------------
PREFIX="${CLIO_PREFIX:-$HOME/.local/share/clio}"
BIN_DIR="${CLIO_BIN_DIR:-$HOME/.local/bin}"
CLIO_VERSION="${CLIO_VERSION:-}"
GACT_VERSION="${GACT_VERSION:-latest}"
CLIO_INSTALLER_REF="${CLIO_INSTALLER_REF:-}"
CLIO_REF="${CLIO_REF:-}"
GACT_REF="${GACT_REF:-}"
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
# venv + Python toolchain itself); pip works if Python 3.12+ is on PATH.
PYINSTALL=""
if   have uv;   then PYINSTALL=uv
elif have pip3; then PYINSTALL=pip3
elif have pip;  then PYINSTALL=pip
else
  die "need uv or pip to install clio-agent. install uv with: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

if [ -n "$CLIO_REF" ]; then
  have git || die "git required when CLIO_REF is set (source-build mode)"
  have uv  || die "uv required to build clio-agent from source"
fi
if [ -n "$GACT_REF" ]; then
  have git || die "git required when GACT_REF is set (source-build mode)"
  have go  || die "go (>= 1.26) required to build gact from source"
fi

mkdir -p "$PREFIX" "$BIN_DIR"

# ---------- install clio-agent ----------------------------------------
VENV="$PREFIX/clio-agent/.venv"

if [ -n "$CLIO_REF" ]; then
  say "Cloning clio-agent at $CLIO_REF (source-build mode)"
  rm -rf "$PREFIX/clio-agent"
  git clone --quiet --branch "$CLIO_REF" --depth 1 "$CLIO_REPO" "$PREFIX/clio-agent"
  say "Installing clio-agent deps (uv sync)"
  ( cd "$PREFIX/clio-agent" && uv sync )
else
  pkg_spec="clio-agent${CLIO_VERSION:+==$CLIO_VERSION}"
  say "Installing $pkg_spec from PyPI"
  rm -rf "$PREFIX/clio-agent"
  mkdir -p "$PREFIX/clio-agent"
  if [ "$PYINSTALL" = "uv" ]; then
    uv venv --python ">=3.12" "$VENV" >/dev/null
    uv pip install --quiet --python "$VENV/bin/python" "$pkg_spec"
  else
    python3 -m venv "$VENV"
    "$VENV/bin/$PYINSTALL" install --quiet --upgrade pip
    "$VENV/bin/$PYINSTALL" install --quiet "$pkg_spec"
  fi
fi

CLIO_INSTALLED_VERSION=""
if [ -x "$VENV/bin/python" ]; then
  CLIO_INSTALLED_VERSION="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("clio-agent"))' 2>/dev/null || true)"
fi

# ---------- install gact ----------------------------------------------
GACT_BIN="$PREFIX/gact"

if [ -n "$GACT_REF" ]; then
  say "Cloning gact-tui at $GACT_REF (source-build mode)"
  rm -rf "$PREFIX/gact-tui"
  git clone --quiet --branch "$GACT_REF" --depth 1 "$GACT_REPO" "$PREFIX/gact-tui"
  say "Building gact"
  ( cd "$PREFIX/gact-tui/tui" && go build -o "$GACT_BIN" . )
else
  tag="$GACT_VERSION"
  if [ "$tag" = "latest" ]; then
    say "Resolving latest gact-tui release"
    tag="$(curl -fsSL https://api.github.com/repos/iowarp/gact-tui/releases/latest \
            | sed -nE 's/.*"tag_name":\s*"([^"]+)".*/\1/p' \
            | head -n1 || true)"
    [ -n "$tag" ] || die "couldn't resolve gact-tui latest release tag"
  fi
  asset="gact-${OS}-${ARCH}"
  url="https://github.com/iowarp/gact-tui/releases/download/${tag}/${asset}"
  say "Downloading $asset from gact-tui $tag"
  curl -fsSL "$url" -o "$GACT_BIN" || die "failed to download $url"
  chmod +x "$GACT_BIN"
fi

# ---------- launcher + uninstaller ------------------------------------
# When we cloned clio-agent (source mode), the scripts are already on
# disk. In release mode, fetch them from the ref that matches the
# installed PyPI version, unless an explicit installer ref is provided.
launcher_ref="${CLIO_REF:-${CLIO_INSTALLER_REF:-}}"
if [ -z "$launcher_ref" ] && [ -n "$CLIO_INSTALLED_VERSION" ]; then
  launcher_ref="v$CLIO_INSTALLED_VERSION"
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
