"""Agent downloads through the existing approved, storage-owned transfer path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.artifacts.observer_bridge import observer_call_id
from clio_agent.gact.permission_gate import _invoke_permission_gate
from clio_agent.gact.task_tools import task_presentation
from clio_agent.tools.task_call_context import require_admission


def connected_data_download(
    source_id: str, selected_paths: list[str] | None = None, description: str | None = None
) -> dict[str, Any]:
    """Start an approved source download and immediately return its durable task handle."""
    app, sid = context.active_app(), context.active_session_id()
    if app is None or not sid:
        raise ValueError("Download requires an active workspace conversation")
    require_admission(app, sid)
    session = app.state.sessions.get(sid)
    if session is None:
        raise ValueError("Download conversation is unavailable")
    workspace = app.state.workspaces.get(session.workspace_id)
    if workspace is None:
        raise ValueError("Download workspace is unavailable")
    if selected_paths is not None:
        from clio_agent.gact.storage.models import FileEntry

        if not selected_paths or len(selected_paths) > 1000:
            raise ValueError("Select between 1 and 1000 relative paths")
        for selected in selected_paths:
            FileEntry(path=selected, kind="file")
    service = app.state.connected_storage
    source = service.get(session.workspace_id, source_id)
    gate = getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()
    arguments = {"source_id": source_id, "selected_paths": selected_paths}
    if (
        _invoke_permission_gate(gate, "connected_data_download", arguments, {"kind": "native"})
        != "allow"
    ):
        raise PermissionError("Source download was not approved")
    invocation = observer_call_id()

    async def submit() -> dict[str, Any]:
        from clio_agent.gact.storage.task_adapter import storage_handle

        require_admission(app, sid)
        operation = service.start_transfer(source, Path(workspace.root_path), selected_paths)
        return storage_handle(
            app, sid, operation, description or f"Download {source.source.label}", invocation
        )

    return asyncio.run_coroutine_threadsafe(submit(), app.state.mcp_app_loop).result()


def build_connected_data_download_tool() -> Any:
    """Declare downloads using the same permission and transcript boundaries as other natives."""
    return native_tool(
        connected_data_download,
        name="connected_data_download",
        desc=connected_data_download.__doc__,
        args={
            "source_id": {"type": "string"},
            "selected_paths": {"type": ["array", "null"], "items": {"type": "string"}},
            "description": {"type": ["string", "null"]},
        },
        presentation=task_presentation,
        title="Download source",
        domain="resources",
    )
