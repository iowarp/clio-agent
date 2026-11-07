#!/bin/bash
# Step 03b: ONE watched turn; SPOTTER must finish a real review (tools, end_turn) on its chosen
# model (SPOTTER_MODE=dedicated: vllm@spotter). Nothing OPAL starts unless this passes.
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
"${KIT_PY[@]}" spotter-smoke
python3 - "$JOB_EVIDENCE/09b-spotter-smoke.json" <<'PY' || die "SPOTTER smoke (see $JOB_EVIDENCE/09b-spotter-smoke.json)"
import json, sys
d = json.load(open(sys.argv[1]))
print("SPOTTER smoke:", d["status"], "; ".join(d.get("problems") or []))
sys.exit(0 if d["status"] in ("ok", "dry_run") else 1)
PY
