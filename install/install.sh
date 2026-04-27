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
CLIO_REF="${CLIO_REF:-v0.3.1}"
GACT_REF="${GACT_REF:-v0.2.1}"
CLIO_REPO="https://github.com/iowarp/clio-agent.git"
GACT_REPO="https://github.com/JaimeCernuda/gact-tui.git"

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
    git clone --quiet --branch "$ref" --depth 1 "$repo" "$dir"
  fi
}

clone_or_update "$CLIO_REPO" "clio-agent" "$CLIO_REF"
clone_or_update "$GACT_REPO" "gact-tui"   "$GACT_REF"

say "Installing CLIO Python deps (uv sync --extra api)"
( cd "$PREFIX/clio-agent" && uv sync --extra api )

say "Building gact TUI"
( cd "$PREFIX/gact-tui/tui" && go build -o "$PREFIX/gact" . )

# ---------- launcher ---------------------------------------------------
LAUNCHER="$BIN_DIR/clio"
say "Writing launcher: $LAUNCHER"
cat > "$LAUNCHER" <<'EOF'
#!/usr/bin/env bash
# CLIO launcher — boots clio-agent-gact + connects the gact TUI.
set -euo pipefail
PREFIX="${CLIO_PREFIX:-$HOME/.local/share/clio}"
PORT="${CLIO_PORT:-17800}"
LOG="${CLIO_LOG:-$PREFIX/clio-server.log}"

if ! curl -sf -m 1 "http://127.0.0.1:$PORT/v1/health" >/dev/null 2>&1; then
  echo "Starting CLIO server on :$PORT (log: $LOG)"
  (
    cd "$PREFIX/clio-agent" &&
    nohup .venv/bin/clio-agent-gact --port "$PORT" > "$LOG" 2>&1 &
  )
  sleep 4
fi

GACT_BACKEND="http://127.0.0.1:$PORT" exec "$PREFIX/gact" --no-intro "$@"
EOF
chmod +x "$LAUNCHER"

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
     The TUI will pop the LM-provider modal on first connect — pick a
     preset (Meridian / Anthropic / OpenAI / OpenRouter / LM Studio /
     Ollama), paste an API key if needed, and you're chatting.
  3. Mid-session provider swap: Ctrl+S → Settings → Model → Change provider…

EOF
