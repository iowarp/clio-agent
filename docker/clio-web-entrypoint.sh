#!/bin/sh
set -eu

CLIO_HOST="${CLIO_GACT_HOST:-127.0.0.1}"
CLIO_PORT="${CLIO_GACT_PORT:-7777}"

clio-agent-gact --host "$CLIO_HOST" --port "$CLIO_PORT" &
clio_pid=$!

shutdown() {
  kill -TERM "$clio_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS "http://${CLIO_HOST}:${CLIO_PORT}/v1/capabilities" >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

nginx -g 'daemon off;' &
nginx_pid=$!
wait "$nginx_pid"
shutdown
