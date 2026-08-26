#!/usr/bin/env bash
# Provenance-qualification serve. ALL deployment parameters come from the
# environment (source your deployment.env first) — no host-specific paths.
set -euo pipefail

: "${CLIO_PQ_REPO:?path to the clio-agent checkout}"
: "${CLIO_PQ_PYTHON:?python interpreter with clio-agent deps (incl. flowcept extra when selected)}"
: "${CLIO_PQ_WORKSPACE:?serve cwd / qualification workspace root}"
: "${CLIO_PQ_PORT:?serve port}"
: "${CLIO_PQ_CMF_PYTHON:?isolated CMF-compatible python (3.9 + cmflib)}"
: "${CLIO_PQ_CMF_METADATA_PATH:?MLMD sqlite path (use a FRESH file per run)}"
: "${CLIO_PQ_CMF_ARTIFACT_ROOT:?DVC-local CAS root}"
: "${CLIO_PQ_CMF_SERVER_URL:?CMF server base URL}"
: "${CLIO_PQ_PIPELINE:?CMF pipeline name for this qualification}"

mkdir -p "$CLIO_PQ_WORKSPACE"
cd "$CLIO_PQ_WORKSPACE"
exec env \
  PYTHONPATH="$CLIO_PQ_REPO/src" \
  CLIO_ARC_STORE="${CLIO_PQ_ARC_STORE:-local}" \
  CLIO_LM_PROVIDER="${CLIO_PQ_LM_PROVIDER:-claude_code}" \
  CLIO_LM_MODEL="${CLIO_PQ_LM_MODEL:-sonnet}" \
  CLIO_PROVENANCE_PROVIDERS="${CLIO_PQ_PROVIDERS:-jsonl}" \
  FLOWCEPT_SETTINGS_PATH="${CLIO_PQ_FLOWCEPT_SETTINGS:-}" \
  CLIO_FLOWCEPT_CAMPAIGN_ID="$CLIO_PQ_PIPELINE" \
  CLIO_FLOWCEPT_CAMPAIGN_SCOPE=workspace \
  CLIO_FLOWCEPT_WORKFLOW_SCOPE=session \
  CLIO_FLOWCEPT_PRIVACY=metadata \
  CLIO_ARTIFACT_PROVENANCE_PROVIDER=cmf \
  CLIO_CMF_PYTHON="$CLIO_PQ_CMF_PYTHON" \
  CLIO_CMF_METADATA_PATH="$CLIO_PQ_CMF_METADATA_PATH" \
  CLIO_CMF_ARTIFACT_ROOT="$CLIO_PQ_CMF_ARTIFACT_ROOT" \
  CLIO_CMF_ARTIFACT_STORE=local \
  CLIO_CMF_PIPELINE_NAME="$CLIO_PQ_PIPELINE" \
  CLIO_CMF_SERVER_URL="$CLIO_PQ_CMF_SERVER_URL" \
  "$CLIO_PQ_PYTHON" "$CLIO_PQ_REPO/src/clio_agent/ui/cli.py" \
  serve --host 127.0.0.1 --port "$CLIO_PQ_PORT"
