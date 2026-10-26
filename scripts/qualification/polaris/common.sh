#!/bin/bash
# Shared setup for the CLIO Polaris kit. Source it; do not run it.
#   KIT_DIR, settings (polaris.env), derived paths, proxy, PATH, cache locations,
#   DRY_RUN handling and the receipt writer.
# shellcheck disable=SC2034  # variables here are consumed by the scripts that source this file

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN="${DRY_RUN:-0}"

USER="${USER:-$(id -un)}"; export USER
CLIO_POLARIS_ENV="${CLIO_POLARIS_ENV:-$HOME/.clio-polaris.env}"
if [ -f "$CLIO_POLARIS_ENV" ]; then
  # shellcheck source=/dev/null
  . "$CLIO_POLARIS_ENV"
elif [ "$DRY_RUN" = 1 ]; then
  echo "[kit] DRY_RUN: $CLIO_POLARIS_ENV missing; using the template defaults" >&2
  # shellcheck source=polaris.env.template
  . "$KIT_DIR/polaris.env.template"
else
  echo "[kit] missing $CLIO_POLARIS_ENV; copy $KIT_DIR/polaris.env.template there and set PBS_ACCOUNT" >&2
  exit 2
fi
# Unset values fall back to the template (a partial env file is fine).
# shellcheck source=polaris.env.template
. "$KIT_DIR/polaris.env.template"

if [ "$PBS_ACCOUNT" = CHANGE_ME ] && [ "$DRY_RUN" != 1 ]; then
  echo "[kit] set PBS_ACCOUNT in $CLIO_POLARIS_ENV" >&2
  exit 2
fi

HOST="$(hostname -s)"
JOB_TAG="${PBS_JOBID:-nojob-$HOST}"
JOB_TAG="${JOB_TAG%%.*}"
SRC="$ROOT/src"
CLIO_SRC="$SRC/clio-agent"
GACT_SRC="$CLIO_SRC/external/gact-tui"
TOOLS="$ROOT/tools"
STATE="$ROOT/state"
BUILD="$ROOT/build"
LOGS="$ROOT/logs"
EVIDENCE="$ROOT/evidence"
JOB_EVIDENCE="$EVIDENCE/job-$JOB_TAG"
RUN_DIR="$ROOT/run/$HOST"
CLIO_ENDPOINT="http://127.0.0.1:$CLIO_PORT"

# Node-local scratch: vLLM/Triton/compile caches and container extraction stay off Lustre.
if [ -d /local/scratch ] && [ -w /local/scratch ]; then NODE_SCRATCH=/local/scratch; else NODE_SCRATCH="${TMPDIR:-/tmp}"; fi
NODE_TMP="$NODE_SCRATCH/clio-$USER-$JOB_TAG"

# Compute nodes reach the internet only through the ALCF proxy; loopback must never use it.
if [ -n "${PBS_JOBID:-}" ] || [ "${FORCE_PROXY:-0}" = 1 ]; then
  export http_proxy="$ALCF_PROXY" https_proxy="$ALCF_PROXY" HTTP_PROXY="$ALCF_PROXY" HTTPS_PROXY="$ALCF_PROXY"
  export ftp_proxy="$ALCF_PROXY"
fi
export no_proxy="localhost,127.0.0.1,::1,${no_proxy:-}" NO_PROXY="localhost,127.0.0.1,::1,${NO_PROXY:-}"

export PATH="$TOOLS/bin:$TOOLS/node/bin:$TOOLS/pnpm/node_modules/.bin:$PATH"
export UV_CACHE_DIR="$ROOT/cache/uv" UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_TOOL_DIR="$TOOLS/uv-tools" UV_TOOL_BIN_DIR="$TOOLS/uv-tools/bin"
export npm_config_cache="$ROOT/cache/npm" npm_config_store_dir="$ROOT/cache/pnpm-store"
export HF_HOME HF_HUB_CACHE
export HF_XET_CACHE="$HF_HOME/xet"
unset TRANSFORMERS_CACHE HUGGINGFACE_HUB_CACHE
export XDG_CACHE_HOME="$NODE_TMP/xdg" VLLM_CACHE_ROOT="$NODE_TMP/vllm-cache"
export TRITON_CACHE_DIR="$NODE_TMP/triton" TORCHINDUCTOR_CACHE_DIR="$NODE_TMP/inductor"
export APPTAINER_CACHEDIR="$ROOT/cache/apptainer" APPTAINER_TMPDIR="$NODE_TMP/apptainer-tmp"

