"""One live server per connector per workspace folder (MI1).

A connector's stdio server keeps its state in its workspace folder: a ParaView scene and
its node lock, a notebook kernel, a database session. Two live processes of it in one
folder fight. Seen live: a route heal started a direct server beside the proxy's
kept-alive one, and the view the first one drew went "Service unavailable"; a
connector-values change started a new fleet's server beside the old fleet's, and its
``open_view`` failed with "another node half is running in this folder".

Every stdio namespace proxy a gateway builds for a folder is registered here
(:func:`register`). Before an executor starts a namespace's server through proxy P,
:func:`claim` stops that connector's server behind every other registered proxy of the
same folder: the owning executor's namespace connection, the application's kept-alive
direct connection (:func:`add_stopper`) and the proxy's own kept-alive backend. Each
stop bumps the owner's instance generation, so the views that server drew close with
Reopen (``gact.mcp_app_fleet``) instead of pointing at a dead service.

Within one proxy, the proxy route's kept-alive backend and the direct route are two
processes of the same server. :func:`may_go_direct` keeps the proxy route while that
backend has open views or calls in flight (typed ``mcp_namespace_route_switch_deferred``)
and otherwise stops it before the direct server starts.

This module owns no connection; executors and the application register how theirs stop.
"""

from __future__ import annotations

import asyncio
import os
import threading
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any

from clio_agent.runtime import trace

MCP_NAMESPACE_INSTANCE_STOPPED = "mcp_namespace_instance_stopped"
MCP_NAMESPACE_ROUTE_SWITCH_DEFERRED = "mcp_namespace_route_switch_deferred"

#: ``stopper(proxies, reason)`` retires what the application keeps open through those
#: proxies; returns how many connections it matched.
Stopper = Callable[[tuple[Any, ...], str], Awaitable[int]]
#: ``probe(executor, namespace)``: why that namespace's server is in use ("" when not).
InUseProbe = Callable[[Any, str], str]

_LOCK = threading.Lock()
_PROXIES: dict[tuple[str, str], weakref.WeakSet[Any]] = {}
_STOPPERS: list[tuple[asyncio.AbstractEventLoop, Stopper]] = []
_PROBES: list[InUseProbe] = []

_KEY = "_clio_instance_key"
_TRANSPORT = "_clio_backend_transport"
_OWNER = "_clio_instance_owner"
_GENERATIONS = "_clio_instance_generations"
_PROXY_CALLS = "_clio_proxy_route_calls"


def _folder(root: str) -> str:
    return os.path.normcase(os.path.abspath(root))


def register(proxy: Any, namespace: str, root: str | None, transport: Any) -> None:
    """Record that ``proxy`` runs ``namespace``'s stdio server in folder ``root``."""

    if not root or proxy is None:
        return
    key = (_folder(root), namespace)
    try:
        setattr(proxy, _KEY, key)
        setattr(proxy, _TRANSPORT, transport)
    except (AttributeError, TypeError):  # a proxy that takes no attributes is not tracked
        return
    with _LOCK:
        _PROXIES.setdefault(key, weakref.WeakSet()).add(proxy)


def _listen(registry: list[Any], entry: Any) -> Callable[[], None]:
    with _LOCK:
        registry.append(entry)

    def remove() -> None:
        with _LOCK:
            if entry in registry:
                registry.remove(entry)

    return remove


def add_stopper(loop: asyncio.AbstractEventLoop, stopper: Stopper) -> Callable[[], None]:
    """Register how the application retires connections through proxies, on ``loop``."""

    return _listen(_STOPPERS, (loop, stopper))


def add_in_use_probe(probe: InUseProbe) -> Callable[[], None]:
    """Register a reader of what keeps a namespace's server in use (open views)."""

    return _listen(_PROBES, probe)


def generation(executor: Any, namespace: str) -> int:
    """How many times ``executor`` stopped ``namespace``'s server (views stamp it)."""

    inner = getattr(executor, "_async_executor", executor)
    return int(getattr(inner, "__dict__", {}).get(_GENERATIONS, {}).get(namespace, 0))


def _bump(executor: Any, namespace: str) -> None:
    generations = executor.__dict__.setdefault(_GENERATIONS, {})
    generations[namespace] = generations.get(namespace, 0) + 1


def backend_live(proxy: Any) -> bool:
    """Whether the proxy route's kept-alive backend process is running."""

    task = getattr(getattr(proxy, _TRANSPORT, None), "_connect_task", None)
    return task is not None and not task.done()


def begin_proxy_call(executor: Any, namespace: str) -> Callable[[], None]:
    """Count a call in flight on ``namespace``'s proxy route; return its end."""

    calls = executor.__dict__.setdefault(_PROXY_CALLS, {})
    calls[namespace] = calls.get(namespace, 0) + 1

    def end() -> None:
        calls[namespace] = max(0, calls.get(namespace, 0) - 1)

    return end


def in_use(executor: Any, namespace: str) -> str:
    """Why ``namespace``'s server behind ``executor`` must keep running ("" if it need not)."""

    if executor.__dict__.get(_PROXY_CALLS, {}).get(namespace, 0):
        return "calls_in_flight"
    with _LOCK:
        probes = tuple(_PROBES)
    for probe in probes:
        try:
            reason = probe(executor, namespace)
        except Exception as exc:  # noqa: BLE001 - an unreadable probe never blocks a call
            trace.event("TOOLS", "mcp_namespace_in_use_probe_failed error=%r", exc)
            continue
        if reason:
            return reason
    return ""


async def _on(loop: asyncio.AbstractEventLoop | None, coro: Coroutine[Any, Any, int]) -> int:
    """Run ``coro`` on ``loop`` (where its connections live) and wait for it."""

    running = asyncio.get_running_loop()
    if loop is None or loop is running:
        return await coro
    if loop.is_closed():
        coro.close()
        return 0
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


