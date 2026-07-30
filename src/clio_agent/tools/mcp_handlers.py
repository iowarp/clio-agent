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
invocation a background event belongs to.

Two review findings were deferred to that P1 work. They are the reason wiring is
forbidden here — do not lose them:

FINDING 1 (gateway proxy backend). The agent gateway mounts each declared MCP
server through a ``create_proxy`` proxy. The proxy does not call the backend with
one fixed client: it runs ``client.new()`` per request, so a handler must be
carried onto the *cloned* upstream client, not just the one the factory built.
FastMCP 4's ``Client.new`` carries the base message handler across, but the
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

The message slot retains the :class:`MessageMultiplexer` adapter name from the
factory contract. On FastMCP 4, task-status routing is extension-based and no
longer occupies ``message_handler``; the adapter forwards only to the CLIO hook.
``Client.new`` preserves that base handler for proxy clones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import mcp.types as mcp_types

__all__ = [
    "ElicitationDispatcher",
    "ElicitationHook",
    "MCPClientCapabilities",
    "MCPInvocationContext",
    "MessageHook",
    "MessageMultiplexer",
    "ProgressDispatcher",
    "ProgressHook",
]


@dataclass(frozen=True)
class MCPClientCapabilities:
    """Client capabilities CLIO *declares* on the wire, decoupled from handler wiring.

    The 2026-07-28 ``_meta`` envelope advertises ``clientCapabilities`` on every
    request. The installed SDK derives that ad from which handler callbacks are
    wired and, for elicitation, hardcodes BOTH ``form`` and ``url`` whenever an
    elicitation callback is present — so wiring a form-only handler would
    over-advertise ``url`` and invite requests CLIO cannot serve. This typed
    declaration separates *what CLIO advertises* from *what handler is live*: the
    factory honors it via the sanctioned ``ClientSession`` ``session_class`` seam
    (see :func:`clio_agent.tools.mcp_runtime.make_mcp_client`) so a capability is
    advertised at exactly the declared granularity, whether or not a live handler
    backs it yet. This is the contract #1113 fills when it wires elicitation.

    A declaration is authoritative PER CAPABILITY DOMAIN IT MODELS — today only
    elicitation. Every field defaults False, so an empty declaration pins the
    elicitation domain absent (the honest state today, since
    correlation-by-protocol-identity is deferred and no handler is wired).
    Domains this type does not model (sampling/roots/log) are deliberately left
    to the session's wiring-derived value, which is truthful on both paths: a
    direct client advertises only what is actually wired, and a proxy backend
    genuinely forwards those server-initiated requests to the front — clearing
    them would sever push-forwarding. Fields are independently selectable.
    """

    elicitation_form: bool = False
    elicitation_url: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether no modeled domain is declared (elicitation pinned absent)."""

        return not (self.elicitation_form or self.elicitation_url)

    def elicitation_capability(self) -> "mcp_types.ElicitationCapability | None":
        """Build the SDK ``ElicitationCapability`` for the declared modes, or None.

        ``None`` when neither elicitation mode is declared, so the advertised
        ``clientCapabilities`` simply omits the ``elicitation`` key.
        """

        import mcp.types as mcp_types  # noqa: PLC0415

        if not (self.elicitation_form or self.elicitation_url):
            return None
        return mcp_types.ElicitationCapability(
            form=mcp_types.FormElicitationCapability() if self.elicitation_form else None,
            url=mcp_types.UrlElicitationCapability() if self.elicitation_url else None,
        )


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
    """FastMCP ``message_handler`` adapter forwarding to the CLIO hook.

    FastMCP 4 routes task notifications through client extensions instead of an
    internal message handler, and ``Client.new`` preserves this adapter for
    proxy clones. The CLIO hook receives ``context=None`` today.
    """

    def __init__(self, hook: MessageHook) -> None:
        self._hook = hook

    async def __call__(self, message: Any) -> None:
        await self._hook(None, message)
