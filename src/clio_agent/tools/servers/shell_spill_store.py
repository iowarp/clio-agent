"""Where spilled shell output lives, and its session-scoped retention (#1487).

Oversize shell output (:mod:`clio_agent.tools.servers.shell_output`) is written
under the workspace's CLIO-owned ``.clio`` root
(:func:`clio_agent.paths.workspace_clio`, the root ``.clio/inputs`` and
``.clio/plans`` share), in a per-session folder::

    <workspace>/.clio/tool-output/<session_id>/<call-id>.<stream>.txt

The session owns its spills: the session-delete path calls
:func:`delete_session_spills`, which removes that folder and emits one typed log
line. There is no timer and no TTL — a spill lives exactly as long as the
session whose transcript cites it. The app-less CLI path has no session, so its
spills land flat in ``.clio/tool-output/``.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from clio_agent import paths
from clio_agent.platform_paths import win_extended_path
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

#: Directory under the workspace ``.clio`` root that holds spilled tool output.
SPILL_DIRNAME = "tool-output"
#: Typed reason logged when a deleted session's spill folder is removed.
SPILLS_DELETED_REASON = "session_deleted"
#: Typed reason when a deleted session's spill folder could not be removed.
SPILLS_DELETE_FAILED_REASON = "shell_output_spills_delete_failed"

_SESSION_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _safe_session_id(session_id: str | None) -> str:
    """Return ``session_id`` when it is a safe single path segment, else ``""``."""

    sid = (session_id or "").strip()
    return sid if _SESSION_SEGMENT.fullmatch(sid) else ""


def spill_directory(root: str | Path, *, session_id: str | None = None) -> Path:
    """Return where oversize shell output is spilled.

    ``<root>/.clio/tool-output/<session_id>`` when a session owns the call, so
    deleting the session deletes its spills (:func:`delete_session_spills`);
    ``<root>/.clio/tool-output`` for the app-less CLI path, which has no session.
    """

    base = paths.workspace_clio(root) / SPILL_DIRNAME
    sid = _safe_session_id(session_id)
    return base / sid if sid else base


def active_session_id() -> str:
    """The session that owns the current tool call, or ``""`` (app-less CLI).

    Read lazily from the GACT runtime context, the same way
    :mod:`clio_agent.tools.mcp_executor` reads it, so the tools layer carries no
    import-time dependency on GACT.
    """

    from clio_agent.gact import context as gact_context  # noqa: PLC0415 - keep tools a leaf

    return gact_context.active_session_id() or gact_context.active_tool_session_id()


def delete_session_spills(root: str | Path, session_id: str) -> int:
    """Delete one session's spilled output; return the number of files removed.

    Called from the session-delete path. Emits one typed log line per cleanup
    (:data:`SPILLS_DELETED_REASON`), or a typed warning when removal fails. An
    unsafe session id (not a single path segment) never deletes anything.
    """

    sid = _safe_session_id(session_id)
    if not sid:
        return 0
    folder = spill_directory(root, session_id=sid)
    if not folder.is_dir():
        return 0
    files = sum(1 for entry in folder.rglob("*") if entry.is_file())
    try:
        shutil.rmtree(win_extended_path(folder))
    except OSError as exc:
        logger.warning(
            "shell output spills delete failed reason=%s session=%s path=%s error=%r",
            SPILLS_DELETE_FAILED_REASON,
            sid,
            folder,
            exc,
        )
        trace.event(
            "TOOLS",
            "shell output spills delete failed reason=%s session=%s path=%s",
            SPILLS_DELETE_FAILED_REASON,
            sid,
            folder,
        )
        return 0
    logger.info(
        "shell output spills deleted reason=%s session=%s files=%d path=%s",
        SPILLS_DELETED_REASON,
        sid,
        files,
        folder,
    )
    return files


def new_call_id() -> str:
    """Return a sortable, collision-safe id for one shell call's spill files."""

    return f"sh_{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


__all__ = [
    "SPILLS_DELETED_REASON",
    "SPILLS_DELETE_FAILED_REASON",
    "SPILL_DIRNAME",
    "active_session_id",
    "delete_session_spills",
    "new_call_id",
    "spill_directory",
]
