"""What an agent viewed stays viewable: a snapshot of the bytes at view time.

``view_image`` / ``view_pdf`` return a small descriptor, not the bytes, and the
agent loop rebuilds each model request from the conversation history, so the
media is read again at every step and in every later turn. Reading the live
workspace file would show the agent a different image than it looked at (or fail)
once the file changes -- e.g. after the agent regenerates a plot. The viewed bytes
are therefore snapshotted, content-addressed, into the session's tool-output folder
(:mod:`clio_agent.tools.servers.shell_spill_store`, removed with the session), and
the history reads the snapshot.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from clio_agent.platform_paths import win_extended_path

__all__ = ["ViewedMediaUnavailable", "read_snapshot", "snapshot"]
_MANAGED_REFERENCE = "clio-tool-output:"


class ViewedMediaUnavailable(ValueError):
    """A snapshot the history refers to is gone or no longer matches."""


def _workspace_root() -> Path:
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415

    explicit = get_active_tool_workspace_root()
    if explicit:
        return Path(explicit).resolve()
    # Context folding may run outside a tool call. Its media still belongs to
    # the session's workspace, never the server's installation directory.
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    sid = context.active_react_session() or context.active_session_id()
    if app is not None and sid:
        session = app.state.sessions.get(sid)
        workspace = app.state.workspaces.get(session.workspace_id) if session else None
        if workspace is None or not workspace.root_path:
            raise ViewedMediaUnavailable("the session has no resolved media workspace")
        return Path(workspace.root_path).resolve()
    return Path.cwd().resolve()


def snapshot(data: bytes, suffix: str) -> tuple[str, str]:
    """Store bytes and return a workspace-bound snapshot reference and SHA-256."""
    from clio_agent.tools.servers.shell_spill_store import (  # noqa: PLC0415
        active_session_id,
        spill_directory,
    )

    root = _workspace_root()
    digest = hashlib.sha256(data).hexdigest()
    folder = spill_directory(root, session_id=active_session_id())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"viewed-{digest[:24]}{suffix}"
    if not path.exists():
        with open(win_extended_path(path), "wb") as stream:
            stream.write(data)
    if path.is_relative_to(root):
        return path.relative_to(root).as_posix(), digest
    # Canonical state lives outside the authored workspace. Bind the reference
    # to this workspace's managed tool-output tree rather than an absolute path.
    return _MANAGED_REFERENCE + path.relative_to(spill_directory(root)).as_posix(), digest


def read_snapshot(relative: str, sha256: str) -> bytes:
    """The snapshot's bytes, verified; :class:`ViewedMediaUnavailable` otherwise."""
    root = _workspace_root()
    if relative.startswith(_MANAGED_REFERENCE):
        from clio_agent.tools.servers.shell_spill_store import spill_directory

        base = spill_directory(root).resolve()
        requested = Path(relative.removeprefix(_MANAGED_REFERENCE))
    else:
        base = root
        requested = Path(relative)
    path = (base / requested).resolve()
    if requested.is_absolute() or not path.is_relative_to(base):
        raise ViewedMediaUnavailable("the snapshot reference escapes its workspace storage")
    try:
        with open(win_extended_path(path), "rb") as stream:
            data = stream.read()
    except OSError as exc:
        raise ViewedMediaUnavailable(f"the snapshot {relative} is gone ({exc})") from exc
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha256):
        raise ViewedMediaUnavailable(f"the snapshot {relative} no longer matches")
    return data
