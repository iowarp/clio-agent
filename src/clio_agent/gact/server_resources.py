"""A mounted server's resources for the UI: read one, and watch it for updates.

Something the UI keeps after its task ends, such as a job's card, can follow the
server's own record of it. The session reads a resource of any server it mounts,
and while a holder watches the URI, each ``notifications/resources/updated`` for it
reaches the session's live stream as :data:`UPDATED` (``namespace``, ``uri``), so the
UI reads it again. A server that cannot push updates still answers reads, and the
watch says so (``pushed`` false). Nothing here names a particular server.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.tools import fleet_close_listeners

logger = logging.getLogger(__name__)

UPDATED = "mcp.resource.updated"
SERVER_UNAVAILABLE = "mcp_resource_server_unavailable"
RESOURCE_FAILED = "mcp_resource_failed"

#: Why a fleet dropped a server, when the server is mounted again at once.
REMOUNT_REASONS = frozenset(
    {"configuration_switched", "namespace_rebound", "restart", "grant_restart"}
)

#: ``(session id, namespace, uri)``
WatchKey = tuple[str, str, str]


class ServerResourceError(RuntimeError):
    """A typed refusal of a resource request (HTTP ``status`` with ``reason``)."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(message)


@dataclass
class Watch:
    """The holders watching one URI in one session, and its subscription."""

    holders: set[str] = field(default_factory=set)
    subscribed: bool = False
    #: The mounted server the subscription was made on; a remount drops it.
    proxy: Any = None


def _mounted(app: Any, session_id: str, namespace: str) -> bool:
    from clio_agent.gact.agents.prompt_basis import mounted_servers  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    try:
        rows = mounted_servers(app, session_id, session_resolution(app, session_id))
    except SessionAgentUnavailable:
        return False
    return isinstance(rows, Mapping) and namespace in rows


