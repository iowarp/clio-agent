"""One live server per connector per workspace folder (MI1), except for open views.

A connector's stdio server keeps its state in its workspace folder: a ParaView scene and
its node lock, a notebook kernel, a database session. Two live processes of it in one
folder fight. Seen live: a route heal started a direct server beside the proxy's
kept-alive one, and the view the first one drew went "Service unavailable"; a
connector-values change started a new fleet's server beside the old fleet's, and its
``open_view`` failed with "another node half is running in this folder".

Every stdio namespace proxy a gateway builds for a folder is registered here
(:func:`register`) with its launch: the command, arguments, environment and folder the
server starts with. Fleets of one folder differ only by the connector values a
conversation set (CV-D3), so most of their servers launch the same way. Before an
executor starts a namespace's server through its proxy P, :func:`adopt` routes it to
a sibling fleet's proxy of the same launch that an open fleet already routes to: one
server serves both fleets, and a values change of one connector never restarts the
others (seen live, take 1p: setting ParaView's ``pvserver`` started a new fleet whose
first relay call stopped the running relay and its ssh forward). Then :func:`claim`
stops that connector's server behind every other registered proxy of the folder whose
launch differs (or that no open fleet routes to any more): each executor routing to
it loses its namespace connection, and the application's kept-alive direct
connection (:func:`add_stopper`) and the proxy's own kept-alive backend stop. Each
stop bumps those executors' instance generation, so the views that server drew close
with Reopen (``gact.mcp_app_fleet``) instead of pointing at a dead service. A fleet
that closes or rebinds a namespace leaves a proxy another open fleet still routes to
running (:func:`unshared`).

An open view outranks "one live server": a superseded server that still draws open
App views is retained (typed ``mcp_namespace_instance_retained``), so for as long as
those views are open the folder runs two servers of that connector, the retained one
for its views and the new one for every other call. Setting a connector value must
not close the person's open view (take 1s, gap 15). The view's own calls keep going
to the fleet that drew it. :func:`retire_superseded` stops the retained server once
its last view has closed, or its session ended (typed reason
``superseded_views_closed``). When a call to the new server fails while a retained one
runs, the executor names the open views holding it (:func:`holding_views`, typed
``mcp_namespace_instance_held``) after the new server's own failure, never before it:
the retained server may hold an exclusive resource (a node lock, a port), but most such
failures are the new server's own, and a note first cut their cause off (W17, 2026-11).
CLIO neither waits silently for the views to close nor stops them.

Within one proxy, the proxy route's kept-alive backend and the direct route are two
processes of the same server. :func:`may_go_direct` keeps the proxy route while that
backend has open views or calls in flight (typed ``mcp_namespace_route_switch_deferred``)
and otherwise stops it before the direct server starts.

This module owns no connection; executors and the application register how theirs stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any

from clio_agent.runtime import trace
from clio_agent.tools import call_origin

MCP_NAMESPACE_INSTANCE_STOPPED = "mcp_namespace_instance_stopped"
MCP_NAMESPACE_INSTANCE_SHARED = "mcp_namespace_instance_shared"
MCP_NAMESPACE_INSTANCE_KEPT = "mcp_namespace_instance_kept"
MCP_NAMESPACE_ROUTE_SWITCH_DEFERRED = "mcp_namespace_route_switch_deferred"
MCP_NAMESPACE_INSTANCE_RETAINED = "mcp_namespace_instance_retained"
#: The stop reason of a retained superseded server once its views have closed.
SUPERSEDED_VIEWS_CLOSED = "superseded_views_closed"

#: ``stopper(proxies, reason)`` retires what the application keeps open through those
#: proxies; returns how many connections it matched.
Stopper = Callable[[tuple[Any, ...], str], Awaitable[int]]
#: ``probe(executor, namespace)``: why that namespace's server is in use ("" when not).
InUseProbe = Callable[[Any, str], str]
#: ``namer(executor, namespace)``: labels of the open views that server draws.
ViewNamer = Callable[[Any, str], list[str]]

_LOCK = threading.Lock()
_PROXIES: dict[tuple[str, str], weakref.WeakSet[Any]] = {}
_STOPPERS: list[tuple[asyncio.AbstractEventLoop, Stopper]] = []
_PROBES: list[InUseProbe] = []
_NAMERS: list[ViewNamer] = []
#: Every executor that started a namespace through a registered proxy.
_EXECUTORS: weakref.WeakSet[Any] = weakref.WeakSet()
#: Superseded proxies kept for their open views, with the loop their stop runs on.
_RETAINED: weakref.WeakKeyDictionary[Any, asyncio.AbstractEventLoop] = weakref.WeakKeyDictionary()
#: The proxy the latest :func:`claim` of each (folder, namespace) started.
_CURRENT: dict[tuple[str, str], weakref.ref[Any]] = {}
#: Retirement stops scheduled (kept referenced until done).
_RETIRING: set[Any] = set()

_KEY = "_clio_instance_key"
_LAUNCH = "_clio_instance_launch"
_TRANSPORT = "_clio_backend_transport"
_OWNER = "_clio_instance_owner"
_GENERATIONS = "_clio_instance_generations"
_STOPS_BEGUN = "_clio_instance_stops_begun"
_PROXY_CALLS = "_clio_proxy_route_calls"


def _folder(root: str) -> str:
    return os.path.normcase(os.path.abspath(root))


def launch_key(spec: Any, root: str | None) -> str:
    """How ``spec``'s stdio server starts in ``root``: two equal keys are one server."""

    env = getattr(spec, "env", None) or {}
    return json.dumps(
        [
            getattr(spec, "transport", None),
            getattr(spec, "command", None),
            [str(arg) for arg in getattr(spec, "args", None) or ()],
            sorted((str(name), str(value)) for name, value in env.items()),
            getattr(spec, "url", None),
            _folder(root) if root else "",
        ],
        default=str,
    )


