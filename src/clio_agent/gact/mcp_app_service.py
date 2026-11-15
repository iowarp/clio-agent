"""Which declared services CLIO carries, and what a view is told (MA23).

:mod:`clio_agent.gact.mcp_app_service_declaration` parses a declaration and
takes it off the result. This module decides whether CLIO carries it, builds
the URL and CSP sources a view gets, tracks the open connections of each app
so closing the app closes them, and records every refusal as a typed reason.

CLIO carries a service only when:

* the declaration is well formed (``service_declaration_invalid``);
* the server that opened the view is a stdio server CLIO launched: its
  namespace has a ``stdio`` spec on the session's tool executor
  (``service_server_not_launched``). A remote server's loopback is not this
  machine's;
* the port is not CLIO's own listener (``service_port_is_clio``).

The service URL is on the sandbox origin, so it lives wherever the sandbox
does and is allowed by the sandbox CSP's explicit service sources.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

from clio_agent.gact.mcp_app_sandbox import _sandbox_url
from clio_agent.gact.mcp_app_service_declaration import (
    ServiceRefusal,
    is_loopback_source,
    service_prefix,
)
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.mcp_apps import MCPAppRecord

logger = logging.getLogger(__name__)

#: Every reason the service path can refuse or end with, and what it means.
SERVICE_REASONS: dict[str, str] = {
    "service_declaration_invalid": "the declaration is not {port: 1..65535, token: str}",
    "service_server_not_launched": (
        "the view's server is not a stdio server CLIO launched, so its loopback "
        "is not known to be this machine's"
    ),
    "service_port_is_clio": "the declared port is CLIO's own listener",
    "service_not_declared": "the app's opening result declared no service",
    "service_unreachable": "nothing answered on the declared loopback port",
    "service_declaration_ignored": (
        "a result that does not open a view declared a service; it was removed and ignored"
    ),
    "csp_loopback_source_refused": (
        "the view's resource declared a loopback CSP source; views reach a service "
        "only through CLIO's route"
    ),
    "mcp_app_closed": "the app was closed, so its service route and connections closed",
}

_MESSAGES: dict[str, str] = {
    "service_server_not_launched": (
        "This view's live service is not available: only a server CLIO started on "
        "this machine can provide one."
    ),
    "service_port_is_clio": (
        "This view's live service is not available: it named CLIO's own address."
    ),
    "service_not_declared": "This view has no live service.",
}

#: What a person reads when the view's service does not answer (the proxy and the tab).
SERVICE_DOWN_MESSAGE = (
    "This view's live service is not answering. Its server may have stopped; "
    "reopen the view to start it again."
)
_MESSAGES["service_unreachable"] = SERVICE_DOWN_MESSAGE


def service_error_page(message: str, reason: str) -> str:
    """A small readable page for a document load the proxy cannot serve."""
    text, code = html.escape(message), html.escape(reason)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Service unavailable</title><style>"
        ":root{color-scheme:light dark;font:15px/1.5 system-ui,sans-serif}"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px}"
        "main{max-width:32rem}h1{font-size:1.1rem;margin:0 0 .5rem}"
        "p{margin:0 0 .5rem;opacity:.85}code{font-size:.8rem;opacity:.6}"
        f"</style></head><body><main><h1>Service unavailable</h1><p>{text}</p>"
        f"<code>{code}</code></main></body></html>"
    )


async def service_answers(record: Any) -> bool:
    """Whether something accepts a connection on the service's loopback port now."""
    service = getattr(record, "service", None)
    if service is None:
        return False
    try:
        # The proxy's own connect bound (mcp_app_service_proxy); loopback answers at once.
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", service.port), timeout=10.0
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


_RECORDED: deque[dict[str, Any]] = deque(maxlen=256)
_RECORDED_LOCK = threading.Lock()


def record_service_reason(reason: str, **fields: Any) -> dict[str, Any]:
    """Record one typed service reason: log, stream audit and a queryable ring."""

    detail = SERVICE_REASONS.get(reason)
    if detail is None:
        raise ValueError(f"Unknown MCP App service reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, "detail": detail, **fields}
    with _RECORDED_LOCK:
        _RECORDED.append(payload)
    stream_audit("mcp_app_service", **payload)
    logger.warning("MCP App service: reason=%s %s fields=%s", reason, detail, fields)
    return payload


def recorded_service_reasons() -> list[dict[str, Any]]:
    """Return a snapshot of the recorded service reasons."""

    with _RECORDED_LOCK:
        return list(_RECORDED)


def refusal(reason: str) -> ServiceRefusal:
    """The person-facing refusal for ``reason``."""

    return ServiceRefusal(reason, _MESSAGES.get(reason, SERVICE_REASONS[reason]))


async def service_refusal(
    app: FastAPI, record: MCPAppRecord, clio_port: int | None
) -> ServiceRefusal | None:
    """Return why CLIO does not carry ``record``'s service, or ``None`` to carry it.

    The launched-server check runs once per record; its verdict is kept on the
    service. A failure to reach the executor is not cached.
    """

    from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415 - module cycle

    service = getattr(record, "service", None)
    if service is None:
        return refusal("service_not_declared")
    if not service.checked:

        def transport(executor: Any) -> str:
            specs = getattr(executor, "_clio_namespace_specs", None) or {}
            spec = specs.get(record.source_namespace or "")
            return str(getattr(spec, "transport", "") or "") if spec is not None else ""

        try:
            kind = await _run_bound(app, record.session_id, transport)
        except RuntimeError as exc:
            record_service_reason(
                "service_server_not_launched",
                app_instance_id=record.app_instance_id,
                error=str(exc),
            )
            return refusal("service_server_not_launched")
        service.checked = True
        if kind != "stdio":
            service.refusal = refusal("service_server_not_launched")
            record_service_reason(
                "service_server_not_launched",
                app_instance_id=record.app_instance_id,
                source_server=record.source_namespace or "",
                transport=kind or "not a configured server",
            )
    if service.refusal is not None:
        return service.refusal
    if clio_port is not None and service.port == clio_port:
        record_service_reason("service_port_is_clio", app_instance_id=record.app_instance_id)
        return refusal("service_port_is_clio")
    return None


def _listener_port(request: Request) -> int | None:
    server = request.scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) == 2 and isinstance(server[1], int):
        return server[1]
    return request.url.port