async def stop_pooled(proxies: Iterable[Any], reason: str) -> int:
    """Retire the application's kept-alive connections through ``proxies``."""

    dropped = tuple(proxy for proxy in proxies if proxy is not None)
    if not dropped:
        return 0
    with _LOCK:
        stoppers = tuple(_STOPPERS)
    matched = 0
    for loop, stopper in stoppers:
        try:
            matched += await _on(loop, _call(stopper, dropped, reason))
        except (RuntimeError, OSError) as exc:  # its loop closed: nothing left to retire there
            trace.event("TOOLS", "mcp_namespace_stopper_failed reason=%s error=%r", reason, exc)
    return matched


async def _call(stopper: Stopper, proxies: tuple[Any, ...], reason: str) -> int:
    return await stopper(proxies, reason)


async def stop_instance(proxy: Any, reason: str, *, direct: bool = True) -> int:
    """Stop ``proxy``'s server: its executor's connection, pooled ones and its backend.

    ``direct=False`` stops only the proxy route's process (a route heal keeps the
    direct connection it is switching to).
    """

    key = getattr(proxy, _KEY, None)
    if key is None:
        return 0
    namespace = key[1]
    owner_ref = getattr(proxy, _OWNER, None)
    owner = owner_ref() if owner_ref is not None else None
    stopped = 0
    if owner is not None and not getattr(owner, "_closed", False):
        stopped += await _on(
            owner.__dict__.get("_clio_loop"),
            _release_executor(owner, namespace, proxy, direct=direct),
        )
    if direct:
        stopped += await stop_pooled((proxy,), reason)
    if backend_live(proxy):
        from clio_agent.tools.mcp_client_lifecycle import (  # noqa: PLC0415
            disconnect_owned_transport,
        )

        loop = owner.__dict__.get("_clio_loop") if owner is not None else None
        try:
            await _on(loop, _disconnect(disconnect_owned_transport, getattr(proxy, _TRANSPORT)))
            stopped += 1
        except (RuntimeError, OSError) as exc:  # typed: the process may outlive this stop
            trace.event(
                "TOOLS",
                "mcp_namespace_backend_stop_failed namespace=%s error=%r",
                namespace,
                exc,
            )
    if stopped:
        if owner is not None:
            _bump(owner, namespace)
        trace.event(
            "TOOLS",
            "%s namespace=%s folder=%s reason=%s stopped=%d",
            MCP_NAMESPACE_INSTANCE_STOPPED,
            namespace,
            key[0],
            reason,
            stopped,
        )
    return stopped


async def _disconnect(disconnect: Callable[[Any], Awaitable[None]], transport: Any) -> int:
    await disconnect(transport)
    return 1


async def _release_executor(executor: Any, namespace: str, proxy: Any, *, direct: bool) -> int:
    """Close ``executor``'s connection of ``namespace`` when it is to ``proxy``'s server."""

    if executor._namespace_servers.get(namespace) is not proxy:
        return 0
    if namespace not in executor._namespace_clients:
        return 0
    if not direct and executor._namespace_direct_routes.get(namespace, False):
        return 0
    await executor._evict_namespace_client(namespace)
    executor._connected_namespaces.discard(namespace)
    executor._namespace_heal_attempted.discard(namespace)
    return 1


def _bind(executor: Any, proxy: Any) -> None:
    executor.__dict__["_clio_loop"] = asyncio.get_running_loop()
    try:
        setattr(proxy, _OWNER, weakref.ref(executor))
    except (AttributeError, TypeError):
        return


async def claim(executor: Any, namespace: str, proxy: Any) -> int:
    """Before ``executor`` starts ``namespace`` through ``proxy``: stop its other servers.

    Every other registered proxy of the same folder and connector whose executor is
    still open has its server stopped (:func:`stop_instance`, reason ``superseded``).
    Returns how many connections were stopped.
    """

    key = getattr(proxy, _KEY, None)
    if key is None:
        return 0
    _bind(executor, proxy)
    with _LOCK:
        others = [other for other in _PROXIES.get(key, ()) if other is not proxy]
    stopped = 0
    for other in others:
        owner_ref = getattr(other, _OWNER, None)
        owner = owner_ref() if owner_ref is not None else None
        if owner is None or getattr(owner, "_closed", False):
            if not backend_live(other):
                continue
        stopped += await stop_instance(other, "superseded")
    return stopped


async def may_go_direct(executor: Any, namespace: str, proxy: Any) -> bool:
    """Whether ``namespace`` may start its direct server now beside ``proxy``'s backend.

    The proxy route's kept-alive backend is the same server: with open views or calls
    in flight on it, the route stays (typed deferral, retried on a later use); otherwise
    it is stopped first so one process serves the namespace.
    """

    if not backend_live(proxy):
        return True
    reason = in_use(executor, namespace)
    if reason:
        trace.event(
            "TOOLS",
            "%s namespace=%s reason=%s",
            MCP_NAMESPACE_ROUTE_SWITCH_DEFERRED,
            namespace,
            reason,
        )
        return False
    _bind(executor, proxy)
    await stop_instance(proxy, "route_heal", direct=False)
    return True


__all__ = [
    "MCP_NAMESPACE_INSTANCE_STOPPED",
    "MCP_NAMESPACE_ROUTE_SWITCH_DEFERRED",
    "add_in_use_probe",
    "add_stopper",
    "backend_live",
    "begin_proxy_call",
    "claim",
    "generation",
    "in_use",
    "may_go_direct",
    "register",
    "stop_instance",
    "stop_pooled",
]
