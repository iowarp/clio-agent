"""``subscriptions/listen`` -> listing-cache invalidation (SEP-2575, #1285 C1-S5 item 2).

The mcp SDK exposes the raw listen driver as a free function,
``mcp.client.subscriptions.listen(session, ...)`` -- a ``fastmcp.Client`` has
no ``.listen()`` wrapper of its own (verified: no ``def listen`` anywhere
under ``fastmcp/client/``), so :func:`watch_list_changed` drives the SDK
function directly against a connected client's ``.session``. One open
subscription watches a single namespace's server for
``notifications/tools/list_changed``; on each event it invalidates that
namespace's ``tools/listing_cache.py`` entry so the next boot/refresh
re-lists live instead of serving a listing that has gone stale for up to the
TTL.

**Verified library gap (#1285 C1-S5):** fastmcp 4.0.0b1's SERVER has NO
``subscriptions/listen`` support at all -- a repo-wide + venv-wide grep for
"Subscription"/"subscriptions/listen" under ``fastmcp/`` returns zero hits,
and a live probe against the exerciser confirms it: ``listen()`` raises
``-32601 Method not found``. The raw mcp SDK's OWN high-level server
(``mcp.server.mcpserver.server.MCPServer``, a DIFFERENT class from
``fastmcp.FastMCP``) DOES wire ``on_subscriptions_listen`` -- so this is a
fastmcp-specific gap, not a protocol-wide one, and :func:`watch_list_changed`
is still the spec-correct client for any server that implements SEP-2575
properly. What DOES work against today's fastmcp servers (verified live):
fastmcp still emits ``notifications/tools/list_changed`` UNSOLICITED over the
plain connection (the legacy push style) even on a modern-era negotiation --
:func:`list_changed_message_handler` reacts to that instead, giving CLIO a
real, live-provable invalidation path against the fastmcp fleet TODAY while
:func:`watch_list_changed` covers spec-complete servers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from clio_agent.runtime import trace

__all__ = ["ListenUnsupported", "list_changed_message_handler", "watch_list_changed"]


class ListenUnsupported(RuntimeError):
    """Raised when the connected session cannot drive ``subscriptions/listen``.

    Typed wrapper around the SDK's own :class:`~mcp.client.subscriptions.ListenNotSupportedError`
    (a legacy-era connection) so callers never need to import the SDK exception
    to distinguish "this server can't do this" from a real failure.
    """


class _SessionHolder(Protocol):
    @property
    def session(self) -> Any: ...


async def watch_list_changed(
    client: _SessionHolder,
    namespace: str,
    *,
    on_change: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Watch ``client``'s connected server for tool-list changes until cancelled.

    Runs :func:`mcp.client.subscriptions.listen` with ``tools_list_changed=True``
    and, for every :class:`~mcp.shared.subscriptions.ToolsListChanged` event,
    invalidates ``namespace``'s cached listing
    (:func:`clio_agent.tools.listing_cache.invalidate_namespace`) and awaits
    ``on_change`` if given (e.g. a caller-supplied re-list trigger). Returns
    normally on the server's graceful close; intended to run as a background
    task a caller cancels on disconnect -- it does not itself reconnect or loop.

    Raises:
        ListenUnsupported: The connection predates 2026-07-28 (D1: "Optional
            feature, required shape" -- absence is a typed, expected outcome
            for a legacy-era or non-listen-capable server, never a silent no-op).
    """

    from mcp.client.subscriptions import (
        ListenNotSupportedError,
        SubscriptionLost,
        ToolsListChanged,
        listen,
    )
    from mcp.shared.exceptions import MCPError

    from clio_agent.tools import listing_cache

    trace.event("TOOLS", "mcp_listen_start namespace=%s", namespace)
    try:
        async with listen(client.session, tools_list_changed=True) as subscription:
            async for event in subscription:
                if isinstance(event, ToolsListChanged):
                    listing_cache.invalidate_namespace(namespace, reason="list_changed")
                    trace.event("TOOLS", "mcp_listen_tools_list_changed namespace=%s", namespace)
                    if on_change is not None:
                        await on_change()
    except ListenNotSupportedError as exc:
        trace.event(
            "TOOLS",
            "mcp_listen_unsupported namespace=%s version=%s",
            namespace,
            exc.negotiated_version,
        )
        raise ListenUnsupported(str(exc)) from exc
    except SubscriptionLost as exc:
        # The stream dropped without the server's graceful close -- typed and
        # surfaced (never silently swallowed); the caller decides whether/when
        # to re-listen (the SDK's own docstring: "re-listen and refetch").
        trace.event("TOOLS", "mcp_listen_lost namespace=%s reason=%s", namespace, exc)
        raise
    except MCPError as exc:
        # A server-side listen REJECTION (not a legacy-era mismatch) is a real,
        # typed failure worth naming -- never a silent watcher death.
        trace.event(
            "TOOLS",
            "mcp_listen_rejected namespace=%s code=%s message=%s",
            namespace,
            exc.code,
            exc.message,
        )
        raise
    finally:
        trace.event("TOOLS", "mcp_listen_end namespace=%s", namespace)


def list_changed_message_handler(namespace: str) -> Callable[[Any], Awaitable[None]]:
    """Build a ``message_handler`` that invalidates ``namespace`` on an unsolicited
    ``notifications/tools/list_changed`` (the path real fastmcp servers use today --
    see the module docstring). Pass the result as ``fastmcp.Client(message_handler=...)``
    or fold it into an existing dispatcher (e.g. CLIO's ``MessageMultiplexer``).

    Ignores every other message type -- a caller with other notifications to handle
    composes this alongside its own handler rather than this one growing a dispatch table.
    """

    async def _handler(message: Any) -> None:
        from mcp_types import ToolListChangedNotification

        if isinstance(message, ToolListChangedNotification):
            from clio_agent.tools import listing_cache

            listing_cache.invalidate_namespace(namespace, reason="list_changed")
            trace.event("TOOLS", "mcp_listen_tools_list_changed namespace=%s", namespace)

    return _handler
