"""Plugin tabs: a server's A2UI resource shown as a tab (host semantics HS3).

A plugin declares ``tabs`` in its ``ai.iowarp.clio`` extension, each naming one of its
servers and an ``a2ui://`` resource. A session lists the tabs of the plugin servers its
agent mounts. Its ``navigation`` page is a tab too, opened only in the page's
host-owned context (:mod:`~clio_agent.gact.plugin_pages`). Opening a tab reads the
resource; its A2UI contents are the tab's whole
state. CLIO then watches the URI (a ``subscriptions/listen`` stream, or the server's
unsolicited pushes), and each ``notifications/resources/updated`` for it re-reads it.
Actions on a tab's surfaces go to its server (HS2) and their results apply to the tab.

Tab state is in memory, per session, outside the transcript, and never seen by the
model. Nothing here names a particular plugin or server.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.gact.a2ui import (
    A2UISurfaceRecord,
    A2UIValidationError,
    apply_batch,
    validate_client_action,
)
from clio_agent.gact.events import Event
from clio_agent.gact.plugin_page_startup import page_startups
from clio_agent.gact.plugin_tab_revisions import carry_revisions, new_lifecycle
from clio_agent.gact.server_actions import (
    ServerActionError,
    deliver_server_action,
    login_context,
    login_provider,
)
from clio_agent.gact.server_instructions_refresh import refresh_server, refresh_soon
from clio_agent.gact.server_surfaces import (
    PayloadOutcome,
    ServerScope,
    ServerSurfaceRefusal,
    a2ui_resources,
    is_a2ui_contents,
    payload_messages,
    publish_refusal,
    refusal_of,
    result_blocks,
    server_scope,
)
from clio_agent.tools import fleet_close_listeners, request_locale

logger = logging.getLogger(__name__)

SURFACE_UPSERTED = "plugin_tab.surface.upserted"
SURFACE_DELETED = "plugin_tab.surface.deleted"
REFRESHED = "plugin_tab.refreshed"
UNAVAILABLE = "plugin_tab.unavailable"

SERVER_UNAVAILABLE = "plugin_tab_server_unavailable"
RESOURCE_FAILED = "plugin_tab_resource_failed"

#: Why a fleet dropped a server, when the server is mounted again at once. ``grant_restart``
#: is a deferred restart (a changed configuration or plugin snapshot) the reaper drained.
REMOUNT_REASONS = frozenset(
    {"configuration_switched", "namespace_rebound", "restart", "grant_restart"}
)

#: ``(session id, plugin id, tab id)``
TabKey = tuple[str, str, str]
Surfaces = dict[tuple[str, str], A2UISurfaceRecord]


class PluginTabError(RuntimeError):
    """A typed refusal of a tab request (HTTP ``status`` with ``reason``)."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(message)


def _prepare_server(executor: Any, namespace: str) -> None:
    """Start the tab's server the way a turn does before the tab reads it.

    A page runs no turn, and its context starts a warm-up only when it is created, so a
    read could otherwise reach a server nothing prepared. On Windows that launches the
    ``uvx`` build inside the fence, where it cannot run. Joining the server's one
    preparation also makes a read during a slow first start wait for it, not fail.
    """

    from clio_agent.gact.mcp_background_prepare import prepare_for_turn  # noqa: PLC0415

    prepared = getattr(executor, "is_namespace_prepared", None)
    if callable(prepared) and prepared(namespace):
        return
    spec = (getattr(executor, "_clio_namespace_specs", None) or {}).get(namespace)
    if spec is None:
        return
    failures = prepare_for_turn(executor, {namespace: spec})
    if namespace in failures:
        raise PluginTabError(
            503, SERVER_UNAVAILABLE, f"server {namespace!r} did not start: {failures[namespace]}"
        )


