#!/bin/sh
set -eu

CLIO_HOST="${CLIO_GACT_HOST:-127.0.0.1}"
CLIO_PORT="${CLIO_GACT_PORT:-8100}"
export GACT_BACKEND="${GACT_BACKEND:-http://${CLIO_HOST}:${CLIO_PORT}}"
export GACT_BRAND="${GACT_BRAND:-clio}"
export TERM="${TERM:-xterm-256color}"

case "${1:-}" in
  -h|--help|help|--version|version|man|env)
    exec gact "$@"
    ;;
esac

clio_pid=""
shutdown() {
  [ -n "$clio_pid" ] && kill -TERM "$clio_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

clio-agent serve --host "$CLIO_HOST" --port "$CLIO_PORT" &
clio_pid=$!

i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS "http://${CLIO_HOST}:${CLIO_PORT}/v1/capabilities" >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 0.5
done

exec gact "$@"
