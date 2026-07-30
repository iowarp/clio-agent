"""Handler dispatch shape for execution-path MCP clients (#1106).

P0.2 ships the SHAPE that P1 fills; the hooks themselves are absent today
(``None`` => that handler is not installed, identical to a bare client).

Two facts make a naive callback slot wrong, so the shape here is deliberate:

* FastMCP client handlers (elicitation / progress / message) fire on the
  client's **background receive loop**, not the coroutine that issued the tool
  call. A per-call :class:`contextvars.ContextVar` set by the caller is not
  reliably visible there, so a handler cannot read the invocation context off
  the ambient context.
* The executor caches **one long-lived client per namespace** while session
  identity is **per tool call**, so a handler cannot capture its invocation
  context at construction time either.

The seam resolves both. Handlers are LONG-LIVED DISPATCHERS bound to a stable
per-client correlation key. The executor binds the current invocation into a
shared :class:`MCPCorrelationRegistry` around each ``call_tool`` (reading the
ambient :data:`current_invocation` on its own coroutine, where it is valid).
A dispatcher, whenever it fires on the background loop, recovers the context by
key via :meth:`MCPCorrelationRegistry.resolve` — never by reading a ContextVar.

The message slot is a MULTIPLEXER (:class:`MessageMultiplexer`): FastMCP's
``Client`` installs a ``TaskNotificationHandler`` by default, and replacing the
``message_handler`` with a plain callback would disable the built-in task-status
routing P1/P2 need. The multiplexer runs the wrapped task handler first, then
fans out to the CLIO hook.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_CORRELATION_REGISTRY",
    "ElicitationDispatcher",
    "ElicitationHook",
    "MCPCorrelationRegistry",
    "MCPInvocationContext",
    "MessageHook",
    "MessageMultiplexer",
    "ProgressDispatcher",
    "ProgressHook",
    "current_invocation",
]


@dataclass(frozen=True)
class MCPInvocationContext:
    """The per-call identity a handler needs to correlate a background event."""

    invocation_id: str
    session_id: str | None = None
    namespace: str | None = None
    tool_name: str | None = None


class MCPCorrelationRegistry:
    """Thread-safe map of correlation key -> the currently-active invocation.

    The executor binds a context around ``call_tool`` (see :meth:`active`); a
    dispatcher firing on the client's background loop calls :meth:`resolve` with
    its own key to recover it. Each key holds a stack so re-entrant or nested
    calls on the same client never lose an outer context.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stacks: dict[str, list[MCPInvocationContext]] = {}

    def bind(self, key: str, context: MCPInvocationContext) -> None:
        """Push ``context`` as the active invocation for ``key``."""
        with self._lock:
            self._stacks.setdefault(key, []).append(context)

    def release(self, key: str) -> None:
        """Pop the most recent context for ``key``."""
        with self._lock:
            stack = self._stacks.get(key)
            if stack:
                stack.pop()
                if not stack:
                    self._stacks.pop(key, None)

    def resolve(self, key: str) -> MCPInvocationContext | None:
        """Return the active invocation for ``key`` (innermost), or ``None``."""
        with self._lock:
            stack = self._stacks.get(key)
            return stack[-1] if stack else None

    @contextmanager
    def active(self, key: str, context: MCPInvocationContext) -> Iterator[None]:
        """Bind ``context`` for the duration of the ``with`` block."""
        self.bind(key, context)
        try:
            yield
        finally:
            self.release(key)


# Shared default so make_mcp_client and the executor agree on one registry
# without any wiring between them.
DEFAULT_CORRELATION_REGISTRY = MCPCorrelationRegistry()

# Ambient per-call context. The executor reads this on its OWN coroutine (valid)
# and binds it into the registry; P1 sets it from the real session. It is never
# read on a handler's background loop.
current_invocation: ContextVar[MCPInvocationContext | None] = ContextVar(
    "clio_mcp_current_invocation", default=None
)


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


class _CorrelatedDispatcher:
    """Long-lived base that resolves its bound context on each fire."""

    def __init__(self, registry: MCPCorrelationRegistry, key: str) -> None:
        self._registry = registry
        self._key = key

    def _context(self) -> MCPInvocationContext | None:
        return self._registry.resolve(self._key)


class ElicitationDispatcher(_CorrelatedDispatcher):
    """fastmcp ``elicitation_handler`` that forwards to a CLIO hook with context."""

    def __init__(
        self, registry: MCPCorrelationRegistry, key: str, hook: ElicitationHook
    ) -> None:
        super().__init__(registry, key)
        self._hook = hook

    async def __call__(
        self, message: str, response_type: Any, params: Any, request_context: Any
    ) -> Any:
        return await self._hook(
            self._context(), message, response_type, params, request_context
        )


class ProgressDispatcher(_CorrelatedDispatcher):
    """fastmcp ``progress_handler`` that forwards to a CLIO hook with context."""

    def __init__(
        self, registry: MCPCorrelationRegistry, key: str, hook: ProgressHook
    ) -> None:
        super().__init__(registry, key)
        self._hook = hook

    async def __call__(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        await self._hook(self._context(), progress, total, message)


class MessageMultiplexer(_CorrelatedDispatcher):
    """fastmcp ``message_handler`` that preserves task routing AND calls a hook.

    The wrapped ``task_handler`` (FastMCP's ``TaskNotificationHandler``) runs
    first so built-in task-status routing is preserved; then the CLIO hook is
    invoked with the correlated context. ``bind_task_handler`` lets the factory
    attach the task handler after the client (which it needs a reference to)
    exists.
    """

    def __init__(
        self,
        registry: MCPCorrelationRegistry,
        key: str,
        hook: MessageHook,
        task_handler: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(registry, key)
        self._hook = hook
        self._task_handler = task_handler

    def bind_task_handler(
        self, task_handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        """Attach the wrapped task handler once the owning client exists."""
        self._task_handler = task_handler

    async def __call__(self, message: Any) -> None:
        if self._task_handler is not None:
            await self._task_handler(message)
        await self._hook(self._context(), message)
