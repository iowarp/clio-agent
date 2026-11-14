"""A fresh demo workspace and its conversation, ready for the walkthrough.

    python scripts/demo/helene/setup_ws.py ROOT TITLE [WORKSPACE_NAME]

ROOT holds an ``era5/`` folder with the files ``fetch_era5.py`` writes. The conversation
runs the ``clio/clio-data`` agent with the ``hdf5-ptc`` plugin in place of ``hdf5``.
Unless ``NO_S3=1``, the ERA5 September 2024 pressure-level prefix is also linked as an
anonymous S3 source, so the agent can read by range instead of from local files. Prints
the workspace id and the session id for ``WS`` and ``SID``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api import call, sources, workspace  # noqa: E402

S3_SOURCE = {
    "provider": "s3",
    "root": "nsf-ncar-era5/e5.oper.an.pl/202409",
    "label": "ERA5 September 2024 (pressure levels)",
    "mode": "read_only",
    "configuration": {"s3_region": "us-west-2", "s3_access": "anonymous"},
}


def _create_source(workspace_id: str) -> dict:
    # The session's tool fleet is busy while it starts; data access waits for it (409).
    for _ in range(40):
        try:
            return call("POST", f"/v1/workspaces/{workspace_id}/sources", S3_SOURCE)
        except RuntimeError as exc:
            if "409" not in str(exc):
                raise
            time.sleep(5)
    raise SystemExit("data access stayed busy")


def main(root: str, title: str, name: str) -> None:
    workspace_id = workspace(name, root)["id"]
    listed = call("GET", f"/v1/sessions?workspace_id={workspace_id}")["sessions"]
    same = [row for row in listed if row.get("title") == title]
    session = (
        same[0]
        if same
        else call(
            "POST",
            "/v1/sessions",
            {
                "workspace_id": workspace_id,
                "title": title,
                "approval_mode": "bypass",
                "metadata": {
                    "agent_ref": "clio/clio-data",
                    "agent_delta": {"add": ["clio/hdf5-ptc"], "remove": ["clio/hdf5"]},
                },
            },
        )
    )
    session_id = session["id"]
    if os.environ.get("NO_S3") == "1":
        print(workspace_id, session_id, "local only")
        return
    source = next((row for row in sources(workspace_id) if row.get("provider") == "s3"), None)
    source = source or _create_source(workspace_id)
    if not source.get("linked"):
        call(
            "POST",
            f"/v1/workspaces/{workspace_id}/sources/{source['id']}/link",
            {"session_id": session_id},
        )
    for _ in range(100):
        source = next(row for row in sources(workspace_id) if row["id"] == source["id"])
        if source.get("linked"):
            break
        time.sleep(3)
    print(workspace_id, session_id, "linked" if source.get("linked") else "not linked")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else "Helene")
