#!/bin/sh
set -eu

# The backend and nginx run as the clio user. Started as root (the default),
# this hands clio the state volumes, which images before this one wrote as
# root, and then runs the rest of this script as clio.
if [ "$(id -u)" = 0 ]; then
  for dir in /home/clio/.config/clio-agent /home/clio/.local/share/clio-agent \
             /home/clio/.local/state/clio-agent /home/clio/.cache/clio-agent; do
    mkdir -p "$dir"
    if [ "$(stat -c %U "$dir")" != clio ]; then
      chown -R clio:clio "$dir"
    fi
  done
  exec setpriv --reuid clio --regid clio --init-groups env HOME=/home/clio "$0" "$@"
fi

CLIO_HOST="${CLIO_GACT_HOST:-127.0.0.1}"
CLIO_PORT="${CLIO_GACT_PORT:-8100}"

# nginx reaches the backend over loopback as the backend's own user, so the
# backend cannot tell which client a proxied request came from. With a bearer
# token configured, every request must therefore carry it. CLIO_WEB_BIND is the
# host address docker compose publishes the UI on; beyond loopback, a token is
# required.
case "${CLIO_WEB_BIND:-127.0.0.1}" in
  127.*|localhost|::1|"[::1]") ;;
  *)
    if [ -z "${CLIO_GACT_BEARER_TOKEN:-}" ]; then
      echo "clio-web: CLIO_WEB_BIND=${CLIO_WEB_BIND} publishes CLIO beyond this computer," \
           "which needs a bearer token. Set CLIO_GACT_BEARER_TOKEN and connect with it" \
           "(Connect by address, token field)." >&2
      exit 1
    fi
    ;;
esac
if [ -n "${CLIO_GACT_BEARER_TOKEN:-}" ]; then
  export CLIO_GACT_LOOPBACK_REQUIRES_TOKEN=1
fi

clio-agent serve --host "$CLIO_HOST" --port "$CLIO_PORT" &
clio_pid=$!

shutdown() {
  kill -TERM "$clio_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

i=0
while [ "$i" -lt 60 ]; do
  # Any HTTP answer means the backend is up (it may require the token).
  if curl -s -o /dev/null "http://${CLIO_HOST}:${CLIO_PORT}/v1/capabilities"; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

nginx -e stderr -g 'daemon off;' &
nginx_pid=$!
wait "$nginx_pid"
shutdown
