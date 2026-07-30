"""Shared helpers for MCP runtime pathways.

Three historical wire contracts are preserved as explicit :func:`wire_value`
modes: ``mcp_results``, ``mcp_apps``, and ``gact_runtime``. Converging those
contracts is future wire-work, not part of this slice.

This module also owns :func:`make_mcp_client` (#1106) — the ONE construction
site for **execution-path** FastMCP clients. It carries the :class:`MCPClientHandlers`
slot (typed CLIO hooks; see :mod:`clio_agent.tools.mcp_handlers`) where P1
attaches elicitation/progress/message/cancellation handlers (no-op-absent
today). Execution paths route through it: the ``AsyncMCPToolExecutor`` default
``client_factory``, the gateway proxy backend (``tools/gateway._proxy_for_spec``),
the dynamic-agent external tool call (``gact/agents/builders``), the per-call
dispatch in ``gact/routes/mcp.py``, and the ``providers/handshake/mcp.py`` probe.

**The execution/introspection split (adopted default):** handlers wire on
execution paths only (paths that ``call_tool`` / dispatch a proxy backend), so
**list-only introspection sites do NOT migrate** and keep their bare
``Client()`` — the catalog/blueprint/status/gateway-listing passes
(``routes/catalog.py``, ``routes/blueprints.py``, ``runtime/status.py``,
``tools/gateway.list_gateway_tools``) plus the install/reconnect/inventory
``list_tools`` passes in ``routes/mcp.py``. They never dispatch a tool call, so
they never need the handler slot; forcing them through the factory would be pure
churn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.tools.mcp_handlers import (
    ElicitationDispatcher,
    MessageMultiplexer,
    ProgressDispatcher,
)

if TYPE_CHECKING:
    from clio_agent.tools.mcp_handlers import (
        ElicitationHook,
        MessageHook,
        ProgressHook,
    )

logger = logging.getLogger(__name__)

WireMode = Literal["mcp_results", "mcp_apps", "gact_runtime"]

_MISSING = object()
_VALID_MODES: frozenset[str] = frozenset(("mcp_results", "mcp_apps", "gact_runtime"))


def _dump_mcp_results_model(value: Any, *, exclude_none: bool) -> Any:
    """Return the historical MCP-results Pydantic projection when available."""

    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        return _MISSING
    attempts: tuple[dict[str, Any], ...] = (
        {"mode": "json", "by_alias": True, "exclude_none": exclude_none},
        {"by_alias": True, "exclude_none": exclude_none},
        {},
    )
    for kwargs in attempts:
        try:
            return dump(**kwargs)
        except TypeError:
            continue
    return _MISSING


def wire_value(
    value: Any,
    *,
    mode: WireMode,
    exclude_none: bool = False,
) -> Any:
    """Convert an SDK or Pydantic value using an explicit historical contract.

    Args:
        value: Value to recursively convert to plain wire data.
        mode: Historical contract to preserve. ``mcp_results`` uses JSON-mode,
            alias-preserving Pydantic dumps; ``mcp_apps`` uses its Python-mode,
            model-None-excluding behavior; ``gact_runtime`` preserves the
            runtime trace's tuple and sorted-set handling.
        exclude_none: Mapping and Pydantic-field filter used only by the
            ``mcp_results`` contract.

    Returns:
        A recursively converted value matching the selected historical output.

    Raises:
        ValueError: If ``mode`` does not name a preserved wire contract.
    """

    if mode not in _VALID_MODES:
        raise ValueError(f"unknown MCP wire mode: {mode!r}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if mode == "mcp_results":
        if isinstance(value, Mapping):
            return {
                str(key): wire_value(item, mode=mode, exclude_none=exclude_none)
                for key, item in value.items()
                if not (exclude_none and item is None)
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [wire_value(item, mode=mode, exclude_none=exclude_none) for item in value]
        dumped = _dump_mcp_results_model(value, exclude_none=exclude_none)
        if dumped is not _MISSING:
            return wire_value(dumped, mode=mode, exclude_none=exclude_none)
        return str(value)

    if mode == "mcp_apps":
        if isinstance(value, Mapping):
            return {str(key): wire_value(item, mode=mode) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [wire_value(item, mode=mode) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return wire_value(dump(by_alias=True, exclude_none=True), mode=mode)
        return str(value)

    if isinstance(value, Mapping):
        return {str(key): wire_value(item, mode=mode) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_value(item, mode=mode) for item in value]
    if isinstance(value, set):
        return sorted(wire_value(item, mode=mode) for item in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return wire_value(model_dump(exclude_none=True), mode=mode)
        except TypeError:
            return wire_value(model_dump(), mode=mode)
    return str(value)


@dataclass(frozen=True)
class MCPClientHandlers:
    """Typed CLIO hook bundle — a CONSTRUCTION-TIME SLOT, not a live wiring.

    Every hook is absent today (``None`` => that handler is not installed,
    identical to a bare client). The hooks are typed
    :mod:`clio_agent.tools.mcp_handlers` Protocols, not raw callbacks: each
    receives an :class:`MCPInvocationContext` first argument that P1 will
    populate. ``make_mcp_client`` wraps a populated hook in a signature adapter
    and hands it to the matching ``fastmcp.Client`` keyword; ``message`` becomes
    a :class:`MessageMultiplexer` that forwards to the CLIO hook. FastMCP 4
    handles task notifications through client extensions. ``cancellation`` has
    no fastmcp ``Client`` keyword today — held as a slot for P1.

    IMPORTANT: no hook may actually be *wired* until correlation-by-protocol-
    identity lands (clio-agent#1111/#1113). See the ``mcp_handlers`` module
    docstring for the two deferred review findings the P1 implementer must honor.
    """

    elicitation: "ElicitationHook | None" = None
    progress: "ProgressHook | None" = None
    message: "MessageHook | None" = None
    cancellation: "MessageHook | None" = None


def make_mcp_client(
    target: Any,
    *,
    handlers: MCPClientHandlers | None = None,
    client_cls: Callable[..., Any] | None = None,
) -> Any:
    """Construct an execution-path FastMCP client with the handler slot.

    This is the ONE construction site for clients that actually dispatch MCP
    calls. With no populated ``handlers`` the construction is byte-identical to
    a bare ``Client(target)`` (zero behavior change for current callers); with a
    populated hook, the hook is wrapped in a signature adapter and forwarded as
    the matching ``fastmcp.Client`` keyword argument — the construction-time slot
    P1 fills once correlation lands (see :mod:`clio_agent.tools.mcp_handlers`).

    Args:
        target: A FastMCP transport / server object (passed straight to the
            client); a CLIO raw ``{transport, command, args, url, env}`` mapping
            spec (resolved via
            :func:`clio_agent.tools.mcp_config.transport_from_spec`); or a native
            FastMCP ``MCPConfig`` mapping (``{"mcpServers": ...}`` or a rootless
            server map), passed unchanged so ``Client`` builds its
            ``MCPConfigTransport``.
        handlers: Optional CLIO hook bundle. ``None`` (or a bundle whose hooks
            are all ``None``) yields a bare client.
        client_cls: Injection seam for the client class. Defaults to
            ``fastmcp.Client``; tests substitute a fake to inspect the
            construction without spawning a real backend.

    Returns:
        A constructed (not yet entered) FastMCP client for ``target``.

    Raises:
        ValueError: If ``target`` is a mapping that is neither a CLIO raw spec
            (scalar ``transport`` key) nor a FastMCP ``MCPConfig`` (``mcpServers``
            key or a rootless server map).
    """

    if isinstance(target, Mapping):
        target = _normalize_mapping_target(target)

    if client_cls is None:
        from fastmcp import Client  # noqa: PLC0415

        client_cls = Client

    if handlers is None:
        return client_cls(target)

    kwargs: dict[str, Any] = {}
    if handlers.elicitation is not None:
        kwargs["elicitation_handler"] = ElicitationDispatcher(handlers.elicitation)
    if handlers.progress is not None:
        kwargs["progress_handler"] = ProgressDispatcher(handlers.progress)
    if handlers.message is not None:
        kwargs["message_handler"] = MessageMultiplexer(handlers.message)
    # `cancellation` has no fastmcp Client keyword today; P1 owns its wiring.

    if not kwargs:
        return client_cls(target)

    return client_cls(target, **kwargs)


def _normalize_mapping_target(target: Mapping[str, Any]) -> Any:
    """Resolve a mapping ``target`` to a transport, or pass a native MCPConfig.

    A CLIO raw spec is recognized ONLY when the top-level ``transport`` value has
    the scalar transport shape (a *string* naming a transport); such specs go
    through :func:`clio_agent.tools.mcp_config.transport_from_spec`. A ``transport``
    key whose value is a *mapping* is a native FastMCP ``MCPConfig`` rootless
    server that merely happens to be named ``transport`` — not a CLIO spec. Native
    ``MCPConfig`` mappings (an ``mcpServers`` wrapper, or a rootless map of server
    configs — values with ``command``/``url``) are returned unchanged so
    ``fastmcp.Client`` builds its own ``MCPConfigTransport``. Anything else is an
    explicit error rather than a silent mis-parse.
    """

    if isinstance(target.get("transport"), str):
        from clio_agent.tools.mcp_config import transport_from_spec  # noqa: PLC0415

        return transport_from_spec(target)
    if "mcpServers" in target or _is_rootless_mcp_config(target):
        return target
    raise ValueError(
        "ambiguous MCP client target mapping: expected a CLIO raw spec (with a "
        "scalar 'transport' key) or a FastMCP MCPConfig (with 'mcpServers' or a "
        f"rootless map of server configs); got keys {sorted(target)!r}"
    )


def _is_rootless_mcp_config(target: Mapping[str, Any]) -> bool:
    """Whether ``target`` is a rootless FastMCP MCPConfig (server map at root).

    Mirrors FastMCP's own ``MCPConfig.wrap_servers_at_root`` heuristic: at least
    one value is a mapping carrying a ``command`` or ``url`` key.
    """

    return any(
        isinstance(value, Mapping) and ("command" in value or "url" in value)
        for value in target.values()
    )


__all__ = ["MCPClientHandlers", "WireMode", "make_mcp_client", "wire_value"]
