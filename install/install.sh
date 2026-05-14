#!/usr/bin/env bash
# CLIO + GACT TUI installer (Linux / macOS).
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh
#
# Honors environment overrides:
#   CLIO_PREFIX        install root (default: $HOME/.local/share/clio)
#   CLIO_REF           clio-agent git ref/tag (default: v0.3.1)
#   GACT_REF           gact-tui git ref/tag    (default: v0.2.1)
#   CLIO_BIN_DIR       where to drop the `clio` launcher (default: $HOME/.local/bin)
#   CLIO_NONINTERACTIVE  skip prerequisite prompts (assumes you have them)

set -euo pipefail

# ---------- pretty output ----------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
RESET='\033[0m'
say()  { printf "${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!! ${RESET} %s\n" "$*" >&2; }
die()  { printf "${RED}xx ${RESET} %s\n" "$*" >&2; exit 1; }

# ---------- defaults ---------------------------------------------------
PREFIX="${CLIO_PREFIX:-$HOME/.local/share/clio}"
BIN_DIR="${CLIO_BIN_DIR:-$HOME/.local/bin}"
# Default to the main branches so a bare `curl | sh` tracks the latest
# released state. GACT_REF stays on `clio` until iowarp/gact-tui#12
# (clio -> main) merges, after which it can move to `main` too.
CLIO_REF="${CLIO_REF:-main}"
GACT_REF="${GACT_REF:-clio}"
# Default to HTTPS for the one-liner UX, but allow override to SSH for
# users who only have SSH access (and for the period while the repos
# are still private — anonymous HTTPS returns 404). Set
#   CLIO_GIT_PROTOCOL=ssh
# to switch both URLs to git@github.com:.../...git form.
CLIO_GIT_PROTOCOL="${CLIO_GIT_PROTOCOL:-https}"
case "$CLIO_GIT_PROTOCOL" in
  https)
    CLIO_REPO="https://github.com/iowarp/clio-agent.git"
    GACT_REPO="https://github.com/JaimeCernuda/gact-tui.git"
    ;;
  ssh)
    CLIO_REPO="git@github.com:iowarp/clio-agent.git"
    GACT_REPO="git@github.com:JaimeCernuda/gact-tui.git"
    ;;
  *)
    die "CLIO_GIT_PROTOCOL must be 'https' or 'ssh' (got: $CLIO_GIT_PROTOCOL)"
    ;;
esac

# ---------- platform detection ----------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="darwin" ;;
  *) die "unsupported OS: $OS (use install.ps1 on Windows)" ;;
esac
case "$ARCH" in
  x86_64|amd64) ARCH_NORM="x64" ;;
  aarch64|arm64) ARCH_NORM="arm64" ;;
  *) warn "untested arch: $ARCH; continuing" ; ARCH_NORM="$ARCH" ;;
esac
say "Detected $PLATFORM/$ARCH_NORM"

# ---------- prerequisite checks ---------------------------------------
need_install=()
have() { command -v "$1" >/dev/null 2>&1; }

have git || need_install+=("git")
have uv  || need_install+=("uv")
have go  || need_install+=("go (>=1.26)")

if [ "${#need_install[@]}" -gt 0 ]; then
  warn "Missing prerequisites: ${need_install[*]}"
  cat <<EOF >&2

Install them first, then re-run:

  uv:   curl -LsSf https://astral.sh/uv/install.sh | sh
  go:   https://go.dev/dl/  (need 1.26+)
  git:  apt install git    (Debian/Ubuntu)
        dnf install git    (Fedora/RHEL)
        brew install git   (macOS)

Then re-run:
  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh

EOF
  exit 1
fi

GO_VERSION="$(go version | awk '{print $3}' | sed 's/^go//')"
say "Using go $GO_VERSION, uv $(uv --version | awk '{print $2}')"

# ---------- clone + build ---------------------------------------------
mkdir -p "$PREFIX" "$BIN_DIR"
cd "$PREFIX"

clone_or_update() {
  local repo="$1" dir="$2" ref="$3"
  if [ -d "$dir/.git" ]; then
    say "Updating $dir → $ref"
    git -C "$dir" fetch --tags --quiet
    git -C "$dir" checkout --quiet "$ref"
  else
    say "Cloning $dir → $ref"
    if ! GIT_TERMINAL_PROMPT=0 git clone --quiet --branch "$ref" --depth 1 "$repo" "$dir" 2>/tmp/clio-clone-err.log; then
      cat /tmp/clio-clone-err.log >&2
      cat <<EOF >&2

clone failed for $repo

If the repo is private, retry over SSH:
  CLIO_GIT_PROTOCOL=ssh \\
    curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh

(SSH needs an authenticated GitHub key on this machine.)

EOF
      exit 1
    fi
  fi
}

clone_or_update "$CLIO_REPO" "clio-agent" "$CLIO_REF"
clone_or_update "$GACT_REPO" "gact-tui"   "$GACT_REF"

say "Installing CLIO Python deps (uv sync --extra api)"
( cd "$PREFIX/clio-agent" && uv sync --extra api )

say "Building gact TUI"
( cd "$PREFIX/gact-tui/tui" && go build -o "$PREFIX/gact" . )

# ---------- launcher + uninstaller -------------------------------------
# The launcher is a real CLI (install/clio) checked into the clio-agent
# repo. We copy it from the freshly-cloned checkout rather than
# generating it inline, so there is one source of truth and
# `clio start|stop|restart|status|logs|doctor|report` ship with it.
INSTALL_SRC="$PREFIX/clio-agent/install"
LAUNCHER="$BIN_DIR/clio"

say "Installing launcher: $LAUNCHER"
cp "$INSTALL_SRC/clio" "$LAUNCHER"
chmod +x "$LAUNCHER"

say "Installing uninstaller: $PREFIX/uninstall.sh"
cp "$INSTALL_SRC/uninstall.sh" "$PREFIX/uninstall.sh"
chmod +x "$PREFIX/uninstall.sh"

# ---------- finishing notes -------------------------------------------
say "Done."
cat <<EOF

Installed to:        $PREFIX
Launcher:            $LAUNCHER
clio-agent ref:      $CLIO_REF
gact-tui  ref:       $GACT_REF

Next steps:
  1. Make sure $BIN_DIR is on your PATH.
  2. Run:   clio
     The TUI pops the LM-provider modal on first connect.
  3. Manage the server: clio status | clio restart | clio logs
  4. Tab-completion:    clio completion bash >> ~/.bashrc   (or zsh)
  5. Uninstall:         clio uninstall   (add --purge to drop config)

EOF
