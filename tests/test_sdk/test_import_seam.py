"""Import-seam guard: the SDK is a pure client (#799 phase 1).

Importing ``clio_agent.sdk`` must not load the gact server package,
FastAPI, or the agent runtime — the whole point of the SDK is talking
to clio from code without dragging the server in.
"""

from __future__ import annotations

import json
import subprocess
import sys


def test_sdk_import_does_not_load_server_code() -> None:
    probe = (
        "import json, sys; import clio_agent.sdk; "
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.startswith('clio_agent.gact') or m in ('fastapi', 'uvicorn', 'dspy'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    leaked = json.loads(result.stdout.strip().splitlines()[-1])
    assert leaked == [], f"clio_agent.sdk must stay a pure client; it imported: {leaked}"
