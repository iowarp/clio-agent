#!/bin/bash
# Step 02: CLIO deploys its services on this node, in order, through its REST API.
#   CLIO serve (loopback) -> storage locations -> model (hub snapshot or CLIO download)
#   -> Flowcept (FLOWCEPT_VARIANT, native by default) -> vLLM native-cuda-attention on
#   Granite 4.2 (MODEL_STAGE: 8B on 1 GPU, or 30B with VLLM_TP=2) -> Flowcept as the attention collector
#   (verify + use + CLIO restart) -> "Use in Models" (bind the managed vLLM).
# Resumable: every step reuses what is already running and matching.
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
[ "$DRY_RUN" = 1 ] || [ -s "$CONNECTOR_SOURCE_FILE" ] || die "no connector build ($CONNECTOR_SOURCE_FILE); run 01_stage_up.sh"
[ "$FLOWCEPT_VARIANT" = managed ] && load_modules   # Apptainer databases
log "CLIO connector source: ${CLIO_VLLM_ATTN_CONNECTOR:-(CLIO pin; will not build)}"
# SPOTTER first: installing it on this host replaces a record left by another node, whose
# stale GPU pin would otherwise block the agent vLLM (gpu_share_exceeded).
for step in serve-start storage model flowcept spotter-vllm vllm provenance bind spotter-bind; do
  log "== $step"
  "${KIT_PY[@]}" "$step" || die "$step (receipts and op logs in $JOB_EVIDENCE)"
done
"${KIT_PY[@]}" status