def register(
    proxy: Any, namespace: str, root: str | None, transport: Any, launch: str | None = None
) -> None:
    """Record that ``proxy`` runs ``namespace``'s stdio server in folder ``root``.

    ``launch`` (:func:`launch_key`) lets a fleet share a sibling's server of the same
    launch; without it the proxy is never shared.
    """

    if not root or proxy is None:
        return
    key = (_folder(root), namespace)
    try:
        setattr(proxy, _KEY, key)
        setattr(proxy, _TRANSPORT, transport)
        setattr(proxy, _LAUNCH, launch)
    except (AttributeError, TypeError):  # a proxy that takes no attributes is not tracked
        return
    with _LOCK:
        _PROXIES.setdefault(key, weakref.WeakSet()).add(proxy)


def _inner(executor: Any) -> Any:
    return getattr(executor, "_async_executor", executor)


def routers(proxy: Any, *, exclude: Any = None) -> list[Any]:
    """The open executors (other than ``exclude``) that route a namespace to ``proxy``."""

    skip = _inner(exclude) if exclude is not None else None
    with _LOCK:
        tracked = tuple(_EXECUTORS)
    return [
        executor
        for executor in tracked
        if executor is not skip
        and not getattr(executor, "_closed", False)
        and any(
            routed is proxy
            for routed in (getattr(executor, "_namespace_servers", None) or {}).values()
        )
    ]


def unshared(proxies: Iterable[Any], by: Any) -> tuple[Any, ...]:
    """Those of ``proxies`` no open executor but ``by`` routes to: safe to retire.

    A proxy another fleet still routes to keeps its server (typed
    ``mcp_namespace_instance_kept``).
    """

    free: list[Any] = []
    for proxy in proxies:
        if proxy is None:
            continue
        others = routers(proxy, exclude=by)
        if not others:
            free.append(proxy)
            continue
        key = getattr(proxy, _KEY, ("-", "-"))
        trace.event(
            "TOOLS",
            "%s namespace=%s folder=%s routers=%d",
            MCP_NAMESPACE_INSTANCE_KEPT,
            key[1],
            key[0],
            len(others),
        )
    return tuple(free)


