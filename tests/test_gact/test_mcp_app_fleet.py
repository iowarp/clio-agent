"""A view whose server's fleet was closed is not live, and its UI is told (fix stream).

Seen live (q-pv): a conversation nobody viewed for two minutes had its fleet reaped;
the pinned ParaView view stayed "live" in the registry, its card offered "Show tab",
and mounting it met a dead service. The registry now drops such a record with the
typed reason ``mcp_app_server_stopped`` and publishes ``mcp_app_closed``, so the card
offers Reopen (MA19). Every other server-side close is published the same way.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from types import SimpleNamespace
from typing import Any

from clio_agent.gact import mcp_app_fleet
from clio_agent.gact.mcp_app_cleanup import APP_CLOSED_EVENT, publish_closed
from clio_agent.gact.mcp_apps import MCPAppRegistry


class _Fleet:
    closed = False


def _registry() -> tuple[MCPAppRegistry, list[tuple[str, str]]]:
    registry = MCPAppRegistry(limit=8, ttl_s=3600)
    closed: list[tuple[str, str]] = []
    registry.on_closed = lambda record, reason: closed.append((record.app_instance_id, reason))
    return registry, closed


def _open(registry: MCPAppRegistry, sid: str = "sess_a") -> Any:
    return registry.register(
        session_id=sid,
        source_namespace="viz",
        tool_name="viz_open",
        resource_uri="ui://viz/view",
        tool_input={},
        tool_result={"content": [{"type": "text", "text": "opened"}]},
        display_mode="fullscreen",
    )


def test_a_pinned_view_whose_fleet_closed_is_dropped_and_announced() -> None:
    registry, closed = _registry()
    fleet = _Fleet()
    record = _open(registry)
    record.fleet = weakref.ref(fleet)
    assert registry.get("sess_a", record.app_instance_id, record.data_ref) is record

    fleet.closed = True  # the reaper (or a configuration change) closed the fleet
    assert registry.records_for_session("sess_a") == []
    assert closed == [(record.app_instance_id, "server_stopped")]


def test_an_unstamped_record_is_never_dropped_on_a_guess() -> None:
    registry, closed = _registry()
    record = _open(registry)
    assert mcp_app_fleet.server_stopped(record) is False
    assert registry.records_for_session("sess_a") == [record]
    assert closed == []


def test_a_collected_fleet_counts_as_stopped() -> None:
    record = SimpleNamespace(fleet=weakref.ref(_Fleet()))  # collected at once
    assert mcp_app_fleet.server_stopped(record) is True


def test_admission_stamps_the_workspace_fleet_that_serves_the_session(tmp_path: Any) -> None:
    fleet = _Fleet()
    root = str(tmp_path)
    from clio_agent.tools.workspace_root import canonical_workspace_root

    agent = SimpleNamespace(
        _workspace_state=lambda: (threading.Lock(), {canonical_workspace_root(root): fleet}, {})
    )
    session = SimpleNamespace(workspace_id="ws_1")
    workspace = SimpleNamespace(root_path=root)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda sid: session),
            workspaces=SimpleNamespace(get=lambda wid: workspace),
        )
    )
    record = SimpleNamespace(session_id="sess_a", fleet=None)
    mcp_app_fleet.admitted(app, record)
    assert record.fleet() is fleet
    fleet.closed = True
    assert mcp_app_fleet.server_stopped(record) is True


def test_every_server_side_close_is_announced() -> None:
    registry, closed = _registry()
    first, second, third = _open(registry), _open(registry), _open(registry, "sess_b")
    registry.remove(first.app_instance_id)
    registry.drop_session("sess_a")
    registry.claim_close("sess_b", third.app_instance_id, third.data_ref)
    assert closed == [
        (first.app_instance_id, "closed"),
        (second.app_instance_id, "session_ended"),
        (third.app_instance_id, "closed"),
    ]


def test_the_close_notice_is_a_transient_session_event() -> None:
    events: list[Any] = []
    app = SimpleNamespace(state=SimpleNamespace(bus=SimpleNamespace(publish=events.append)))
    record = SimpleNamespace(session_id="sess_a", app_instance_id="app_1")
    publish_closed(app, record, "server_stopped")
    (event,) = events
    assert event.type == APP_CLOSED_EVENT
    assert event.session_id == "sess_a"
    assert event.payload == {
        "session_id": "sess_a",
        "app_instance_id": "app_1",
        "reason": "server_stopped",
    }


def test_a_view_whose_server_was_stopped_and_replaced_is_dropped() -> None:
    """MI1: the fleet stopped the namespace's server (superseded or healed); the view
    it drew is dead even though the fleet is open, so it closes with Reopen."""
    from clio_agent.tools import namespace_instances

    registry, closed = _registry()
    fleet = _Fleet()
    record = _open(registry)
    record.fleet = weakref.ref(fleet)
    record.instance_generation = namespace_instances.generation(fleet, "viz")
    assert registry.records_for_session("sess_a") == [record]

    namespace_instances._bump(fleet, "other")  # another connector's server: unaffected
    assert registry.records_for_session("sess_a") == [record]
    namespace_instances._bump(fleet, "viz")
    assert registry.records_for_session("sess_a") == []
    assert closed == [(record.app_instance_id, "server_stopped")]


def test_an_open_view_keeps_its_server_from_a_route_heal() -> None:
    from clio_agent.tools import namespace_instances

    registry, _closed = _registry()
    inner = _Fleet()
    fleet = _Fleet()
    fleet._async_executor = inner  # type: ignore[attr-defined]
    record = _open(registry)
    record.fleet = weakref.ref(fleet)
    before = set(map(id, namespace_instances._PROBES))
    mcp_app_fleet.watch_views(registry)
    (mine,) = (p for p in namespace_instances._PROBES if id(p) not in before)
    try:
        assert namespace_instances.in_use(inner, "viz") == "open_views"
        assert namespace_instances.in_use(inner, "notebook") == ""
        assert namespace_instances.in_use(_Fleet(), "viz") == ""
        registry.remove(record.app_instance_id)
        assert namespace_instances.in_use(inner, "viz") == ""
    finally:
        del registry
        import gc

        gc.collect()
        assert namespace_instances.in_use(inner, "viz") == ""  # a gone registry unhooks
        # a close may unhook other gone registries' probes too (they unhook when read)
        assert mine not in namespace_instances._PROBES


class _Inner:
    """A tools executor holding one namespace's connection (namespace_instances' shape)."""

    def __init__(self) -> None:
        self._closed = False
        self._namespace_servers: dict[str, Any] = {}
        self._namespace_clients: dict[str, Any] = {}
        self._namespace_direct_routes: dict[str, bool] = {}
        self._connected_namespaces: set[str] = set()
        self._namespace_heal_attempted: set[str] = set()
        self.evicted: list[str] = []

    async def _evict_namespace_client(self, namespace: str) -> None:
        self.evicted.append(namespace)
        self._namespace_clients.pop(namespace, None)


class _Proxy:
    pass


def _fleet_serving(tmp_path: Any) -> tuple[Any, _Inner, _Proxy]:
    from clio_agent.tools import namespace_instances

    inner, proxy, fleet = _Inner(), _Proxy(), _Fleet()
    fleet._async_executor = inner  # type: ignore[attr-defined]
    namespace_instances.register(proxy, "viz", str(tmp_path), object())
    inner._namespace_servers["viz"] = proxy
    inner._namespace_clients["viz"] = object()
    return fleet, inner, proxy


async def _superseded_with_a_view(tmp_path: Any) -> tuple[Any, ...]:
    """A view drawn by the old fleet's server, then a connector value change."""
    from clio_agent.tools import namespace_instances

    registry, closed = _registry()
    mcp_app_fleet.watch_views(registry)
    old_fleet, old, old_proxy = _fleet_serving(tmp_path)
    await namespace_instances.claim(old, "viz", old_proxy)
    record = _open(registry)
    record.fleet = weakref.ref(old_fleet)
    record.instance_generation = namespace_instances.generation(old, "viz")
    new_fleet, new, new_proxy = _fleet_serving(tmp_path)
    assert await namespace_instances.claim(new, "viz", new_proxy) == 0
    assert old.evicted == []  # retained: the view keeps rendering
    assert registry.records_for_session("sess_a") == [record]
    return registry, closed, record, old_fleet, old, new_fleet, new


