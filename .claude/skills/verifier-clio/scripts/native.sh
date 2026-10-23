#!/usr/bin/env bash
# The native desktop surface: the real Tauri app (WebKitGTK) attached to the
# running stack's agent, driven through tauri-driver under its own Xvfb display.
#
#   bash native.sh deps                       # once per container: WebKitGTK, WebDriver, tauri-driver
#   bash native.sh build                      # debug binary (first build ~15 min on 4 cores)
#   bash native.sh run STEPS [--label NAME]   # launch, run the steps, screenshot, tear down
#
# Screenshots and a report land in $VERIFY_DIR. Everything this script starts
# (Xvfb, tauri-driver, WebKitWebDriver, the app) is one process group per
# launcher and is stopped before it returns.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$ROOT/external/gact-tui/desktop"
export VERIFY_DIR="${VERIFY_DIR:-$ROOT/.cloud-run/verify/current}"
export CLIO_DESKTOP_APP="${CLIO_DESKTOP_APP:-$DESKTOP/src-tauri/target/debug/clio-desktop}"
DISPLAY_NUM="${NATIVE_DISPLAY:-:97}"
PORT="${NATIVE_DRIVER_PORT:-4444}"

case "${1:-}" in
  deps)
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev libayatana-appindicator3-dev \
      librsvg2-dev libdbus-1-dev libssl-dev libxdo-dev patchelf build-essential pkg-config \
      webkit2gtk-driver xvfb dbus-x11
    command -v tauri-driver >/dev/null || cargo install tauri-driver --locked
    ;;
  build)
    cd "$DESKTOP" && pnpm tauri:build:debug
    ls -la "$CLIO_DESKTOP_APP"
    ;;
  run)
    shift
    STEPS="${1:?steps file}"; shift
    mkdir -p "$VERIFY_DIR"
    [ -x "$CLIO_DESKTOP_APP" ] || { echo "no desktop binary at $CLIO_DESKTOP_APP; run: native.sh build"; exit 2; }
    # Attach to the stack's agent instead of launching a private backend. The
    # supervisor reads the env names its build's brand declares: the neutral
    # gact-tui build reads GACT_URL/GACT_PORT, a CLIO-branded build CLIO_*.
    export CLIO_GACT_URL="${CLIO_GACT_URL:-http://127.0.0.1:8787}" CLIO_PORT="${CLIO_PORT:-8787}"
    export GACT_URL="${GACT_URL:-$CLIO_GACT_URL}" GACT_PORT="${GACT_PORT:-$CLIO_PORT}"
    setsid Xvfb "$DISPLAY_NUM" -screen 0 1440x900x24 -nolisten tcp > "$VERIFY_DIR/native-xvfb.log" 2>&1 &
    XVFB=$!
    sleep 1
    DISPLAY="$DISPLAY_NUM" setsid tauri-driver --native-driver "$(command -v WebKitWebDriver)" --port "$PORT" \
      > "$VERIFY_DIR/native-driver.log" 2>&1 &
    DRIVER=$!
    cleanup() {
      kill -TERM -- "-$DRIVER" 2>/dev/null || true
      sleep 2; kill -KILL -- "-$DRIVER" 2>/dev/null || true
      kill -TERM -- "-$XVFB" 2>/dev/null || true
      sleep 1; kill -KILL -- "-$XVFB" 2>/dev/null || true
    }
    trap cleanup EXIT
    for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/status" >/dev/null && break; sleep 0.5; done
    TAURI_DRIVER_URL="http://127.0.0.1:$PORT" DISPLAY="$DISPLAY_NUM" node "$HERE/native-drive.mjs" --steps "$STEPS" "$@"
    ;;
  *)
    sed -n 2,12p "$0"; exit 2 ;;
esac
