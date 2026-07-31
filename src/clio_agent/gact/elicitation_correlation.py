"""Receive-loop correlation for elicitation on shared / cloned execution clients (#1113, finding 2).

The dynamic-agent path builds one client per call, so construction-bound correlation
is correct there. The declared-server GATEWAY and the main EXECUTOR reuse a long-lived
client / per-request clones that serve MANY calls, so a wired elicitation handler
cannot capture its invocation at construction. This module resolves the invocation at
the receive loop instead: a per-call lifecycle record is OPENED at the tool-call
boundary (the gact tool observer, on the call task where the CLIO session resolves) and
RESOLVED from the SDK ``ClientRequestContext.session`` when the handler fires, then
CLOSED on completion / error.

The MCP client SDK exposes only the elicitation request's own id and the
``ClientSession`` to the handler (no ``related_request_id``), so the resolvable
protocol identity is the ``ClientSession``. Because the executor serializes tool calls
(one in flight per executor), a single open record correlates unambiguously; the
``ClientSession`` is bound to the record on first fire so several elicitations within
one call stay precisely keyed. When more than one record is open and none matches the
session — concurrent cross-executor elicitations, which the SDK cannot disambiguate —
the handler DECLINES with a typed reason rather than mis-correlate.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.tools.mcp_handlers import MCPInvocationContext

logger = logging.getLogger(__name__)

__all__ = [
    "close_invocation",
    "correlated_capabilities",
    "correlated_elicitation_handler",
    "correlated_session_id",
    "make_correlated_handlers",
    "open_invocation",
]


@dataclass
class _InvocationRecord:
    """One in-flight tool call's correlation record (opened at the call boundary)."""

    app: Any
    invocation: "MCPInvocationContext"
    session_key: int | None = None


_LOCK = threading.Lock()
_OPEN: list[_InvocationRecord] = []


def open_invocation(
    app: Any, *, session_id: str, tool_name: str, invocation_id: str = ""
) -> _InvocationRecord:
    """Open a per-call record when a tool call starts (call task). Returns the token."""

    from clio_agent.tools.mcp_handlers import MCPInvocationContext  # noqa: PLC0415

    namespace = tool_name.split("_", 1)[0] if "_" in tool_name else None
    invocation = MCPInvocationContext(
        invocation_id=invocation_id or tool_name,
        session_id=session_id,
        namespace=namespace,
        tool_name=tool_name,
    )
    record = _InvocationRecord(app=app, invocation=invocation)
    with _LOCK:
        _OPEN.append(record)
    return record


def close_invocation(record: _InvocationRecord | None) -> None:
    """Close the record when the call completes / errors (always paired with open)."""

    if record is None:
        return
    with _LOCK:
        try:
            _OPEN.remove(record)
        except ValueError:
            pass


def _resolve_for_session(session_key: int) -> _InvocationRecord | None:
    """Resolve the in-flight record for the ClientSession the elicitation arrived on.

    Session-keyed when a prior fire bound this session; otherwise the single open
    record (unambiguous under executor serialization), which is then bound to the
    session. Ambiguous (>1 open, none matching) -> ``None`` (typed decline).
    """

    with _LOCK:
        for record in _OPEN:
            if record.session_key == session_key:
                return record
        if len(_OPEN) == 1:
            record = _OPEN[0]
            record.session_key = session_key
            return record
        return None


async def correlated_elicitation_handler(
    context: Any,
    message: str,
    response_type: Any,
    params: Any,
    request_context: Any,
) -> Any:
    """App-agnostic elicitation hook resolving its invocation at the receive loop.

    Matches :class:`~clio_agent.tools.mcp_handlers.ElicitationHook`. Resolves the
    originating call from the correlation registry via ``request_context.session``;
    on success delegates to the bridge's :func:`handle_elicitation`, on failure
    returns a typed decline (never mis-correlates a background event).
    """

    from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
        _resolve_trusted_origins,
        handle_elicitation,
    )

    session_key = id(getattr(request_context, "session", None))
    record = _resolve_for_session(session_key)
    if record is None:
        from fastmcp.client.elicitation import ElicitResult  # noqa: PLC0415

        logger.info(
            "elicitation declined reason=elicitation_uncorrelated session_key=%s open=%d",
            session_key,
            len(_OPEN),
        )
        return ElicitResult(action="decline")
    return await handle_elicitation(
        record.app,
        record.invocation,
        message,
        params,
        url_trusted_origins=_resolve_trusted_origins(None),
    )


def correlated_session_id(session: Any) -> str | None:
    """The CLIO session id for the ``ClientSession`` a task was created on (#1115).

    Same registry, same resolution rule as the elicitation handler: a SEP-2663
    ``CreateTaskResult`` is resolved on the call that produced it, so the
    ``ClientSession`` carried on the claim context identifies the in-flight call.
    ``None`` when the call cannot be attributed (no open record, or several open
    and none bound) — the task-record store then reports the unattributed write
    with a typed reason rather than guessing a session.
    """

    record = _resolve_for_session(id(session))
    return record.invocation.session_id if record is not None else None


def make_correlated_handlers() -> Any:
    """Return an :class:`MCPClientHandlers` wiring the receive-loop elicitation hook.

    Passed to ``build_gateway`` / the executor so every declared-server tool call can
    reach the HITL surface. The hook is app-agnostic (it resolves the app from the
    correlation record), so it is safe to bind once onto a shared gateway.
    """

    from clio_agent.tools.mcp_runtime import MCPClientHandlers  # noqa: PLC0415

    return MCPClientHandlers(elicitation=correlated_elicitation_handler)


def correlated_capabilities() -> Any:
    """Declare the elicitation capability at the served granularity for the gateway.

    #1113 finding 2 (partial): the wired SDK callback otherwise auto-advertises BOTH
    form and url, but the correlated handler declines every url request unless a trust
    allow-list is configured. Advertise form always and url ONLY when trusted origins
    are configured, so a server never picks an advertised mode that always fails.
    """

    from clio_agent.gact.elicitation_bridge import _resolve_trusted_origins  # noqa: PLC0415
    from clio_agent.tools.mcp_handlers import MCPClientCapabilities  # noqa: PLC0415

    origins = _resolve_trusted_origins(None)
    return MCPClientCapabilities(elicitation_form=True, elicitation_url=bool(origins))
