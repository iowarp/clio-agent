"""The session's tool posture, applied where a react agent's toolset is assembled.

A read-only side session (:mod:`clio_agent.gact.side_sessions`) may only run
tools that declare no side effects. Tool calls that reach the permission gate
are held to that per call (the read-only ACL). Native agent tools do not reach
the gate (spawn and workflow control, parent messaging, scheduling, artifact
publication, alert cards, UI surfaces, skill effects), so for such a session
they are removed from the toolset before the agent ever sees them:

* a gate-routed tool (an MCP-bridged or external-MCP tool, whose callable the
  execution boundary marks observed and which carries no native presenter) is
  kept; the gate decides each call from its annotations;
* a native tool is kept only when its construction DECLARED ``read_only``
  (:data:`~clio_agent.gact.agents.tool_instrumentation.READ_ONLY_ATTR`);
* anything else (an undeclared native, an unrecognised shape) is removed,
  never guessed.

Every removal is traced with its typed reason so the reduced surface is
visible after the fact. Every other session is returned unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.runtime import trace

#: Typed reason on a native tool withheld from a read-only side session.
REASON_NOT_DECLARED_READ_ONLY = "native_tool_not_declared_read_only"


def _active_session() -> Any:
    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        return None
    return app.state.sessions.get(sid)


def _gate_routed(func: Any) -> bool:
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        PRESENTER_ATTR,
        TOOL_OBSERVED_ATTR,
    )

    return bool(getattr(func, TOOL_OBSERVED_ATTR, False)) and (
        getattr(func, PRESENTER_ATTR, None) is None
    )


def restrict_tools_to_session_posture(tools: Iterable[Any]) -> list[Any]:
    """Return the tools the active session's posture allows its agent to see."""

    from clio_agent.gact.agents.tool_instrumentation import READ_ONLY_ATTR  # noqa: PLC0415
    from clio_agent.gact.side_sessions import is_read_only_side_session  # noqa: PLC0415

    listed = list(tools)
    session = _active_session()
    if not is_read_only_side_session(session):
        return listed
    kept: list[Any] = []
    withheld: list[str] = []
    for tool in listed:
        func = getattr(tool, "func", None)
        if _gate_routed(func) or bool(getattr(func, READ_ONLY_ATTR, False)):
            kept.append(tool)
        else:
            withheld.append(str(getattr(tool, "name", "") or "<unnamed>"))
    if withheld:
        trace.event(
            "SIDE-SESSION",
            "side_session_tools_withheld session=%s reason=%s tools=%s",
            getattr(session, "id", ""),
            REASON_NOT_DECLARED_READ_ONLY,
            ",".join(sorted(withheld)),
        )
    return kept


__all__ = ["REASON_NOT_DECLARED_READ_ONLY", "restrict_tools_to_session_posture"]