@dataclass
class TabState:
    """One open tab in one session."""

    session_id: str
    plugin: str
    tab: str
    declared: dict[str, Any]
    scope: ServerScope
    surfaces: Surfaces = field(default_factory=dict)
    revision: int = 0
    reason: str = ""
    detail: str = ""
    subscribed: bool = False
    #: The mounted server the watch was started on; a remount drops it (HS3).
    watch_proxy: Any = None
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The person's language when the tab was opened, so a pushed re-read keeps it.
    locale: str | None = None

    @property
    def tab_id(self) -> str:
        return f"{self.plugin}/{self.tab}"

    @property
    def watch_key(self) -> tuple[str, str]:
        return (self.scope.namespace, str(self.declared["resource"]))

    def live(self) -> list[A2UISurfaceRecord]:
        return [record for record in self.surfaces.values() if record.state != "deleted"]


def declared_tabs(app: Any, session_id: str) -> list[dict[str, Any]]:
    """The tabs of every plugin server the session's agent mounts, in mount order."""

    from clio_agent.gact.agents.prompt_basis import mounted_servers  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    try:
        rows = mounted_servers(app, session_id, session_resolution(app, session_id))
    except SessionAgentUnavailable:
        return []
    tabs: list[dict[str, Any]] = []
    for namespace, row in rows.items():
        if not isinstance(row, Mapping) or not row.get("plugin"):
            continue
        plugin = str(row["plugin"])
        host = row.get("host") or {}
        # A plugin's navigation page is a tab the host shows in its primary navigation.
        entries = [(tab, "tab") for tab in host.get("tabs") or ()]
        if host.get("navigation"):
            entries.append((host["navigation"], "primary"))
        for tab, placement in entries:
            declared = {
                "id": f"{plugin}/{tab['id']}",
                "plugin": plugin,
                "tab": str(tab["id"]),
                "title": str(tab.get("title") or tab["id"]),
                "titles": dict(tab.get("titles") or {}),
                "icon": str(tab.get("icon") or ""),
                "server": str(namespace),
                "resource": str(tab["resource"]),
                "placement": placement,
            }
            if host.get("title"):
                declared["plugin_title"] = str(host["title"])
            tabs.append(declared)
    return tabs


def _tab_wire(declared: Mapping[str, Any], *, reason: str = "") -> dict[str, Any]:
    keys = ("id", "plugin", "icon", "server", "resource")
    row = {key: declared[key] for key in keys}
    row["title"] = request_locale.localized(declared.get("titles"), str(declared["title"]))
    row["placement"] = declared.get("placement", "tab")
    if declared.get("plugin_title"):
        row["plugin_title"] = declared["plugin_title"]
    row["available"] = reason not in (SERVER_UNAVAILABLE, RESOURCE_FAILED)
    row["reason"] = reason
    return row


def _contents(result: Any) -> list[Any]:
    from clio_agent.gact.mcp_apps import read_resource_result_to_wire  # noqa: PLC0415

    contents = read_resource_result_to_wire(result).get("contents")
    return list(contents) if isinstance(contents, list) else []