async def service_descriptor_fields(
    app: FastAPI, request: Request, record: MCPAppRecord
) -> dict[str, Any]:
    """The descriptor's ``service_url``/``service_refusal``/``csp_refused`` fields."""

    refused = [
        {"directive": key, "source": source, "reason": "csp_loopback_source_refused"}
        for key in ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")
        for source in (record.csp.get(key) or [])
        if isinstance(source, str) and is_loopback_source(source)
    ]
    for row in refused:
        record_service_reason(
            "csp_loopback_source_refused",
            app_instance_id=record.app_instance_id,
            directive=row["directive"],
            source=row["source"],
        )
    fields: dict[str, Any] = {"service_url": None, "service_refusal": None, "csp_refused": refused}
    if getattr(record, "service", None) is None:
        return fields
    denied = await service_refusal(app, record, _listener_port(request))
    if denied is None and not await service_answers(record):
        record_service_reason("service_unreachable", app_instance_id=record.app_instance_id)
        denied = refusal("service_unreachable")  # the tab says so instead of "Ready"
    if denied is not None:
        fields["service_refusal"] = denied.to_wire()
        return fields
    prefix = service_prefix(record.session_id, record.app_instance_id, record.data_ref)
    fields["service_url"] = _sandbox_url(request, f"{prefix}/", namespace=record.source_namespace)
    return fields


async def service_csp_sources(app: FastAPI, request: Request, record: MCPAppRecord) -> list[str]:
    """The sandbox CSP sources for this app's service, on the sandbox's own origin."""

    if getattr(record, "service", None) is None:
        return []
    if await service_refusal(app, record, _listener_port(request)) is not None:
        return []
    prefix = service_prefix(record.session_id, record.app_instance_id, record.data_ref)
    scheme = request.url.scheme
    socket_scheme = "wss" if scheme == "https" else "ws"
    netloc = request.url.netloc
    return [f"{scheme}://{netloc}{prefix}/", f"{socket_scheme}://{netloc}{prefix}/"]


Closer = Callable[[], Awaitable[None]]


class ServiceConnections:
    """The open proxied connections of each app, closed when the app closes."""

    def __init__(self) -> None:
        self._by_app: dict[str, set[Closer]] = {}

    def add(self, app_instance_id: str, closer: Closer) -> None:
        """Track one open connection."""

        self._by_app.setdefault(app_instance_id, set()).add(closer)

    def discard(self, app_instance_id: str, closer: Closer) -> None:
        """Forget a connection that ended on its own."""

        closers = self._by_app.get(app_instance_id)
        if closers is not None:
            closers.discard(closer)
            if not closers:
                self._by_app.pop(app_instance_id, None)

    def count(self, app_instance_id: str) -> int:
        """The number of open connections for one app."""

        return len(self._by_app.get(app_instance_id, ()))

    async def close_app(self, app_instance_id: str) -> int:
        """Close every open connection of one app; return how many there were."""

        closers = self._by_app.pop(app_instance_id, set())
        for closer in list(closers):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - close every connection
                logger.exception(
                    "MCP App service connection close failed app_instance_id=%s",
                    app_instance_id,
                )
        return len(closers)


def service_connections(app: FastAPI) -> ServiceConnections:
    """The app-wide connection tracker."""

    tracker = getattr(app.state, "mcp_app_service_connections", None)
    if not isinstance(tracker, ServiceConnections):
        tracker = ServiceConnections()
        app.state.mcp_app_service_connections = tracker
    return tracker


async def close_app_service(app: FastAPI, app_instance_id: str) -> None:
    """Close an app's service: its route is already gone; end its connections."""

    closed = await service_connections(app).close_app(app_instance_id)
    if closed:
        record_service_reason("mcp_app_closed", app_instance_id=app_instance_id, closed=closed)


__all__ = [
    "SERVICE_REASONS",
    "ServiceConnections",
    "close_app_service",
    "record_service_reason",
    "recorded_service_reasons",
    "refusal",
    "service_connections",
    "service_csp_sources",
    "service_descriptor_fields",
    "service_refusal",
]
