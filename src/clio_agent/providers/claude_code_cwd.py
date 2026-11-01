"""The folder the Claude Code CLI runs in for a CLIO session.

The bare-model transport turns off Claude Code's tools, settings and hooks, but the
CLI still tells the model its working directory and whether that folder is a git
repository. Left at the server's own folder, the model was told it worked in the
CLIO checkout while CLIO's prompt named the session's workspace, and agents searched
the server's tree instead of their workspace. The CLI therefore runs in the session's
workspace root, the folder CLIO's prompt names; with no session or workspace it runs
in an empty CLIO folder, and the trace says why.
"""

from __future__ import annotations

from pathlib import Path

from clio_agent.runtime import trace

__all__ = ["NO_WORKSPACE_REASON", "session_cwd"]

#: Trace reason when a call has no bound workspace and runs in the empty folder.
NO_WORKSPACE_REASON = "claude_code_cwd_no_workspace"


def _workspace_root(session_id: str) -> Path | None:
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents import resolution  # noqa: PLC0415

    app = _ctx.active_app()
    if app is None or not session_id:
        return None
    root = resolution._runtime_workspace_catalog_cwd(app, session_id=session_id)
    return Path(root) if root is not None and Path(root).is_dir() else None


def _empty_folder() -> Path:
    from clio_agent import paths  # noqa: PLC0415

    folder = paths.user_state_dir() / "claude-code" / "no-workspace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def session_cwd(session_id: str) -> str:
    """The CLI's working folder for ``session_id``: its workspace root, else an empty one."""
    try:
        root = _workspace_root(session_id)
    except Exception as exc:  # noqa: BLE001 - resolution failure falls to the empty folder
        trace.event("CLAUDE-CODE", "%s session=%s error=%s", NO_WORKSPACE_REASON, session_id, exc)
        root = None
    if root is not None:
        return str(root)
    folder = _empty_folder()
    trace.event("CLAUDE-CODE", "%s session=%s cwd=%s", NO_WORKSPACE_REASON, session_id, folder)
    return str(folder)