# CLIO itself: all state on project storage, clio-core runtime per host.
export CLIO_AGENT_HOME="$STATE"
export CLIO_RUNTIME_STATE_DIR="$STATE/runtime-hosts/$HOST"
export CLIO_WEB_DIR="$BUILD/clio-web"
export CLIO_SEARCH_AUTO_INSTALL
if [ -n "$CUDA_COMPAT_DIR" ]; then export LD_LIBRARY_PATH="$CUDA_COMPAT_DIR:${LD_LIBRARY_PATH:-}"; fi
CONNECTOR_SOURCE_FILE="$BUILD/connector/CONNECTOR_SOURCE.txt"
if [ -z "${CLIO_VLLM_ATTN_CONNECTOR:-}" ] && [ -s "$CONNECTOR_SOURCE_FILE" ]; then
  CLIO_VLLM_ATTN_CONNECTOR="$(cat "$CONNECTOR_SOURCE_FILE")"
  export CLIO_VLLM_ATTN_CONNECTOR
fi

export KIT_DIR DRY_RUN ROOT HOST JOB_TAG CLIO_SRC STATE BUILD LOGS EVIDENCE JOB_EVIDENCE RUN_DIR
export CLIO_PORT CLIO_ENDPOINT NODE_TMP MODEL_REPO MODEL_REVISION MODEL_PATH
export FLOWCEPT_VARIANT FLOWCEPT_PORT FLOWCEPT_REDIS_PORT FLOWCEPT_MONGO_PORT
export VLLM_VARIANT VLLM_TP VLLM_GPU_UTIL VLLM_MAX_NUM_SEQS VLLM_MAX_MODEL_LEN
export SPOTTER_MODE SPOTTER_GPU SPOTTER_GPU_UTIL SPOTTER_MAX_NUM_SEQS SPOTTER_MAX_MODEL_LEN SPOTTER_VLLM_VARIANT
export OPAL_AGENT OPAL_CHAIN_AGENT OPAL_CHAIN_ID OPAL_CHAINS_CSV OPAL_EXPORT OPAL_TURN_TIMEOUT_S
export OPAL_CONDITIONS OPERATOR_DIR

if [ "$DRY_RUN" != 1 ]; then
  mkdir -p "$SRC" "$TOOLS/bin" "$STATE" "$BUILD" "$LOGS" "$JOB_EVIDENCE" "$RUN_DIR" "$NODE_TMP" \
    "$UV_CACHE_DIR" "$HF_HUB_CACHE" "$XDG_CACHE_HOME" "$APPTAINER_TMPDIR" "$ROOT/data"
  chmod 700 "$NODE_TMP" 2>/dev/null || true
fi

# The kit's Python helper runs on CLIO's interpreter once it exists, else the system python3
# (SLES python3 is 3.6; kit.py stays compatible with it).
if [ -x "$CLIO_SRC/.venv/bin/python" ]; then PY="$CLIO_SRC/.venv/bin/python"; else PY=python3; fi
KIT_PY=("$PY" -I "$KIT_DIR/kit.py")

log() { echo "[kit $(date +%H:%M:%S)] $*" >&2; }
die() { log "FAILED: $*"; exit 1; }

# run CMD...: execute, or print it under DRY_RUN=1.
run() {
  if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN + $*"; return 0; fi
  "$@"
}

# receipt STEP STATUS [key=value ...]: JSON receipt in the job evidence directory.
receipt() { "${KIT_PY[@]}" receipt "$@"; }

load_modules() {
  command -v module >/dev/null 2>&1 || { log "no 'module' command on this host"; return 0; }
  local line
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # shellcheck disable=SC2086  # a module line is several words by design
    module $line >/dev/null 2>&1 || log "module $line: failed (continuing; only the Apptainer fallback needs it)"
  done <<< "$POLARIS_MODULES"
}
