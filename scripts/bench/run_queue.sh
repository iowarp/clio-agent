#!/usr/bin/env bash
# Run the remaining clio-core measurements one after another (never concurrently).
#
#   scripts/bench/run_queue.sh <out-dir> <venv-2.5.0> <venv-2.4.1> <develop-src>
#
# <develop-src> is a develop checkout's src/ (CLIO as shipped today), used for the
# 2.4.1 baseline so the baseline runs CLIO code written for 2.4.1. Steps: the 2.4.1
# soak on develop code, idle CPU + metadata amplification on both versions, the
# adoption on/off bench on 2.5.0, and a soak per adoption setting. Every private home
# is removed when its step ends; SOAK_MINUTES (default 30) and ADOPT_SOAK_MINUTES
# (default 10) set the soak lengths.
set -euo pipefail

out=$(realpath -m "$1"); v250=$2; v241=$3; dev_src=$(realpath "$4")
here=$(cd "$(dirname "$0")" && pwd)
homes="$out/homes-queue"
mkdir -p "$homes"
trap 'rm -rf "$homes"' EXIT

step() { echo "== $(date -u +%T) $*" >&2; }

step "soak 2.4.1 on develop code"
PYTHONPATH="$dev_src" "$v241/bin/python" "$here/soak.py" --home "$homes/soak241dev" \
  --minutes "${SOAK_MINUTES:-30}" --out "$out/soak_2.4.1_develop.json" \
  2> "$out/soak_2.4.1_develop.log"

step "idle + metadata 2.4.1 (develop code)"
PYTHONPATH="$dev_src" "$v241/bin/python" "$here/idle_and_metadata_bench.py" \
  --home "$homes/idle241" --out "$out/idle_meta_2.4.1.json" 2> "$out/idle_meta_2.4.1.log"
step "idle + metadata 2.5.0"
"$v250/bin/python" "$here/idle_and_metadata_bench.py" --home "$homes/idle250" \
  --out "$out/idle_meta_2.5.0.json" 2> "$out/idle_meta_2.5.0.log"

step "adoption bench 2.5.0"
"$v250/bin/python" "$here/adoption_bench.py" --root "$homes/adopt" \
  --out "$out/adoption_2.5.0.json" 2> "$out/adoption_2.5.0.log"

for mode in sync sync_shm_md; do
  step "soak 2.5.0 $mode"
  env CLIO_ARC_CLIO_CORE_BLOB_CALLS=sync \
    CLIO_ARC_CTE_SHM_CACHE=$([ "$mode" = sync_shm_md ] && echo md_only || echo off) \
    "$v250/bin/python" "$here/soak.py" --home "$homes/soak_$mode" \
    --minutes "${ADOPT_SOAK_MINUTES:-10}" --out "$out/soak_2.5.0_$mode.json" \
    2> "$out/soak_2.5.0_$mode.log"
done
step "soak 2.5.0 async (same length, for the on/off comparison)"
"$v250/bin/python" "$here/soak.py" --home "$homes/soak_async" \
  --minutes "${ADOPT_SOAK_MINUTES:-10}" --out "$out/soak_2.5.0_async.json" \
  2> "$out/soak_2.5.0_async.log"
step "done"
