"""Tell the application which namespace proxies a workspace fleet stopped routing to.

A call through a fleet's namespace proxy is made on a connection the application keeps
beyond the turn (``gact.external_task_connections``, keyed by the proxy). That retained
connection holds the proxy's server process open, so a fleet the reaper closed, or a
namespace a connector change rebound to a new proxy, would leave its server running
with nothing able to reach it (CV-N2: an idle ParaView front outliving its fleet). The
places that drop proxies call :func:`proxies_dropped`; the application listens with
:func:`listen` and retires the connections to them (typed
``external_mcp_connection_fleet_closed``).

The other way round, a retained connection can still carry work the server accepted
(an MCP task it is running, or a call waiting on its reply) after every turn ended. The
fleet is then in use even though no call runs through it, and closing it would kill the
process doing that work. The application says so with :func:`hold`, and the reaper asks
:func:`fleet_held` before it treats a fleet as idle.

This module only carries the notice; it owns no connection.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any

from clio_agent.runtime import trace

#: ``listener(root, proxies, reason)``; called on the thread that dropped them.
Listener = Callable[[str, tuple[Any, ...], str], None]

#: ``holder(proxies)``: whether accepted work still runs on a connection to one of them.
Holder = Callable[[tuple[Any, ...]], bool]

_LOCK = threading.Lock()
_LISTENERS: list[Listener] = []
_HOLDERS: list[Holder] = []


def hold(holder: Holder) -> Callable[[], None]:
    """Register ``holder``; return the function that removes it."""

    with _LOCK:
        _HOLDERS.append(holder)

    def remove() -> None:
        with _LOCK:
            if holder in _HOLDERS:
                _HOLDERS.remove(holder)

    return remove


def fleet_held(executor: Any) -> bool:
    """Whether accepted work still runs on one of a fleet's proxies.

    Called on the reaper's thread. A holder that fails cannot prove the fleet idle, so
    the fleet counts as held (typed).
    """

    proxies = fleet_proxies(executor)
    if not proxies:
        return False
    with _LOCK:
        holders = tuple(_HOLDERS)
    for holder in holders:
        try:
            if holder(proxies):
                return True
        except Exception as exc:  # noqa: BLE001 - can't prove idle: keep the fleet
            trace.event("TOOLS", "fleet_hold_probe_failed error=%s", type(exc).__name__)
            return True
    return False


def listen(listener: Listener) -> Callable[[], None]:
    """Register ``listener``; return the function that removes it."""

    with _LOCK:
        _LISTENERS.append(listener)

    def remove() -> None:
        with _LOCK:
            if listener in _LISTENERS:
                _LISTENERS.remove(listener)

    return remove


def fleet_proxies(executor: Any) -> tuple[Any, ...]:
    """The namespace proxies a fleet executor routes to."""

    inner = getattr(executor, "_async_executor", executor)
    servers = getattr(inner, "_namespace_servers", None)
    return tuple(servers.values()) if isinstance(servers, dict) else ()


def proxies_dropped(root: str, proxies: Iterable[Any], reason: str, *, by: Any = None) -> None:
    """Notify every listener that ``proxies`` of fleet ``root`` are no longer routed to.

    With ``by`` (the executor that dropped them), a proxy another open fleet still
    routes to is left out: that fleet shares its server (MI1) and keeps it.
    """

    dropped = tuple(proxy for proxy in proxies if proxy is not None)
    if by is not None:
        from clio_agent.tools import namespace_instances  # noqa: PLC0415

        dropped = namespace_instances.unshared(dropped, by)
    if not dropped:
        return
    with _LOCK:
        listeners = tuple(_LISTENERS)
    for listener in listeners:
        try:
            listener(root, dropped, reason)
        except RuntimeError as exc:  # its loop closed while scheduling: never breaks a close
            trace.event(
                "TOOLS",
                "fleet_close_listener_failed root=%s reason=%s error=%s",
                root,
                reason,
                type(exc).__name__,
            )


def fleet_closed(root: str, executor: Any, reason: str) -> None:
    """:func:`proxies_dropped` for every proxy of a fleet that was just closed."""

    proxies_dropped(root, fleet_proxies(executor), reason, by=executor)
