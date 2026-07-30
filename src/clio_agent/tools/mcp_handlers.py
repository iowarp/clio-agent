"""Typed handler slots for execution-path MCP clients (#1106).

======================================================================
P1 IMPLEMENTER — READ THIS FIRST. Handlers here are CONSTRUCTION-TIME SLOTS
ONLY. NO handler may be *wired* (given a real hook that acts on a firing) until
correlation-by-protocol-identity lands: request ids / progress tokens / task
ids with real lifecycles, resolved AT THE BACKEND RECEIVE LOOP where the event
actually arrives — NOT off ambient state. Track that work in clio-agent#1111 /
clio-agent#1113. Until then the CLIO hooks (:class:`ElicitationHook`,
:class:`ProgressHook`, :class:`MessageHook`) are adapters that forward with a
``None`` context: honest about the fact that we cannot yet say *which*
invocation a background event belongs to. The one exception is
:class:`MessageMultiplexer`, which is safe today because task-status messages
carry their own ``taskId`` and self-correlate through FastMCP's task registry
(no ambient state involved).

Two review findings were deferred to that P1 work. They are the reason wiring is
forbidden here — do not lose them:

FINDING 1 (gateway proxy backend). The agent gateway mounts each declared MCP
server as a ``FastMCP.as_proxy`` proxy. The proxy does not call the backend with
one fixed client: it runs ``client.new()`` per request, so a handler must be
carried onto the *cloned* upstream client, not just the one the factory built.
``Client.new``'s ``copy.copy`` carries session-kwargs handlers across, and
:class:`MessageMultiplexer` is made clone-safe for the message slot, but the
GENERAL question — how every handler kind reaches the true upstream call path
through the proxy, and how a proxied elicitation/progress event is attributed
back to the originating frontend request — is unsolved design work, not a slot.

FINDING 6 (cross-session correlation). The executor caches ONE long-lived client
per namespace, while session identity is PER TOOL CALL, and handlers fire on the
client's background receive loop — not the coroutine that issued the call. So a
handler cannot read the invocation off a ``ContextVar`` (wrong task/loop) nor
capture it at construction (wrong call). Correct attribution must key off the
protocol identity carried IN the event (request id / progress token / task id)
matched to a per-invocation record the executor opened for that id. That is the
lifecycle P1 must build before any elicitation/progress hook is wired.
======================================================================

The message slot is a MULTIPLEXER (:class:`MessageMultiplexer`): FastMCP's
``Client`` installs a ``TaskNotificationHandler`` by default, and replacing the
``message_handler`` with a plain callback would disable the built-in task-status
routing P1/P2 need. The multiplexer runs the wrapped task handler first, then
fans out to the CLIO hook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ElicitationDispatcher",
    "ElicitationHook",
    "MCPInvocationContext",
    "MessageHook",
    "MessageMultiplexer",
    "ProgressDispatcher",
    "ProgressHook",
]


@dataclass(frozen=True)
class MCPInvocationContext:
    """The per-call identity a wired handler will need (P1 populates it).

    Present today only as the declared type of the ``context`` argument the CLIO
    hook Protocols receive. Until correlation-by-protocol-identity lands the
    adapters pass ``None`` — see the module docstring.
    """

    invocation_id: str
    session_id: str | None = None
    namespace: str | None = None
    tool_name: str | None = None


@runtime_checkable
class ElicitationHook(Protocol):
    """CLIO elicitation hook: fastmcp's elicitation signature + the context."""

    async def __call__(
        self,
        context: MCPInvocationContext | None,
        message: str,
        response_type: Any,
        params: Any,
        request_context: Any,
    ) -> Any: ...


@runtime_checkable
class ProgressHook(Protocol):
    """CLIO progress hook: fastmcp's progress signature + the context."""

    async def __call__(
        self,
        context: MCPInvocationContext | None,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None: ...


@runtime_checkable
class MessageHook(Protocol):
    """CLIO message hook: the received server message + the context."""

    async def __call__(
        self,
        context: MCPInvocationContext | None,
        message: Any,
    ) -> None: ...


class ElicitationDispatcher:
    """fastmcp ``elicitation_handler`` adapter forwarding to a CLIO hook.

    Passes ``context=None`` — correlation is P1 work (see the module docstring).
    """

    def __init__(self, hook: ElicitationHook) -> None:
        self._hook = hook

    async def __call__(
        self, message: str, response_type: Any, params: Any, request_context: Any
    ) -> Any:
        return await self._hook(None, message, response_type, params, request_context)


class ProgressDispatcher:
    """fastmcp ``progress_handler`` adapter forwarding to a CLIO hook.

    Passes ``context=None`` — correlation is P1 work (see the module docstring).
    """

    def __init__(self, hook: ProgressHook) -> None:
        self._hook = hook

    async def __call__(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        await self._hook(None, progress, total, message)


class MessageMultiplexer:
    """fastmcp ``message_handler`` that preserves task routing AND calls a hook.

    The wrapped ``task_handler`` (FastMCP's ``TaskNotificationHandler``) runs
    first so built-in task-status routing is preserved; then the CLIO hook is
    invoked. ``bind_task_handler`` lets the factory attach the task handler after
    the owning client (which the handler needs a reference to) exists — and lets
    a clone rebind a fresh handler to the cloned client (finding 5 / clone
    safety). The CLIO hook receives ``context=None`` today; task-status messages
    self-correlate through their ``taskId``, so the built-in routing is exact
    regardless.
    """

    def __init__(
        self,
        hook: MessageHook,
        task_handler: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self._hook = hook
        self._task_handler = task_handler

    def bind_task_handler(
        self, task_handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        """Attach (or rebind, for a clone) the wrapped task handler."""
        self._task_handler = task_handler

    async def __call__(self, message: Any) -> None:
        if self._task_handler is not None:
            await self._task_handler(message)
        await self._hook(None, message)