async def _retired(inner: _Inner) -> bool:
    for _ in range(200):
        if inner.evicted:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_closing_the_last_view_retires_the_superseded_server(tmp_path: Any) -> None:
    """Take 1s gap 15: the old server stays while its view is open, then retires."""
    registry, closed, record, _old_fleet, old, _new_fleet, new = await _superseded_with_a_view(
        tmp_path
    )
    claimed = registry.claim_close("sess_a", record.app_instance_id, record.data_ref)
    await asyncio.sleep(0.05)
    assert old.evicted == []  # its cleanup call still needs the server
    registry.finish_close(claimed.app_instance_id)
    assert await _retired(old)
    assert old.evicted == ["viz"]
    assert new.evicted == []
    assert closed == [(record.app_instance_id, "closed")]


async def test_a_session_ending_retires_the_superseded_server(tmp_path: Any) -> None:
    registry, closed, record, _old_fleet, old, _new_fleet, new = await _superseded_with_a_view(
        tmp_path
    )
    registry.drop_session("sess_a")
    assert await _retired(old)
    assert new.evicted == []
    assert closed == [(record.app_instance_id, "session_ended")]


def test_a_views_own_calls_go_to_the_fleet_that_drew_it() -> None:
    from clio_agent.gact import mcp_apps

    drew, current = _Fleet(), _Fleet()
    agent = SimpleNamespace(_active_tool_executor=lambda: current)
    app = SimpleNamespace(state=SimpleNamespace(agent=agent))
    record = SimpleNamespace(fleet=weakref.ref(drew), instance_generation=0, source_namespace="viz")
    assert mcp_apps._bound_executor(app, "sess_a", record) is drew
    assert mcp_apps._bound_executor(app, "sess_a") is current  # the agent's calls: the new one
    drew.closed = True
    assert mcp_apps._bound_executor(app, "sess_a", record) is current
