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

__all__ = ["ViewedMediaUnavailable", "read_snapshot", "snapshot"]


class ViewedMediaUnavailable(ValueError):
    """A snapshot the history refers to is gone or no longer matches."""


def _workspace_root() -> Path:
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415

    return Path(get_active_tool_workspace_root() or Path.cwd()).resolve()


def snapshot(data: bytes, suffix: str) -> tuple[str, str]:
    """Store ``data``; return ``(workspace-relative path, sha256)`` of the snapshot."""
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
        path.write_bytes(data)
    return path.relative_to(root).as_posix(), digest


def read_snapshot(relative: str, sha256: str) -> bytes:
    """The snapshot's bytes, verified; :class:`ViewedMediaUnavailable` otherwise."""
    path = _workspace_root() / relative
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ViewedMediaUnavailable(f"the snapshot {relative} is gone ({exc})") from exc
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha256):
        raise ViewedMediaUnavailable(f"the snapshot {relative} no longer matches")
    return data
