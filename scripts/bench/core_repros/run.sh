#!/usr/bin/env bash
# Minimal clio-core 2.5.0 reproductions for docs/design/clio-core-2.5-feedback.md.
#
#   scripts/bench/core_repros/run.sh <venv> <work-dir> <case>...
#
# Cases: full_wal (needs root: a tmpfs), volatile_wipe, stripe, viz. Each case uses a
# fresh directory under <work-dir>, a private port and memfd dir, plain clio-core (no
# CLIO code), and stops its daemon and removes its files when done.
set -uo pipefail
venv=$(realpath "$1"); work=$(realpath -m "$2"); shift 2
here=$(cd "$(dirname "$0")" && pwd)
export PATH="$venv/bin:$PATH" CLIO_BIND_ADDR=127.0.0.1
py="$venv/bin/python"

setup() {  # setup <name> <port> [capacity]
  export H="$work/$1" P=$2 USER=lv$2 CAP=${3:-2GB}
  export CLIO_MEMFD_DIR=/tmp/clv$P CLIO_SERVER_CONF=$H/cte.yaml CLIO_VIZ_ENABLE=${VIZ:-0}
  mkdir -p "$H/conf" "$CLIO_MEMFD_DIR" && chmod 700 "$CLIO_MEMFD_DIR"
  cat > "$CLIO_SERVER_CONF" <<Y
networking: {port: $P}
runtime: {num_threads: 4, conf_dir: "$H/conf"}
compose:
  - {mod_name: clio_bdev, pool_name: "ram::chi_default_bdev", pool_query: local, pool_id: "301.0", bdev_type: ram, capacity: "1GB"}
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    restart: true
    storage:
      - {path: "$H/storage.bin", bdev_type: file, capacity_limit: "$CAP", score: 1.0, persistence_level: temporary}
    dpe: {dpe_type: max_bw}
    performance: {metadata_log_path: "$H/metadata.log", transaction_log_capacity: "32MB"}
Y
}
start() { setsid clio-run start > "$H/daemon.$(date +%s%N).log" 2>&1 < /dev/null & sleep 4; }
stop() { clio-run stop > /dev/null 2>&1; sleep 1; }
teardown() { stop; rm -rf "$CLIO_MEMFD_DIR"; }

case_full_wal() {
  mkdir -p "$work/full_wal" && mount -t tmpfs -o size=6m tmpfs "$work/full_wal" || return 1
  setup full_wal 19511 4MB
  start
  dd if=/dev/zero of="$H/filler" bs=4k 2> /dev/null
  echo "puts on a full metadata-log fs: $($py "$here/put.py" 150 16384)"
  grep -ciE "error|no space|ENOSPC" "$H"/daemon.*.log | sed 's/^/daemon log lines naming an error: /'
  stop; cp "$H/ok.json" "$work/full_wal.ok.json"; rm -f "$H/filler"; start
  echo "after restart: $($py "$here/check.py")"
  teardown; umount "$work/full_wal"
}

case_volatile_wipe() {
  setup volatile_wipe 19521
  start; $py "$here/put.py" 50; stop
  echo "metadata log files before: $(ls "$H" | grep -c '^metadata.log')"
  sed -i 's/, persistence_level: temporary//' "$CLIO_SERVER_CONF"
  start; stop
  echo "metadata log files after one start without the label: $(ls "$H" | grep -c '^metadata.log')"
  grep -h "discard" "$H"/daemon.*.log | head -2
  setup volatile_wipe 19521  # the label back
  start; echo "after restoring the label: $($py "$here/check.py")"
  teardown
}

case_stripe() {
  setup stripe 19531
  start; $py "$here/put.py" 100 65536; stop
  CLIO_BDEV_STRIPE_FILES=2 start
  echo "reopened with CLIO_BDEV_STRIPE_FILES=2: $($py "$here/check.py")"
  stop; start
  echo "reopened without it: $($py "$here/check.py")"
  teardown
}

case_viz() {
  VIZ=1 setup viz 19541
  start
  curl -s http://127.0.0.1:8080/api/pools | head -c 300; echo
  echo "cross-origin form POST: $(curl -s -X POST -H 'Origin: http://evil.example' \
    -H 'Content-Type: application/x-www-form-urlencoded' --data-binary 'x=1' \
    http://127.0.0.1:8080/api/pools/301.0/destroy | head -c 300)"
  echo "pools after: $(curl -s http://127.0.0.1:8080/api/pools | grep -o '301.0' | head -1)"
  teardown
}

for c in "$@"; do echo "=== $c"; "case_$c"; done
rm -rf "$work"
