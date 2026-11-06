"""The injections a session's plugins declare (``<plugin root>/ai.iowarp.clio/injections``).

A session's plugin set is its pinned resolution (``gact.agents.resolution``). Each
non-core plugin's declarations are read from its root once per plugin version (the
resolution's ``text_digest``); core's come from :func:`~clio_agent.injections.
declarations.core_injections`. A session with no pinned resolution yet (before its
first turn), or whose pin no longer resolves, has no plugin injections; nothing is
pinned from here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from clio_agent.injections.declarations import Injection, read_dir

__all__ = ["plugin_injections", "plugin_set_key"]

_CORE = "core"


@lru_cache(maxsize=256)
def _read(root: str, plugin: str, digest: str) -> tuple[Injection, ...]:
    del digest  # the cache key: a new plugin version is read again
    return tuple(read_dir(Path(root) / "ai.iowarp.clio" / "injections", plugin=plugin))


def plugin_set_key(app: Any, session_id: str) -> str:
    """The session's pinned resolution digest (``""`` before its first turn): it changes
    whenever the session's plugins, or one plugin's version, change."""
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    if session is None:
        return ""
    from clio_agent.gact.agents.resolution import AGENT_RESOLUTION_KEY  # noqa: PLC0415

    return str((getattr(session, "metadata", None) or {}).get(AGENT_RESOLUTION_KEY) or "")


def plugin_injections(app: Any, session_id: str) -> list[Injection]:
    """Every injection the session's plugins (not core) declare, in plugin order."""
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    if session is None:
        return []
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_RESOLUTION_KEY,
        SessionAgentUnavailable,
        pinned_resolution,
    )

    metadata = getattr(session, "metadata", None) or {}
    digest = str(metadata.get(AGENT_RESOLUTION_KEY) or "")
    if not digest:
        return []
    try:
        resolution = pinned_resolution(app, digest)
    except SessionAgentUnavailable:
        # A pin that does not resolve has no plugins to read. The turn cannot run on
        # it: its agent resolution, the owner of that error, fails it typed
        # (``agent_unavailable``) with every resolution error. Raised here, in the
        # turn's prologue, it was settled as a generic finalize crash instead.
        return []
    out: list[Injection] = []
    for row in resolution.plugins:
        if row.get("id") == _CORE or not row.get("root"):
            continue
        out.extend(_read(str(row["root"]), str(row["id"]), str(row.get("text_digest") or "")))
    return out