def adopt(executor: Any, namespace: str, proxy: Any) -> Any:
    """The proxy ``executor`` should start ``namespace`` through: ``proxy``, or the
    proxy of the same launch in this folder that another open fleet routes to.

    Adopting routes the executor's namespace there (typed
    ``mcp_namespace_instance_shared``): its calls reach the running server and its
    kept-alive connection, so a sibling fleet's values change never restarts it.
    """

    key = getattr(proxy, _KEY, None)
    launch = getattr(proxy, _LAUNCH, None)
    if key is None or launch is None or routers(proxy, exclude=executor):
        return proxy
    with _LOCK:
        twins = [
            other
            for other in _PROXIES.get(key, ())
            if other is not proxy and getattr(other, _LAUNCH, None) == launch
        ]
    for twin in twins:
        if routers(twin, exclude=executor):
            executor._namespace_servers[namespace] = twin
            _bind(executor, twin)
            trace.event(
                "TOOLS",
                "%s namespace=%s folder=%s",
                MCP_NAMESPACE_INSTANCE_SHARED,
                namespace,
                key[0],
            )
            return twin
    return proxy


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


def add_view_namer(namer: ViewNamer) -> Callable[[], None]:
    """Register a reader of the open views a namespace's server draws, by label."""

    return _listen(_NAMERS, namer)


def generation(executor: Any, namespace: str) -> int:
    """How many times ``executor`` stopped ``namespace``'s server (views stamp it)."""

    inner = getattr(executor, "_async_executor", executor)
    return int(getattr(inner, "__dict__", {}).get(_GENERATIONS, {}).get(namespace, 0))


def stops_begun(executor: Any, namespace: str) -> int:
    """How many stops of ``namespace``'s server behind ``executor`` have begun.

    Counted before the stop closes anything, so a call cut off by it (its reply is the
    closed transport's error) can tell, even before :func:`generation` moves.
    """

    inner = getattr(executor, "_async_executor", executor)
    return int(getattr(inner, "__dict__", {}).get(_STOPS_BEGUN, {}).get(namespace, 0))


def _bump(executor: Any, namespace: str, counter: str = "") -> None:
    generations = executor.__dict__.setdefault(counter or _GENERATIONS, {})
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


def proxy_calls_in_flight(namespace: str) -> int:
    """How many calls are in flight on ``namespace``'s proxy route, over every fleet."""

    with _LOCK:
        tracked = tuple(_EXECUTORS)
    return sum(
        int(executor.__dict__.get(_PROXY_CALLS, {}).get(namespace, 0)) for executor in tracked
    )


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


def _owners(proxy: Any) -> list[Any]:
    """The open executors routing to ``proxy``, and the one that started it."""

    owners = routers(proxy)
    owner_ref = getattr(proxy, _OWNER, None)
    owner = owner_ref() if owner_ref is not None else None
    if owner is not None and not getattr(owner, "_closed", False) and owner not in owners:
        owners.append(owner)
    return owners


def _views(proxy: Any, namespace: str) -> str:
    """Why ``proxy``'s server still draws a view ("" when none): the probes only.

    A call in flight is not a reason to keep a superseded server: it gets the typed
    stop error and is retried on the new one.
    """

    with _LOCK:
        probes = tuple(_PROBES)
    for executor in _owners(proxy):
        for probe in probes:
            try:
                reason = probe(executor, namespace)
            except Exception as exc:  # noqa: BLE001 - an unreadable probe keeps nothing
                trace.event("TOOLS", "mcp_namespace_in_use_probe_failed error=%r", exc)
                continue
            if reason:
                return reason
    return ""


