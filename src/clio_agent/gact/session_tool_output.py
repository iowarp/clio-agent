"""Delete a deleted session's spilled tool output (#1487).

The shell tool spills oversize output to
``<workspace root>/.clio/tool-output/<session_id>/``
(:mod:`clio_agent.tools.servers.shell_spill_store`). The session owns those
files, so the session-delete route calls :func:`delete_session_tool_output`
next to the other per-session cleanups. Retention is tied to the session's
life, never a timer or a TTL.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.tools.servers.shell_spill_store import delete_session_spills

logger = logging.getLogger(__name__)

#: Typed reason when a deleted session's workspace root cannot be resolved.
NO_WORKSPACE_ROOT_REASON = "tool_output_cleanup_no_workspace_root"


def delete_session_tool_output(app: Any, workspace_id: str, session_id: str) -> int:
    """Remove ``session_id``'s spill folder from its workspace; return files removed.

    Args:
        app: The GACT app (its ``state.workspaces`` resolves the workspace root).
        workspace_id: The deleted session's workspace id.
        session_id: The deleted session's id.

    Returns:
        The number of spilled files removed (0 when there were none).
    """

    workspaces = getattr(app.state, "workspaces", None)
    workspace = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
    root = str(getattr(workspace, "root_path", "") or "")
    if not root:
        logger.info(
            "tool output cleanup skipped reason=%s session=%s workspace=%s",
            NO_WORKSPACE_ROOT_REASON,
            session_id,
            workspace_id,
        )
        return 0
    return delete_session_spills(root, session_id)


__all__ = ["NO_WORKSPACE_ROOT_REASON", "delete_session_tool_output"]
