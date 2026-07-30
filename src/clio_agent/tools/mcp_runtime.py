"""Shared helpers for MCP runtime pathways.

Three historical wire contracts are preserved as explicit :func:`wire_value`
modes: ``mcp_results``, ``mcp_apps``, and ``gact_runtime``. Converging those
contracts is future wire-work, not part of this slice.

This module also owns :func:`make_mcp_client` (#1106) — the ONE construction
site for **execution-path** FastMCP clients. It carries the :class:`MCPClientHandlers`
slot where P1 attaches elicitation/progress/message/cancellation handlers (no-op
slots today). Execution paths route through it: the ``AsyncMCPToolExecutor``
default ``client_factory``, the per-call dispatch in ``gact/routes/mcp.py``, and
the ``providers/handshake/mcp.py`` connectivity probe.

**The execution/introspection split (adopted default):** handlers wire on
execution paths only, so **list-only introspection sites do NOT migrate** and
keep their bare ``Client()`` — the catalog/blueprint/status/gateway listing
passes (``routes/catalog.py``, ``routes/blueprints.py``, ``agents/builders.py``,
``runtime/status.py``, ``tools/gateway.py``) plus the install/reconnect/inventory
``list_tools`` passes in ``routes/mcp.py``. They never dispatch a tool call, so
they never need the handler slot; forcing them through the factory would be pure
churn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

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
    """Handler bundle attached to an execution-path MCP client.

    Every field is a no-op slot today (``None`` => nothing wired, identical to
    a bare client). P1 fills these with real handlers per its own slices; the
    factory is the single seam they attach through so no P1 slice has to hunt
    for client-construction sites.

    ``elicitation``, ``progress``, and ``message`` map directly onto the
    matching ``fastmcp.Client`` handler keyword arguments. ``cancellation`` has
    no FastMCP ``Client`` keyword today — it is held as a slot for P1 to route
    however cancellation lands (e.g. via the message handler) without churning
    this dataclass again.
    """

    elicitation: Any | None = None
    progress: Any | None = None
    message: Any | None = None
    cancellation: Any | None = None


def make_mcp_client(
    target: Any,
    *,
    handlers: MCPClientHandlers | None = None,
    client_cls: Callable[..., Any] | None = None,
) -> Any:
    """Construct an execution-path FastMCP client with the handler slot.

    This is the ONE construction site for clients that actually dispatch MCP
    calls. With no ``handlers`` the construction is byte-identical to a bare
    ``Client(target)`` (zero behavior change for current callers); with a
    handler bundle, the populated handlers are forwarded as the matching
    ``fastmcp.Client`` keyword arguments — the seam P1 fills.

    Args:
        target: Either a FastMCP transport/server object (passed straight to
            the client) or a raw ``{transport, command, args, url, env}``
            mapping spec, which is resolved via
            :func:`clio_agent.tools.mcp_config.transport_from_spec`.
        handlers: Optional handler bundle. ``None`` (or a bundle whose fields
            are all ``None``) yields a bare client.
        client_cls: Injection seam for the client class. Defaults to
            ``fastmcp.Client``; tests substitute a fake to inspect the
            construction without spawning a real backend.

    Returns:
        A constructed (not yet entered) FastMCP client for ``target``.
    """

    if isinstance(target, Mapping):
        from clio_agent.tools.mcp_config import transport_from_spec  # noqa: PLC0415

        target = transport_from_spec(target)

    if client_cls is None:
        from fastmcp import Client  # noqa: PLC0415

        client_cls = Client

    kwargs: dict[str, Any] = {}
    if handlers is not None:
        if handlers.elicitation is not None:
            kwargs["elicitation_handler"] = handlers.elicitation
        if handlers.progress is not None:
            kwargs["progress_handler"] = handlers.progress
        if handlers.message is not None:
            kwargs["message_handler"] = handlers.message
        # `cancellation` has no fastmcp Client keyword today; P1 owns its wiring.

    return client_cls(target, **kwargs)


__all__ = ["MCPClientHandlers", "WireMode", "make_mcp_client", "wire_value"]