def holding_views(proxy: Any) -> list[str]:
    """Labels of the open views a retained superseded server of ``proxy``'s connector
    and folder still draws (empty when none is retained beside ``proxy``)."""

    key = getattr(proxy, _KEY, None)
    if key is None:
        return []
    with _LOCK:
        retained = [other for other in _RETAINED if other is not proxy]
        namers = tuple(_NAMERS)
    labels: list[str] = []
    for other in retained:
        if getattr(other, _KEY, None) != key or _is_current(other):
            continue
        for executor in _owners(other):
            for namer in namers:
                try:
                    found = namer(executor, key[1])
                except Exception as exc:  # noqa: BLE001 - an unreadable namer names nothing
                    trace.event("TOOLS", "mcp_namespace_view_namer_failed error=%r", exc)
                    continue
                labels.extend(label for label in found if label not in labels)
    return labels


def retained_owners() -> set[str]:
    """The connection owner tags (``in-process:<id>``) of the retained superseded proxies."""

    with _LOCK:
        return {f"in-process:{id(proxy):x}" for proxy in _RETAINED}


def _is_current(proxy: Any) -> bool:
    key = getattr(proxy, _KEY, None)
    with _LOCK:
        ref = _CURRENT.get(key) if key is not None else None
    return ref is not None and ref() is proxy


def retire_superseded() -> int:
    """Stop each retained superseded server whose last view has closed.

    Called whenever a view leaves (closed, cleaned up, expired, or its session
    ended). A retained server that became current again, or whose fleet closed, is
    forgotten. Returns how many stops were scheduled.
    """

    with _LOCK:
        retained = list(_RETAINED.items())
    scheduled = 0
    for proxy, loop in retained:
        key = getattr(proxy, _KEY, ("-", "-"))
        if _is_current(proxy) or not (_owners(proxy) or backend_live(proxy)):
            with _LOCK:
                _RETAINED.pop(proxy, None)
            continue
        if _views(proxy, key[1]):
            continue
        with _LOCK:
            if _RETAINED.pop(proxy, None) is None:
                continue  # another close already scheduled it
        if loop.is_closed():
            continue
        coro = _retire(proxy)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            future: Any = loop.create_task(coro)
        else:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        _RETIRING.add(future)
        future.add_done_callback(_RETIRING.discard)
        scheduled += 1
    return scheduled


async def _retire(proxy: Any) -> int:
    key = getattr(proxy, _KEY, ("-", "-"))
    if _is_current(proxy):
        return 0
    if _views(proxy, key[1]):  # a view drew on it again before the stop ran
        with _LOCK:
            _RETAINED[proxy] = asyncio.get_running_loop()
        return 0
    try:
        return await stop_instance(proxy, SUPERSEDED_VIEWS_CLOSED)
    except (RuntimeError, OSError) as exc:  # typed: the process may outlive this stop
        trace.event(
            "TOOLS",
            "mcp_namespace_backend_stop_failed namespace=%s reason=%s error=%r",
            key[1],
            SUPERSEDED_VIEWS_CLOSED,
            exc,
        )
        return 0