class PluginTabs:
    """Every open tab of one app, with its subscriptions."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._lock = threading.RLock()
        self._open: dict[TabKey, TabState] = {}
        self._failures: dict[TabKey, tuple[str, str]] = {}
        self._handles: dict[str, TabKey] = {}
        self._refreshing: dict[TabKey, bool] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._remove_listener: Any = None
        self._remove_drop_listener: Any = None

    # -- listing -----------------------------------------------------------------

    def list(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            failures = dict(self._failures)
        return [
            _tab_wire(
                tab,
                reason=failures.get((session_id, tab["plugin"], tab["tab"]), ("", ""))[0],
            )
            for tab in declared_tabs(self._app, session_id)
            if tab.get("placement", "tab") == "tab"
        ]

    def failure(self, session_id: str, plugin: str, tab: str) -> str:
        """Why the open tab's last read failed ("" when it did not)."""

        with self._lock:
            return self._failures.get((session_id, plugin, tab), ("", ""))[0]

    def failure_detail(self, session_id: str, plugin: str, tab: str) -> str:
        """The message of the open tab's last failed read ("" when it did not fail)."""

        with self._lock:
            return self._failures.get((session_id, plugin, tab), ("", ""))[1]

    def _declared(self, session_id: str, plugin: str, tab: str) -> dict[str, Any]:
        from clio_agent.gact.plugin_pages import page_context  # noqa: PLC0415

        for row in declared_tabs(self._app, session_id):
            if row["plugin"] != plugin or row["tab"] != tab:
                continue
            # A navigation page opens only in its own host-owned context.
            primary = row.get("placement", "tab") != "tab"
            if not primary or page_context(self._app, session_id) == (plugin, tab):
                return row
        raise PluginTabError(404, "plugin_tab_not_found", f"no tab {plugin}/{tab} in this session")

    # -- events ------------------------------------------------------------------

    def _publish(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        from clio_agent.gact.plugin_pages import page_event  # noqa: PLC0415

        bus = getattr(self._app.state, "bus", None)
        if bus is not None:
            event_type, payload = page_event(self._app, session_id, event_type, payload)
            bus.publish(Event(type=event_type, session_id=session_id, payload=payload))

    def _publish_changes(
        self, state: TabState, before: Surfaces, after: Surfaces, *, refreshed: bool
    ) -> None:
        """Emit one event per surface change, then ``plugin_tab.refreshed`` for a re-read."""

        live_before = {key for key, row in before.items() if row.state != "deleted"}
        for key, record in after.items():
            old = before.get(key)
            if record.state == "deleted":
                if key in live_before:
                    state.revision += 1
                    self._publish(
                        state.session_id,
                        SURFACE_DELETED,
                        {"tab_id": state.tab_id, "revision": state.revision, "surface_id": key[1]},
                    )
                continue
            if old is not None and old.revision == record.revision and not refreshed:
                continue
            state.revision += 1
            self._publish(
                state.session_id,
                SURFACE_UPSERTED,
                {"tab_id": state.tab_id, "revision": state.revision, "surface": record.to_wire()},
            )
        for key in live_before - set(after):
            state.revision += 1
            self._publish(
                state.session_id,
                SURFACE_DELETED,
                {"tab_id": state.tab_id, "revision": state.revision, "surface_id": key[1]},
            )
        if refreshed:
            self._publish(
                state.session_id,
                REFRESHED,
                {
                    "tab_id": state.tab_id,
                    "revision": state.revision,
                    "surface_ids": [record.id for record in state.live()],
                },
            )

    def _unavailable(self, key: TabKey, reason: str, detail: str) -> None:
        with self._lock:
            self._failures[key] = (reason, detail)
        self._publish(
            key[0],
            UNAVAILABLE,
            {"tab_id": f"{key[1]}/{key[2]}", "reason": reason, "detail": detail},
        )

    # -- reading -----------------------------------------------------------------

    def _fold_contents(self, state: TabState, contents: list[Any]) -> tuple[Surfaces, str, str]:
        """The tab's surfaces from one ``resources/read``, or ``({}, reason, detail)``."""

        from clio_agent.gact.a2ui_catalogs.activation import (  # noqa: PLC0415
            session_catalog_resolver,
        )

        surfaces: Surfaces = {}
        uri = str(state.declared["resource"])
        lifecycle = new_lifecycle()
        first = True
        for item in contents:
            if not is_a2ui_contents(item):
                continue
            outcome = PayloadOutcome(uri=str(item.get("uri") or uri))
            try:
                messages = payload_messages(item)
                if first and "createSurface" not in messages[0]:
                    raise ServerSurfaceRefusal(
                        "a2ui_validation_failed",
                        "a tab's first A2UI batch must start with createSurface",
                    )
                surfaces, _ = apply_batch(
                    surfaces,
                    state.session_id,
                    messages,
                    catalogs=session_catalog_resolver(self._app, state.session_id),
                    producible=state.scope.producible,
                    owner=state.scope.owner,
                    part_id=lifecycle,
                )
            except ServerSurfaceRefusal as exc:
                outcome.reason, outcome.detail = exc.reason, exc.detail
            except A2UIValidationError as exc:
                refusal = refusal_of(exc)
                outcome.reason, outcome.detail = refusal.reason, refusal.detail
            if outcome.reason:
                publish_refusal(self._app, state.session_id, state.scope, tool="", outcome=outcome)
                return {}, outcome.reason, outcome.detail
            first = False
        return surfaces, "", ""

    async def _read(self, state: TabState) -> tuple[Surfaces, str, str]:
        from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415
        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        namespace, uri = state.watch_key
        locale = request_locale.current() or state.locale
        startups = page_startups(self._app)

        def read(executor: Any) -> Any:
            _prepare_server(executor, namespace)
            startups.server_ready(state.session_id, state.plugin, state.tab)
            return executor.read_resource(namespace, uri)

        try:
            with request_locale.scope(locale):
                result = await _run_bound(self._app, state.session_id, read)
        except PluginTabError:
            raise
        except ValueError as exc:
            if "unknown MCP namespace" in str(exc):
                raise PluginTabError(503, SERVER_UNAVAILABLE, str(exc)) from exc
            raise PluginTabError(502, RESOURCE_FAILED, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - forwards: a failed read is the tab's typed state
            raise PluginTabError(502, RESOURCE_FAILED, f"{type(exc).__name__}: {exc}") from exc
        return await run_off_loop(self._fold_contents, state, _contents(result))

    async def _subscribe(self, state: TabState, *, subscribed: bool) -> bool:
        from mcp.shared.exceptions import MCPError  # noqa: PLC0415

        from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415

        namespace, uri = state.watch_key

        def change(executor: Any) -> bool:
            method = getattr(executor, "set_resource_subscription", None)
            if method is None:
                return False
            held = bool(method(namespace, uri, subscribed=subscribed))
            inner = getattr(executor, "_async_executor", executor)
            servers = getattr(inner, "_namespace_servers", None)
            with self._lock:
                held_on = servers.get(namespace) if isinstance(servers, Mapping) else None
                state.watch_proxy = held_on if held else None
            return held

        try:
            return await _run_bound(self._app, state.session_id, change)
        except (MCPError, OSError, RuntimeError, ValueError, LookupError):
            # Without a subscription the tab still opens; it just isn't pushed changes.
            logger.warning(
                "resource subscription change failed namespace=%s uri=%s subscribed=%s",
                namespace,
                uri,
                subscribed,
                exc_info=True,
            )
            return False

    # -- open, close, refresh ----------------------------------------------------

    async def open(self, session_id: str, plugin: str, tab: str) -> dict[str, Any]:
        self._ensure_listener()
        declared = self._declared(session_id, plugin, tab)
        key: TabKey = (session_id, plugin, tab)
        scope = server_scope(self._app, session_id, declared["server"])
        if scope is None:
            detail = f"server {declared['server']!r} is not mounted in this session"
            self._unavailable(key, SERVER_UNAVAILABLE, detail)
            return {
                "tab": _tab_wire(declared, reason=SERVER_UNAVAILABLE),
                "surfaces": [],
                "revision": 0,
            }
        with self._lock:
            state = self._open.get(key)
            if state is None:
                state = TabState(session_id, plugin, tab, declared, scope)
                self._open[key] = state
            state.locale = request_locale.current() or state.locale
        try:
            surfaces, reason, detail = await self._read(state)
        except PluginTabError as exc:
            self._unavailable(key, exc.reason, str(exc))
            with self._lock:
                # The records stay, so the tab's next read keeps their revisions rising.
                state.reason, state.detail = exc.reason, str(exc)
            return {
                "tab": _tab_wire(declared, reason=exc.reason),
                "surfaces": [],
                "revision": state.revision,
            }
        with self._lock:
            self._failures.pop(key, None)
            surfaces = carry_revisions(state.surfaces, surfaces)
            state.surfaces, state.reason, state.detail = surfaces, reason, detail
            state.revision += 1
            if self._open.get(key) is None:
                # A close finished while this open was reading. The client's requests race
                # on separate connections (a remount sends open, close, open), and this open
                # answers last, so the tab is open: keep it, and watch it again.
                self._open[key] = state
                state.subscribed = False
            need_subscribe = not state.subscribed
        if need_subscribe:
            # Per tab, on the session's own bound executor: an executor holds one watch
            # per URI, so a client shared by several sessions watches it once.
            state.subscribed = await self._subscribe(state, subscribed=True)
        return {
            "tab": _tab_wire(declared, reason=reason),
            "surfaces": [record.to_wire() for record in state.live()],
            "revision": state.revision,
        }

    async def close(self, session_id: str, plugin: str, tab: str) -> dict[str, Any]:
        key: TabKey = (session_id, plugin, tab)
        with self._lock:
            state = self._open.pop(key, None)
            self._failures.pop(key, None)
            for handle in [h for h, k in self._handles.items() if k == key]:
                self._handles.pop(handle, None)
            last = (
                state is not None
                and state.subscribed
                and not any(other.watch_key == state.watch_key for other in self._open.values())
            )
        if last and state is not None:
            await self._subscribe(state, subscribed=False)
            with self._lock:
                reopened = self._open.get(key)
            if reopened is not None:
                # An open answered while this watch was being dropped: watch it again.
                reopened.subscribed = await self._subscribe(reopened, subscribed=True)
        return {"closed": True}

    async def refresh(self, key: TabKey) -> None:
        """Re-read one open tab and replace its surfaces; coalesces overlapping updates."""

        with self._lock:
            if key not in self._open:
                return
            if self._refreshing.get(key) is not None:
                self._refreshing[key] = True
                return
            self._refreshing[key] = False
        try:
            while True:
                with self._lock:
                    state = self._open.get(key)
                if state is None:
                    return
                try:
                    surfaces, reason, detail = await self._read(state)
                except PluginTabError as exc:
                    self._unavailable(key, exc.reason, str(exc))
                else:
                    with self._lock:
                        if self._open.get(key) is state:
                            self._failures.pop(key, None)
                            before = state.surfaces
                            surfaces = carry_revisions(before, surfaces)
                            state.surfaces, state.reason, state.detail = surfaces, reason, detail
                            self._publish_changes(state, before, surfaces, refreshed=True)
                with self._lock:
                    again = self._refreshing.get(key)
                    if not again:
                        self._refreshing.pop(key, None)
                        return
                    self._refreshing[key] = False
        except BaseException:
            with self._lock:
                self._refreshing.pop(key, None)
            raise

    def _on_updated(self, namespace: str, uri: str) -> None:
        """A ``notifications/resources/updated`` (on the client's loop): hand off only."""

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            keys = [key for key, state in self._open.items() if state.watch_key == (namespace, uri)]
        if keys:
            loop.call_soon_threadsafe(self._start_refreshes, keys)
            # What the server reaches may have changed with what it shows.
            refresh_soon(self._app, namespace, loop)

    def _start_refreshes(self, keys: list[TabKey]) -> None:
        for key in keys:
            task = asyncio.ensure_future(self.refresh(key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _on_proxies_dropped(self, root: str, proxies: tuple[Any, ...], reason: str) -> None:
        """A fleet stopped routing to ``proxies`` (on the dropping thread): hand off only.

        A tab watching one of them lost its watch with that server. A remount (a changed
        configuration or plugin, a rebound connector, a restart) watches the server
        mounted now; a reap only forgets the watch, and the tab's next ``open`` starts it.
        """

        del root
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            keys = [
                key
                for key, state in self._open.items()
                if any(state.watch_proxy is proxy for proxy in proxies)
            ]
            for key in keys:
                self._open[key].subscribed = False
                self._open[key].watch_proxy = None
        if keys and reason in REMOUNT_REASONS:
            loop.call_soon_threadsafe(self._start_rebinds, keys)

    def session_agent_changed(self, session_id: str) -> None:
        """The session's agent changed: watch each open tab's server as mounted now."""

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            keys = [key for key in self._open if key[0] == session_id]
        if keys:
            loop.call_soon_threadsafe(self._start_rebinds, keys)

    def _start_rebinds(self, keys: list[TabKey]) -> None:
        for key in keys:
            task = asyncio.ensure_future(self.rebind(key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def rebind(self, key: TabKey) -> None:
        """Subscribe an open tab on the session's current executor, then re-read it."""

        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        with self._lock:
            state = self._open.get(key)
        if state is None:
            return
        # The server mounted now may come from an updated plugin (a new title, say).
        scope = await run_off_loop(server_scope, self._app, state.session_id, state.scope.namespace)
        with self._lock:
            if scope is not None and self._open.get(key) is state:
                state.scope = scope
        held = await self._subscribe(state, subscribed=True)
        with self._lock:
            if self._open.get(key) is state:
                state.subscribed = held
        await self.refresh(key)

    def _ensure_listener(self) -> None:
        from clio_agent.tools.mcp_resource_updates import add_listener  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        if self._remove_listener is None:
            ref = weakref.ref(self)
            remove: list[Any] = []

            def listener(namespace: str, uri: str) -> None:
                tabs = ref()
                if tabs is None:
                    if remove:
                        remove.pop()()
                    return
                tabs._on_updated(namespace, uri)

            self._remove_listener = add_listener(listener)
            remove.append(self._remove_listener)
        if self._remove_drop_listener is None:
            ref_drop = weakref.ref(self)
            remove_drop: list[Any] = []

            def dropped(root: str, proxies: tuple[Any, ...], reason: str) -> None:
                tabs = ref_drop()
                if tabs is None:
                    if remove_drop:
                        remove_drop.pop()()
                    return
                tabs._on_proxies_dropped(root, proxies, reason)

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
            self._open.clear()
            self._handles.clear()

    # -- actions -----------------------------------------------------------------

    def _apply_result(self, key: TabKey, raw_result: Any) -> list[PayloadOutcome] | None:
        """Apply a server result's A2UI to the tab (HS2 for tab surfaces)."""

        from clio_agent.gact.a2ui_catalogs.activation import (  # noqa: PLC0415
            session_catalog_resolver,
        )

        resources = a2ui_resources(result_blocks(raw_result))
        if not resources:
            return None
        lifecycle = new_lifecycle()
        outcomes: list[PayloadOutcome] = []
        for resource in resources:
            with self._lock:
                state = self._open.get(key)
            if state is None:
                return outcomes or None
            outcome = PayloadOutcome(uri=str(getattr(resource, "uri", "") or ""))
            try:
                messages = payload_messages(resource)
                with self._lock:
                    before = state.surfaces
                    after, applied = apply_batch(
                        before,
                        state.session_id,
                        messages,
                        catalogs=session_catalog_resolver(self._app, state.session_id),
                        producible=state.scope.producible,
                        owner=state.scope.owner,
                        part_id=lifecycle,
                    )
                    state.surfaces = after
                    touched = {(state.session_id, surface_id) for _, surface_id, _ in applied}
                    self._publish_changes(
                        state,
                        {k: v for k, v in before.items() if k in touched},
                        {k: v for k, v in after.items() if k in touched},
                        refreshed=False,
                    )
                outcome.surface_ids = list(
                    dict.fromkeys(surface_id for _, surface_id, _ in applied)
                )
            except ServerSurfaceRefusal as exc:
                outcome.reason, outcome.detail = exc.reason, exc.detail
            except A2UIValidationError as exc:
                refusal = refusal_of(exc)
                outcome.reason, outcome.detail = refusal.reason, refusal.detail
            if outcome.reason:
                publish_refusal(self._app, state.session_id, state.scope, tool="", outcome=outcome)
            outcomes.append(outcome)
        return outcomes

    async def action(
        self, session_id: str, plugin: str, tab: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Deliver one action on a tab surface to the tab's server."""

        from clio_agent.gact.a2ui_actions.record import (  # noqa: PLC0415
            compute_idempotency_key,
            new_record_id,
        )
        from clio_agent.gact.a2ui_catalogs.activation import (  # noqa: PLC0415
            session_catalog_resolver,
        )

        key: TabKey = (session_id, plugin, tab)
        if set(body) - {"message", "correlation", "metadata"}:
            raise PluginTabError(
                422, "validation_error", "A2UI action body contains unknown fields"
            )
        message = body.get("message")
        if not isinstance(message, Mapping):
            raise PluginTabError(422, "validation_error", "A2UI action message is required")
        raw_action = message.get("action")
        surface_id = (
            str(raw_action.get("surfaceId") or "") if isinstance(raw_action, Mapping) else ""
        )
        with self._lock:
            known = key in self._open
        if not known:
            # The client acts on a tab it shows, so the host lost the open record (a close
            # that raced the client's reopen, or a restart): read the tab again first.
            await self.open(session_id, plugin, tab)
        with self._lock:
            state = self._open.get(key)
            surface = state.surfaces.get((session_id, surface_id)) if state is not None else None
        if state is None:
            reason = self.failure(session_id, plugin, tab)
            if reason:
                raise PluginTabError(409, reason, f"tab {plugin}/{tab} is unavailable")
            raise PluginTabError(404, "plugin_tab_not_open", f"tab {plugin}/{tab} is not open")
        if surface is None or surface.state == "deleted":
            raise PluginTabError(404, "not_found", f"A2UI surface not found: {surface_id}")
        try:
            action = validate_client_action(
                message,
                surface_id=surface_id,
                catalog_entry=session_catalog_resolver(self._app, session_id).get(
                    surface.catalog_id
                ),
            )
        except A2UIValidationError as exc:
            raise PluginTabError(422, "a2ui_validation_failed", str(exc)) from exc
        name = str(action["name"])
        context = dict(action.get("context") or {})
        idempotency_key = compute_idempotency_key(
            surface_id,
            str(action.get("sourceComponentId") or ""),
            str(action.get("timestamp") or ""),
            context,
        )
        with self._lock:
            seen = state.actions.get(idempotency_key)
        if seen is not None:
            return dict(seen)
        action_id = new_record_id()
        provider = login_provider(name)
        try:
            if provider:
                result = {
                    "name": name,
                    "status": "accepted",
                    "action_id": action_id,
                    "state": "consumed",
                    "delivery": "client_ui",
                    "destination": "client",
                    "provider": provider,
                    "context": login_context(self._app, session_id),
                }
            else:
                outcome = await deliver_server_action(
                    self._app,
                    session_id,
                    state.scope,
                    name,
                    context,
                    apply=lambda raw: self._apply_result(key, raw),
                    place="page" if state.declared.get("placement") == "primary" else "tab",
                    place_title=request_locale.localized(
                        state.declared.get("titles"),
                        str(state.declared.get("title") or ""),
                        state.locale,
                    ),
                )
                if outcome.task_handle:
                    with self._lock:
                        self._handles[outcome.task_handle] = key
                result = outcome.wire(action_id)
                # The action may change what the server says it reaches (a site added on
                # a page, say): the next search in a running turn reads it as it is now.
                await refresh_server(self._app, state.scope.namespace)
        except ServerActionError as exc:
            raise PluginTabError(exc.status, exc.reason, str(exc)) from exc
        with self._lock:
            state.actions[idempotency_key] = dict(result)
        return result

    def apply_task_result(self, handle: str, result: Any) -> bool:
        """Apply a tab action's task terminal result to its tab; ``False`` if not a tab's."""

        with self._lock:
            key = self._handles.pop(handle, None)
            state = self._open.get(key) if key is not None else None
        if key is None:
            return False
        self._apply_result(key, result)
        if state is not None:
            refresh_soon(self._app, state.scope.namespace, self._loop)
        return True


def plugin_tabs(app: Any) -> PluginTabs:
    """The app's tab registry, created on first use."""

    tabs = getattr(app.state, "plugin_tabs", None)
    if not isinstance(tabs, PluginTabs):
        tabs = PluginTabs(app)
        app.state.plugin_tabs = tabs
    return tabs


def apply_task_result(app: Any, handle: str, result: Any) -> bool:
    """Route a task's terminal result to the tab whose action started it."""

    tabs = getattr(app.state, "plugin_tabs", None)
    return isinstance(tabs, PluginTabs) and tabs.apply_task_result(handle, result)


def session_agent_changed(app: Any, session_id: str) -> None:
    """Re-watch the session's open tabs after its agent (and so its servers) changed."""

    tabs = getattr(app.state, "plugin_tabs", None)
    if isinstance(tabs, PluginTabs):
        tabs.session_agent_changed(session_id)


__all__ = [
    "REFRESHED",
    "RESOURCE_FAILED",
    "SERVER_UNAVAILABLE",
    "SURFACE_DELETED",
    "SURFACE_UPSERTED",
    "UNAVAILABLE",
    "PluginTabError",
    "PluginTabs",
    "TabState",
    "apply_task_result",
    "session_agent_changed",
    "declared_tabs",
    "plugin_tabs",
]
