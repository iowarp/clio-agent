#!/usr/bin/env bash
# Benchmark iowarp-core versions on this machine, one at a time (never concurrently).
#
#   scripts/bench/compare_versions.sh <out-dir> <venv> [<venv> ...]
#
# Each venv must have clio-agent (editable, this checkout) and one iowarp-core version,
# e.g. the project .venv (2.5.0) and a copy with `uv pip install iowarp-core==2.4.1`.
# SOAK_MINUTES (default 30) sets the soak length; SKIP_SOAK=1 skips it.
# Every daemon runs under a private CLIO_AGENT_HOME in <out-dir>/homes and is stopped by
# the script that started it; the homes are removed at the end.
set -euo pipefail

out=$(realpath -m "$1"); shift
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out/homes"
for venv in "$@"; do
  py="$venv/bin/python"
  ver=$("$py" -c 'import iowarp_core; print(iowarp_core.__version__)')
  echo "== iowarp-core $ver ($venv)" >&2
  "$py" "$here/daemon_lifecycle_bench.py" --root "$out/homes/life-$ver" --reps 5 \
    --out "$out/lifecycle_$ver.json" 2> "$out/lifecycle_$ver.log"
  "$py" "$here/arc_store_bench.py" --home "$out/homes/arc-$ver" \
    --out "$out/arc_store_$ver.json" 2> "$out/arc_store_$ver.log"
  "$py" "$here/native_bench.py" --home "$out/homes/native-$ver" \
    --out "$out/native_$ver.json" 2> "$out/native_$ver.log"
  if [ "${SKIP_SOAK:-0}" != "1" ]; then
    "$py" "$here/soak.py" --home "$out/homes/soak-$ver" --minutes "${SOAK_MINUTES:-30}" \
      --out "$out/soak_$ver.json" 2> "$out/soak_$ver.log"
  fi
done
rm -rf "$out/homes"
