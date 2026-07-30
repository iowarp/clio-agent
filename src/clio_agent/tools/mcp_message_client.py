"""Clone-safe FastMCP ``Client`` for the message multiplexer slot (#1106).

FastMCP proxies (``FastMCP.as_proxy``) call a backend through ``client.new()``
once per request, and ``Client.new`` only rebinds ``message_handler`` when it is
a plain ``TaskNotificationHandler`` (it constructs a fresh
``TaskNotificationHandler(new_client)``). A :class:`MessageMultiplexer` is not
that type, so a naive clone would carry a multiplexer whose task handler still
points at the ORIGINAL client — task-status notifications delivered to the clone
would update the wrong client's task registry.

:class:`MultiplexingMessageClient` overrides ``new`` to reinstall a fresh
multiplexer bound to a ``TaskNotificationHandler`` for the CLONE, so the built-in
task routing is exact on the clone while the CLIO message hook still fires.

This module is imported lazily (only when a message hook is present) so importing
``fastmcp`` stays off the hot ``mcp_runtime`` import path.
"""

from __future__ import annotations

from fastmcp import Client
from fastmcp.client.client import TaskNotificationHandler

from clio_agent.tools.mcp_handlers import MessageMultiplexer


class MultiplexingMessageClient(Client):
    """A ``Client`` whose ``new()`` clone rebinds the message multiplexer."""

    def new(self) -> "MultiplexingMessageClient":
        """Clone, then rebind a fresh multiplexer + task handler to the clone."""
        clone = super().new()
        mux = clone._session_kwargs.get("message_handler")
        if isinstance(mux, MessageMultiplexer):
            fresh = MessageMultiplexer(mux._hook)
            fresh.bind_task_handler(TaskNotificationHandler(clone))
            clone._session_kwargs["message_handler"] = fresh
        return clone
