#!/usr/bin/env bash
# One launch of the installed desktop app under its own Xvfb display and
# tauri-driver, with an isolated HOME (kept between launches of one run, so a
# second launch is a returning user's launch).
#   bash desktop_launch.sh LABEL HOME_DIR [APP]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="$1"; HOME_DIR="$2"; APP="${3:-/usr/bin/clio-desktop}"; DRIVE="${DRIVE:-desktop_launch.mjs}"; shift 3 2>/dev/null || shift $#
OUT="${OUT:-$PWD/desktop-results}"
DISPLAY_NUM=:91; PORT=4455
mkdir -p "$OUT" "$HOME_DIR"
setsid Xvfb "$DISPLAY_NUM" -screen 0 1440x900x24 -nolisten tcp > "$OUT/$LABEL-xvfb.log" 2>&1 &
XVFB=$!
sleep 1
# The app inherits this environment through tauri-driver and WebKitWebDriver.
env -i PATH="/usr/local/bin:/usr/bin:/bin:/root/.cargo/bin" HOME="$HOME_DIR" DISPLAY="$DISPLAY_NUM" \
  LANG=C.UTF-8 ${EXTRA_ENV:-} HTTPS_PROXY="${HTTPS_PROXY:-}" https_proxy="${HTTPS_PROXY:-}" NO_PROXY="${NO_PROXY:-}" \
  no_proxy="${NO_PROXY:-}" SSL_CERT_FILE="${SSL_CERT_FILE:-}" REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-}" \
  CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-}" NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-}" \
  setsid dbus-run-session -- bash -c 'printf "test" | gnome-keyring-daemon --unlock --components=secrets >/dev/null; exec "$@"' keyring \
  tauri-driver --native-driver "$(command -v WebKitWebDriver)" --port "$PORT" \
  > "$OUT/$LABEL-driver.log" 2>&1 &
DRIVER=$!
cleanup() {
  # A test that fails before quitting leaves the app's backend running when the
  # driver kills the window; stop this run's server gracefully first.
  for p in /proc/[0-9]*; do
    if grep -qz "^HOME=$HOME_DIR\$" "$p/environ" 2>/dev/null && tr '\0' ' ' < "$p/cmdline" | grep -q 'clio_agent.gact'; then
      echo "cleanup: stopping server ${p#/proc/}"; kill -TERM "${p#/proc/}"
    fi
  done
  sleep 8
  kill -TERM -- "-$DRIVER" 2>/dev/null; sleep 2; kill -KILL -- "-$DRIVER" 2>/dev/null
  kill -TERM -- "-$XVFB" 2>/dev/null; sleep 1; kill -KILL -- "-$XVFB" 2>/dev/null
}
trap cleanup EXIT
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/status" >/dev/null && break; sleep 0.5; done
TAURI_DRIVER_URL="http://127.0.0.1:$PORT" node "$HERE/$DRIVE" \
  --app "$APP" --home "$HOME_DIR" --out "$OUT" --label "$LABEL" "$@"