class ServerResources:
    """The app's watched server resources, per session."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._lock = threading.RLock()
        self._watches: dict[WatchKey, Watch] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._remove_listener: Any = None
        self._remove_drop_listener: Any = None

    def _require(self, session_id: str, namespace: str) -> None:
        if not _mounted(self._app, session_id, namespace):
            raise ServerResourceError(
                404, SERVER_UNAVAILABLE, f"server {namespace!r} is not mounted in this session"
            )

    async def read(self, session_id: str, namespace: str, uri: str) -> dict[str, Any]:
        """One ``resources/read`` of ``uri`` on the session's ``namespace``, on the wire."""

        from clio_agent.gact.mcp_apps import (  # noqa: PLC0415
            _run_bound,
            read_resource_result_to_wire,
        )
        from clio_agent.gact.plugin_tabs import PluginTabError, _prepare_server  # noqa: PLC0415

        self._require(session_id, namespace)

        def read(executor: Any) -> Any:
            _prepare_server(executor, namespace)
            return executor.read_resource(namespace, uri)

        try:
            result = await _run_bound(self._app, session_id, read)
        except PluginTabError as exc:
            raise ServerResourceError(503, SERVER_UNAVAILABLE, str(exc)) from exc
        except ValueError as exc:
            if "unknown MCP namespace" in str(exc):
                raise ServerResourceError(503, SERVER_UNAVAILABLE, str(exc)) from exc
            raise ServerResourceError(502, RESOURCE_FAILED, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a failed read is a typed answer
            raise ServerResourceError(502, RESOURCE_FAILED, f"{type(exc).__name__}: {exc}") from exc
        return read_resource_result_to_wire(result)

    async def watch(
        self, session_id: str, namespace: str, uri: str, holder: str, *, watching: bool
    ) -> dict[str, Any]:
        """Start or stop ``holder``'s watch of ``uri``; the URI is watched while any holds it."""

        self._ensure_listener()
        key: WatchKey = (session_id, namespace, uri)
        if not watching:
            with self._lock:
                held = self._watches.get(key)
                if held is None:
                    return {"watching": False, "pushed": False}
                held.holders.discard(holder)
                if held.holders:
                    return {"watching": False, "pushed": held.subscribed}
                self._watches.pop(key, None)
                shared = any(k[1:] == key[1:] for k in self._watches)
            if held.subscribed and not shared:
                await self._subscribe(key, held, subscribed=False)
            return {"watching": False, "pushed": False}
        self._require(session_id, namespace)
        with self._lock:
            held = self._watches.setdefault(key, Watch())
            held.holders.add(holder)
            need = not held.subscribed
        if need:
            pushed = await self._subscribe(key, held, subscribed=True)
            with self._lock:
                held.subscribed = pushed and self._watches.get(key) is held
        return {"watching": True, "pushed": held.subscribed}

    async def _subscribe(self, key: WatchKey, held: Watch, *, subscribed: bool) -> bool:
        from mcp.shared.exceptions import MCPError  # noqa: PLC0415

        from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415

        session_id, namespace, uri = key

        def change(executor: Any) -> bool:
            method = getattr(executor, "set_resource_subscription", None)
            if method is None:
                return False
            done = bool(method(namespace, uri, subscribed=subscribed))
            inner = getattr(executor, "_async_executor", executor)
            servers = getattr(inner, "_namespace_servers", None)
            with self._lock:
                proxy = servers.get(namespace) if isinstance(servers, Mapping) else None
                held.proxy = proxy if done and subscribed else None
            return done

        try:
            return await _run_bound(self._app, session_id, change)
        except (MCPError, OSError, RuntimeError, ValueError, LookupError):
            # Without a subscription the resource still reads; it just isn't pushed changes.
            logger.warning(
                "resource subscription change failed namespace=%s uri=%s subscribed=%s",
                namespace,
                uri,
                subscribed,
                exc_info=True,
            )
            return False

    # -- updates -----------------------------------------------------------------

    def _publish(self, session_ids: list[str], namespace: str, uri: str) -> None:
        bus = getattr(self._app.state, "bus", None)
        if bus is None:
            return
        for session_id in dict.fromkeys(session_ids):
            bus.publish(
                Event(
                    type=UPDATED,
                    session_id=session_id,
                    payload={"namespace": namespace, "uri": uri},
                )
            )

    def _on_updated(self, namespace: str, uri: str) -> None:
        """A ``notifications/resources/updated`` (on the client's loop): hand off only."""

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            sessions = [k[0] for k in self._watches if k[1:] == (namespace, uri)]
        if sessions:
            loop.call_soon_threadsafe(self._publish, sessions, namespace, uri)

    def _on_proxies_dropped(self, root: str, proxies: tuple[Any, ...], reason: str) -> None:
        """A fleet stopped routing to ``proxies``: the watches on them lost their server."""

        del root
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            keys = [
                key
                for key, held in self._watches.items()
                if any(held.proxy is proxy for proxy in proxies)
            ]
            for key in keys:
                self._watches[key].subscribed = False
                self._watches[key].proxy = None
        if keys and reason in REMOUNT_REASONS:
            loop.call_soon_threadsafe(self._start_rewatch, keys)

    def _start_rewatch(self, keys: list[WatchKey]) -> None:
        for key in keys:
            task = asyncio.ensure_future(self._rewatch(key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _rewatch(self, key: WatchKey) -> None:
        """Watch again on the server mounted now, and tell the UI to read again."""

        with self._lock:
            held = self._watches.get(key)
        if held is None:
            return
        pushed = await self._subscribe(key, held, subscribed=True)
        with self._lock:
            if self._watches.get(key) is held:
                held.subscribed = pushed
        # The update may have landed while the server was away.
        self._publish([key[0]], key[1], key[2])

    def _ensure_listener(self) -> None:
        from clio_agent.tools.mcp_resource_updates import add_listener  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        if self._remove_listener is None:
            ref = weakref.ref(self)
            remove: list[Any] = []

            def listener(namespace: str, uri: str) -> None:
                resources = ref()
                if resources is None:
                    if remove:
                        remove.pop()()
                    return
                resources._on_updated(namespace, uri)

            self._remove_listener = add_listener(listener)
            remove.append(self._remove_listener)
        if self._remove_drop_listener is None:
            ref_drop = weakref.ref(self)
            remove_drop: list[Any] = []

            def dropped(root: str, proxies: tuple[Any, ...], reason: str) -> None:
                resources = ref_drop()
                if resources is None:
                    if remove_drop:
                        remove_drop.pop()()
                    return
                resources._on_proxies_dropped(root, proxies, reason)

            self._remove_drop_listener = fleet_close_listeners.listen(dropped)
            remove_drop.append(self._remove_drop_listener)

    def shutdown(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        if self._remove_drop_listener is not None:
            self._remove_drop_listener()
            self._remove_drop_listener = None
        for task in list(self._tasks):
            task.cancel()
        with self._lock:
            self._watches.clear()


def server_resources(app: Any) -> ServerResources:
    """The app's server resource registry, created on first use."""

    resources = getattr(app.state, "server_resources", None)
    if not isinstance(resources, ServerResources):
        resources = ServerResources(app)
        app.state.server_resources = resources
    return resources


__all__ = [
    "RESOURCE_FAILED",
    "SERVER_UNAVAILABLE",
    "UPDATED",
    "ServerResourceError",
    "ServerResources",
    "server_resources",
]
