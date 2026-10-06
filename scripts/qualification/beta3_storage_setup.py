"""Seed a recorded native setup call in an isolated API; no inference is claimed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4


def seed_setup_session(app: Any, root: Path) -> None:
    """Exercise the real status tool and retain its presentation for browser review."""
    from clio_agent.gact import context
    from clio_agent.gact.session_store import _extend_session_messages
    from clio_agent.gact.storage.models import now
    from clio_agent.gact.storage.setup_tool import connected_data_status, setup_presentation
    from clio_agent.gact.types import Message, Part

    workspace_path = root / "storage-review-workspace"
    workspace_path.mkdir(exist_ok=True)
    input_path = root / "storage-review-inputs"
    input_path.mkdir(exist_ok=True)
    (input_path / "observations.csv").write_text("sample,signal\nclean,1\ncontrol,2\n")
    workspace = app.state.workspaces.create(
        name="Connected data review", root_path=str(workspace_path)
    )
    session = app.state.sessions.create(workspace_id=workspace.id, title="Private setup handoff")
    app_token = context.set_app(app)
    session_token = context.set_session_id(session.id)
    try:
        result = connected_data_status()
    finally:
        context.reset(session_token)
        context.reset(app_token)
    timestamp = now()
    call_id = "qualification_" + uuid4().hex
    _extend_session_messages(
        app,
        session.id,
        [
            Message(
                id="msg_" + uuid4().hex,
                session_id=session.id,
                role="assistant",
                created_at=timestamp,
                updated_at=timestamp,
                parts=[
                    Part(
                        id="part_" + uuid4().hex,
                        type="text",
                        text="Recorded tool qualification: choose inputs through private setup. No inference was run.",
                    ),
                    Part(
                        id="part_" + uuid4().hex,
                        type="tool_call",
                        call_id=call_id,
                        tool_name="connected_data_status",
                        tool_title="Connected data",
                        input={},
                    ),
                    Part(
                        id="part_" + uuid4().hex,
                        type="tool_result",
                        call_id=call_id,
                        tool_name="connected_data_status",
                        text=json.dumps(result),
                        presentation=setup_presentation({}, result, None),
                    ),
                ],
            )
        ],
    )
    print(
        json.dumps(
            {"workspace_id": workspace.id, "session_id": session.id, "input_path": str(input_path)}
        ),
        flush=True,
    )
