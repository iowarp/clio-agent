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

if [[ "${CLIO_PQ_ARC_STORE:-cte}" != "cte" ]]; then
  echo "qualification requires CLIO_PQ_ARC_STORE=cte; LocalFS is not acceptance evidence" >&2
  exit 2
fi

# Fail before binding the port when the selected interpreter is stale or
# incomplete. These imports cover the exact pre-executor seams that construct
# the ARC, relay surfaces, provider, and pinned GACT schema.
PYTHONPATH="$CLIO_PQ_REPO/src" "$CLIO_PQ_PYTHON" - <<'PY'
from importlib.metadata import version

import iowarp_core  # noqa: F401
import litellm  # noqa: F401
import openai_codex  # noqa: F401
import psutil  # noqa: F401
import claude_agent_sdk  # noqa: F401
from clio_schemas import A2UIClientActionMessage  # noqa: F401

expected = {
    "claude-agent-sdk": "0.2.128",
    "clio-schemas": "0.2.3",
    "litellm": "1.91.3",
    "openai-codex": "0.147.0",
}
actual = {name: version(name) for name in expected}
if actual != expected:
    raise SystemExit(f"qualification dependency drift: expected={expected}, actual={actual}")
PY

mkdir -p "$CLIO_PQ_WORKSPACE"
cd "$CLIO_PQ_WORKSPACE"
exec env \
  PYTHONPATH="$CLIO_PQ_REPO/src" \
  CLIO_ARC_STORE="${CLIO_PQ_ARC_STORE:-cte}" \
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
