#!/bin/bash
# Step 04: stage the OPAL tabular core (139 files, 1.24 GB) from the public Drive folder by
# Drive id, sha256-verified against the Delta/Aurora manifest, then made read-only. Outside
# every agent workspace; CLIO sees it only as a read-only source.
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
"${KIT_PY[@]}" stage-opal "${OPAL_MANIFEST:-$KIT_DIR/opal-staging-manifest.json}" "$OPAL_EXPORT" \
  || die "OPAL staging (see $JOB_EVIDENCE/11-opal-data.json); rerun to resume"
