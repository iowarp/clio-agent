"""The calling session, stamped into every product MCP tool call's ``_meta``.

A plugin's MCP server serves every session of a workspace, so it cannot tell which
session called it. CLIO tells it: each call's ``_meta`` carries
``ai.iowarp.clio/caller`` = ``{"session": <calling session>, "root": <the root of its
task tree>}``. A server that keeps per-branch state (a study's readings, a game's
branch) keys it by these ids. The ids are opaque; they grant nothing.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: The ``_meta`` key a plugin server reads the caller from.
CALLER_META_KEY = "ai.iowarp.clio/caller"

_ACTIVE_CALLER: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "clio_active_tool_caller", default=None
)


@contextmanager
def tool_caller_context(session: str, root: str) -> Iterator[None]:
    """Bind the calling session and its tree root for the turn's tool calls."""

    token = _ACTIVE_CALLER.set({"session": session, "root": root or session} if session else None)
    try:
        yield
    finally:
        _ACTIVE_CALLER.reset(token)


def caller_meta() -> dict[str, Any] | None:
    """The ``_meta`` entry naming the caller, or ``None`` off-turn."""

    caller = _ACTIVE_CALLER.get()
    return {CALLER_META_KEY: dict(caller)} if caller else None


__all__ = ["CALLER_META_KEY", "caller_meta", "tool_caller_context"]