async def _on(loop: asyncio.AbstractEventLoop | None, coro: Coroutine[Any, Any, int]) -> int:
    """Run ``coro`` on ``loop`` (where its connections live) and wait for it."""

    running = asyncio.get_running_loop()
    if loop is None or loop is running:
        return await coro
    if loop.is_closed() or not loop.is_running():
        # A retired executor's private loop that stopped without closing would never
        # run ``coro``: waiting on it held the reconcile (and the fleet) forever.
        coro.close()
        trace.event("TOOLS", "mcp_namespace_stop_loop_idle closed=%s", loop.is_closed())
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
    owners = routers(proxy)
    if owner is not None and not getattr(owner, "_closed", False) and owner not in owners:
        owners.append(owner)
    if owner is None and owners:
        owner = owners[0]
    for routed in owners or ([owner] if owner is not None else []):
        _bump(routed, namespace, _STOPS_BEGUN)
    stopped = 0
    for routed in owners:
        stopped += await _on(
            routed.__dict__.get("_clio_loop"),
            _release_executor(routed, namespace, proxy, direct=direct),
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
        for routed in owners or ([owner] if owner is not None else []):
            _bump(routed, namespace)
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
    with _LOCK:
        _EXECUTORS.add(executor)
    try:
        setattr(proxy, _OWNER, weakref.ref(executor))
    except (AttributeError, TypeError):
        return


async def claim(executor: Any, namespace: str, proxy: Any) -> int:
    """Before ``executor`` starts ``namespace`` through ``proxy``: stop its other servers.

    Every other registered proxy of the same folder and connector whose server may
    run has it stopped (:func:`stop_instance`, reason ``superseded``), unless it
    launches the same way and an open fleet still routes to it: that one is the same
    server, shared through :func:`adopt`, and is never stopped for a sibling's change.
    One whose server still draws open App views is retained instead (typed
    ``mcp_namespace_instance_retained``) and stopped by :func:`retire_superseded`
    once those views close. Returns how many connections were stopped.
    """

    key = getattr(proxy, _KEY, None)
    if key is None:
        return 0
    _bind(executor, proxy)
    loop = asyncio.get_running_loop()
    launch = getattr(proxy, _LAUNCH, None)
    if call_origin.current() == "view":
        with _LOCK:
            drawing = proxy in _RETAINED
        if drawing:  # a view's call on the server that drew it: the current one stays
            return 0
    with _LOCK:
        _CURRENT[key] = weakref.ref(proxy)
        _RETAINED.pop(proxy, None)
        others = [other for other in _PROXIES.get(key, ()) if other is not proxy]
    stopped = 0
    for other in others:
        if launch is not None and getattr(other, _LAUNCH, None) == launch and routers(other):
            continue
        owner_ref = getattr(other, _OWNER, None)
        owner = owner_ref() if owner_ref is not None else None
        if (owner is None or getattr(owner, "_closed", False)) and not routers(other):
            if not backend_live(other):
                continue
        reason = _views(other, namespace)
        if reason:
            with _LOCK:
                known = other in _RETAINED
                _RETAINED[other] = loop
            if not known:
                trace.event(
                    "TOOLS",
                    "%s namespace=%s folder=%s reason=%s",
                    MCP_NAMESPACE_INSTANCE_RETAINED,
                    namespace,
                    key[0],
                    reason,
                )
            continue
        with _LOCK:
            _RETAINED.pop(other, None)
        stopped += await stop_instance(other, "superseded")
    return stopped


async def may_go_direct(executor: Any, namespace: str, proxy: Any) -> bool:
    """Whether ``namespace`` may start its direct server now beside ``proxy``'s backend.

    The proxy route's kept-alive backend is the same server: with open views or calls
    in flight on it (through any fleet routing to it), the route stays (typed
    deferral, retried on a later use); otherwise it is stopped first so one process
    serves the namespace.
    """

    if not backend_live(proxy):
        return True
    reason = in_use(executor, namespace)
    for routed in routers(proxy, exclude=executor):
        if reason:
            break
        reason = in_use(routed, namespace)
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
    "MCP_NAMESPACE_INSTANCE_KEPT",
    "MCP_NAMESPACE_INSTANCE_RETAINED",
    "MCP_NAMESPACE_INSTANCE_SHARED",
    "MCP_NAMESPACE_INSTANCE_STOPPED",
    "MCP_NAMESPACE_ROUTE_SWITCH_DEFERRED",
    "SUPERSEDED_VIEWS_CLOSED",
    "add_in_use_probe",
    "add_stopper",
    "add_view_namer",
    "adopt",
    "backend_live",
    "begin_proxy_call",
    "claim",
    "generation",
    "holding_views",
    "in_use",
    "launch_key",
    "may_go_direct",
    "proxy_calls_in_flight",
    "register",
    "retained_owners",
    "retire_superseded",
    "routers",
    "stop_instance",
    "stop_pooled",
    "stops_begun",
    "unshared",
]
