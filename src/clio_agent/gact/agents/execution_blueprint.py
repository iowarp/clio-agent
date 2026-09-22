"""Resolve the persistent and turn-scoped Agent Blueprint identities.

Deep Research is an execution overlay: it must drive the live coordinator and
its children without replacing the base blueprint the user selected for the
session.  Keeping that distinction in one small owner module prevents the
general agent-resolution module from accumulating lifecycle policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from clio_agent.gact import context as _ctx

if TYPE_CHECKING:
    from fastapi import FastAPI


def runtime_active_agent_blueprint_id(app: "FastAPI", session_id: str = "") -> str:
    """Return the session's explicitly activated persistent blueprint id."""

    if not session_id:
        return ""
    session = app.state.sessions.get(session_id)
    if session is None:
        return ""
    metadata = getattr(session, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("active_agent_blueprint_id") or "").strip()


def runtime_active_agent_blueprint_path(app: "FastAPI", session_id: str = "") -> Path | None:
    """Return the session's explicitly activated persistent blueprint path."""

    if not session_id:
        return None
    session = app.state.sessions.get(session_id)
    if session is None:
        return None
    metadata = getattr(session, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return None
    raw = str(metadata.get("active_agent_blueprint_path") or "").strip()
    return Path(raw).expanduser() if raw else None


def runtime_effective_agent_blueprint_id(
    app: "FastAPI",
    session_id: str = "",
    *,
    active_resolver: Callable[["FastAPI", str], str] = runtime_active_agent_blueprint_id,
) -> str:
    """Return the matching live-turn overlay, or the persistent blueprint id."""

    if session_id and _ctx.active_session_id() == session_id:
        override = _ctx.active_execution_blueprint_id().strip()
        if override:
            return override
    return active_resolver(app, session_id)


def runtime_effective_agent_blueprint_path(
    app: "FastAPI",
    session_id: str = "",
    *,
    active_resolver: Callable[["FastAPI", str], Path | None] = runtime_active_agent_blueprint_path,
) -> Path | None:
    """Return a persistent path only when no id-based turn overlay is active."""

    if session_id and _ctx.active_session_id() == session_id:
        if _ctx.active_execution_blueprint_id().strip():
            return None
    return active_resolver(app, session_id)


__all__ = [
    "runtime_active_agent_blueprint_id",
    "runtime_active_agent_blueprint_path",
    "runtime_effective_agent_blueprint_id",
    "runtime_effective_agent_blueprint_path",
]
