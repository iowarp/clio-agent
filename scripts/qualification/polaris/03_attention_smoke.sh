#!/bin/bash
# Step 03: ONE attention turn on the tensor-parallel deployment. The connector has never run
# under TP on CUDA, so nothing OPAL starts unless this turn's answer is attention-available.
# Pass = assistant reply settled with end_turn AND POST .../attention returns available=true.
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
"${KIT_PY[@]}" smoke || die "attention smoke (see $JOB_EVIDENCE/09-attention-smoke.json)"
