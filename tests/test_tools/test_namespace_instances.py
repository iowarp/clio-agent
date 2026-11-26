"""One live server per connector per workspace folder (MI1).

Seen live (W17 take 1o): a route heal started a direct ParaView server beside the
proxy's kept-alive one, so the view the first drew went "Service unavailable"; a
connector-values change then started a second fleet's server in the same folder and
its ``open_view`` failed with "another node half is running in this folder".
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

import pytest

from clio_agent.tools import namespace_instances


class _Transport:
    def __init__(self) -> None:
        self._connect_task: asyncio.Future[None] | None = None
        self.disconnects = 0

    def start(self) -> None:
        self._connect_task = asyncio.get_running_loop().create_future()

    async def disconnect(self) -> None:
        self.disconnects += 1
        if self._connect_task is not None:
            self._connect_task.cancel()
        self._connect_task = None


class _Proxy:
    pass


class _Executor:
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
        self._namespace_direct_routes.pop(namespace, None)


@pytest.fixture(autouse=True)
def _fake_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.tools import mcp_client_lifecycle

    async def disconnect(transport: Any) -> None:
        await transport.disconnect()

    monkeypatch.setattr(mcp_client_lifecycle, "disconnect_owned_transport", disconnect)


def _served(tmp_path: Any, namespace: str = "paraview") -> tuple[_Executor, _Proxy, _Transport]:
    executor, proxy, transport = _Executor(), _Proxy(), _Transport()
    namespace_instances.register(proxy, namespace, str(tmp_path), transport)
    executor._namespace_servers[namespace] = proxy
    return executor, proxy, transport


def _pooled(log: list[tuple[set[int], str]]) -> namespace_instances.Stopper:
    async def stop(proxies: tuple[Any, ...], reason: str) -> int:
        log.append(({id(proxy) for proxy in proxies}, reason))
        return len(proxies)

    return stop


async def test_a_new_fleets_server_stops_the_old_fleets_server_in_that_folder(
    tmp_path: Any,
) -> None:
    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    old._namespace_clients["paraview"] = object()
    pooled: list[tuple[set[int], str]] = []
    remove = namespace_instances.add_stopper(asyncio.get_running_loop(), _pooled(pooled))
    try:
        new, new_proxy, new_backend = _served(tmp_path)  # a connector-values change
        stopped = await namespace_instances.claim(new, "paraview", new_proxy)
    finally:
        remove()

    assert stopped == 3  # its executor's connection, the kept-alive direct one, the backend
    assert old.evicted == ["paraview"]
    assert pooled == [({id(old_proxy)}, "superseded")]
    assert old_backend.disconnects == 1
    assert new_backend.disconnects == 0
    assert namespace_instances.generation(old, "paraview") == 1  # its views close (Reopen)


async def test_another_folder_or_connector_is_left_alone(tmp_path: Any) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, a_proxy, a_backend = _served(tmp_path / "a")
    await namespace_instances.claim(a, "paraview", a_proxy)
    a_backend.start()
    other, other_proxy, _ = _served(tmp_path / "a", "notebook")
    b, b_proxy, _ = _served(tmp_path / "b")

    assert await namespace_instances.claim(other, "notebook", other_proxy) == 0
    assert await namespace_instances.claim(b, "paraview", b_proxy) == 0
    assert a_backend.disconnects == 0
    a_backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_a_plugins_server_leaves_the_persons_same_named_server_running(
    tmp_path: Any,
) -> None:
    # Live (round 4): each fleet's ``probe`` call stopped the other's "superseded".
    person, person_proxy, person_backend = _served(tmp_path, "probe")
    await namespace_instances.claim(person, "probe", person_proxy)
    person_backend.start()
    plugin, plugin_proxy, plugin_backend = _Executor(), _Proxy(), _Transport()
    owner = namespace_instances.connector_owner(type("S", (), {"source": "plugin:probe-kit"}))
    namespace_instances.register(plugin_proxy, "probe", str(tmp_path), plugin_backend, None, owner)
    plugin._namespace_servers["probe"] = plugin_proxy

    assert await namespace_instances.claim(plugin, "probe", plugin_proxy) == 0
    assert person_backend.disconnects == 0
    person_backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_a_route_heal_waits_while_the_proxy_server_draws_a_view(tmp_path: Any) -> None:
    executor, proxy, backend = _served(tmp_path)
    await namespace_instances.claim(executor, "paraview", proxy)
    backend.start()
    remove = namespace_instances.add_in_use_probe(
        lambda owner, ns: "open_views" if owner is executor and ns == "paraview" else ""
    )
    try:
        assert await namespace_instances.may_go_direct(executor, "paraview", proxy) is False
    finally:
        remove()
    assert backend.disconnects == 0

    assert await namespace_instances.may_go_direct(executor, "paraview", proxy) is True
    assert backend.disconnects == 1  # stopped before the direct server starts
    assert namespace_instances.generation(executor, "paraview") == 1


async def test_a_route_heal_waits_for_calls_in_flight_on_the_proxy_route(tmp_path: Any) -> None:
    executor, proxy, backend = _served(tmp_path)
    await namespace_instances.claim(executor, "paraview", proxy)
    backend.start()
    end = namespace_instances.begin_proxy_call(executor, "paraview")
    assert await namespace_instances.may_go_direct(executor, "paraview", proxy) is False
    end()
    assert await namespace_instances.may_go_direct(executor, "paraview", proxy) is True


async def test_a_route_heal_keeps_the_direct_connection_it_switches_to(tmp_path: Any) -> None:
    executor, proxy, backend = _served(tmp_path)
    await namespace_instances.claim(executor, "paraview", proxy)
    backend.start()
    executor._namespace_clients["paraview"] = object()
    executor._namespace_direct_routes["paraview"] = True
    pooled: list[tuple[set[int], str]] = []
    remove = namespace_instances.add_stopper(asyncio.get_running_loop(), _pooled(pooled))
    try:
        assert await namespace_instances.may_go_direct(executor, "paraview", proxy) is True
    finally:
        remove()
    assert executor.evicted == []
    assert pooled == []
    assert backend.disconnects == 1


async def test_with_no_live_backend_the_direct_route_is_free(tmp_path: Any) -> None:
    executor, proxy, backend = _served(tmp_path)
    assert await namespace_instances.may_go_direct(executor, "paraview", proxy) is True
    assert backend.disconnects == 0


async def test_a_closed_fleets_dead_proxy_is_skipped(tmp_path: Any) -> None:
    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old._closed = True
    pooled: list[tuple[set[int], str]] = []
    remove = namespace_instances.add_stopper(asyncio.get_running_loop(), _pooled(pooled))
    try:
        new, new_proxy, _ = _served(tmp_path)
        assert await namespace_instances.claim(new, "paraview", new_proxy) == 0
    finally:
        remove()
    assert pooled == []
    assert old_backend.disconnects == 0


def _launched(tmp_path: Any, namespace: str, launch: str) -> tuple[_Executor, _Proxy, _Transport]:
    executor, proxy, transport = _Executor(), _Proxy(), _Transport()
    namespace_instances.register(proxy, namespace, str(tmp_path), transport, launch)
    executor._namespace_servers[namespace] = proxy
    return executor, proxy, transport


async def test_a_values_change_of_one_connector_keeps_its_siblings_server(
    tmp_path: Any,
) -> None:
    """Take 1p: setting ParaView's value started a new fleet whose relay call stopped
    the running relay (and its ssh forward). An unchanged launch is one shared server."""

    old, relay, relay_backend = _launched(tmp_path, "relay", "relay-launch")
    _, view, view_backend = _launched(tmp_path, "paraview", "pv-old")
    old._namespace_servers["paraview"] = view
    await namespace_instances.claim(old, "relay", relay)
    await namespace_instances.claim(old, "paraview", view)
    relay_backend.start()
    view_backend.start()
    old._namespace_clients.update(relay=object(), paraview=object())
    pooled: list[tuple[set[int], str]] = []
    remove = namespace_instances.add_stopper(asyncio.get_running_loop(), _pooled(pooled))
    try:
        new, new_relay, new_relay_backend = _launched(tmp_path, "relay", "relay-launch")
        _, new_view, _ = _launched(tmp_path, "paraview", "pv-new")
        new._namespace_servers["paraview"] = new_view

        adopted = namespace_instances.adopt(new, "relay", new_relay)
        assert adopted is relay
        assert new._namespace_servers["relay"] is relay
        assert await namespace_instances.claim(new, "relay", adopted) == 0

        assert namespace_instances.adopt(new, "paraview", new_view) is new_view
        assert await namespace_instances.claim(new, "paraview", new_view) == 3
    finally:
        remove()

    assert relay_backend.disconnects == 0
    assert new_relay_backend.disconnects == 0
    assert old.evicted == ["paraview"]  # only the connector whose launch changed moved
    assert pooled == [({id(view)}, "superseded")]
    assert view_backend.disconnects == 1
    assert namespace_instances.generation(old, "relay") == 0
    relay_backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_the_old_fleet_reclaiming_a_shared_server_stops_nothing(tmp_path: Any) -> None:
    old, relay, backend = _launched(tmp_path, "relay", "same")
    await namespace_instances.claim(old, "relay", relay)
    backend.start()
    new, new_relay, _ = _launched(tmp_path, "relay", "same")
    assert namespace_instances.adopt(new, "relay", new_relay) is relay
    await namespace_instances.claim(new, "relay", relay)

    assert await namespace_instances.claim(old, "relay", relay) == 0  # no ping-pong
    assert backend.disconnects == 0
    backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_a_shared_server_survives_one_fleet_dropping_it(tmp_path: Any) -> None:
    old, relay, _ = _launched(tmp_path, "relay", "same")
    await namespace_instances.claim(old, "relay", relay)
    new, new_relay, _ = _launched(tmp_path, "relay", "same")
    namespace_instances.adopt(new, "relay", new_relay)
    await namespace_instances.claim(new, "relay", relay)

    assert namespace_instances.unshared((relay,), old) == ()
    assert namespace_instances.routers(relay, exclude=old) == [new]
    new._closed = True
    assert namespace_instances.unshared((relay,), old) == (relay,)


async def test_a_changed_launch_still_supersedes_the_running_server(tmp_path: Any) -> None:
    old, relay, backend = _launched(tmp_path, "relay", "v1")
    await namespace_instances.claim(old, "relay", relay)
    backend.start()
    new, new_relay, _ = _launched(tmp_path, "relay", "v2")
    assert namespace_instances.adopt(new, "relay", new_relay) is new_relay
    assert await namespace_instances.claim(new, "relay", new_relay) >= 1
    assert backend.disconnects == 1


def test_the_launch_key_is_the_servers_start() -> None:
    spec = type("Spec", (), {})()
    spec.transport, spec.command, spec.args = "stdio", "relay", ["serve"]
    spec.env, spec.url = {"B": "2", "A": "1"}, None
    other = type("Spec", (), {})()
    other.transport, other.command, other.args = "stdio", "relay", ["serve"]
    other.env, other.url = {"A": "1", "B": "2"}, None
    assert namespace_instances.launch_key(spec, "/w") == namespace_instances.launch_key(other, "/w")
    other.env = {"A": "1", "B": "3"}
    assert namespace_instances.launch_key(spec, "/w") != namespace_instances.launch_key(other, "/w")


def test_an_executor_without_stops_has_generation_zero() -> None:
    assert namespace_instances.generation(object(), "paraview") == 0
    wrapper = type("Sync", (), {})()
    inner = _Executor()
    wrapper._async_executor = inner  # type: ignore[attr-defined]
    namespace_instances._bump(inner, "paraview")
    assert namespace_instances.generation(wrapper, "paraview") == 1
    assert weakref.ref(inner)() is inner


async def _settle() -> None:
    while namespace_instances._RETIRING:
        await asyncio.sleep(0)
        await asyncio.gather(
            *(f for f in tuple(namespace_instances._RETIRING) if isinstance(f, asyncio.Future))
        )


async def test_a_superseded_server_drawing_a_view_is_retained_until_the_view_closes(
    tmp_path: Any,
) -> None:
    """Take 1s gap 15: a connector value change closed the open ParaView view."""
    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    old._namespace_clients["paraview"] = object()
    views = {"open": True}
    remove = namespace_instances.add_in_use_probe(
        lambda owner, ns: "open_views"
        if views["open"] and owner is old and ns == "paraview"
        else ""
    )
    try:
        new, new_proxy, new_backend = _served(tmp_path)  # the connector value changed
        assert await namespace_instances.claim(new, "paraview", new_proxy) == 0
        assert old_backend.disconnects == 0  # the view keeps its server
        assert namespace_instances.generation(old, "paraview") == 0  # the card stays live
        assert namespace_instances.retire_superseded() == 0  # still drawn
        assert old_backend.disconnects == 0

        views["open"] = False  # the last view closed
        assert namespace_instances.retire_superseded() == 1
        await _settle()
    finally:
        remove()
    assert old.evicted == ["paraview"]
    assert old_backend.disconnects == 1
    assert new_backend.disconnects == 0
    assert namespace_instances.retire_superseded() == 0  # retired once


async def test_a_call_in_flight_does_not_retain_a_superseded_server(tmp_path: Any) -> None:
    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    end = namespace_instances.begin_proxy_call(old, "paraview")
    try:
        new, new_proxy, _ = _served(tmp_path)
        assert await namespace_instances.claim(new, "paraview", new_proxy) >= 1
    finally:
        end()
    assert old_backend.disconnects == 1  # the call gets the typed stop error and retries


async def test_a_views_call_on_its_retained_server_does_not_take_over(tmp_path: Any) -> None:
    """Live take: each view call re-claimed the old server and stopped the new one."""
    from clio_agent.tools import call_origin

    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    views = {"open": True}
    remove = namespace_instances.add_in_use_probe(
        lambda owner, ns: "open_views" if views["open"] and owner is old else ""
    )
    try:
        new, new_proxy, new_backend = _served(tmp_path)
        new_backend.start()
        await namespace_instances.claim(new, "paraview", new_proxy)
        tag = f"in-process:{id(old_proxy):x}"
        assert namespace_instances.retained_owners() == {tag}
        with call_origin.scope("view"):
            assert await namespace_instances.claim(old, "paraview", old_proxy) == 0
        assert new_backend.disconnects == 0  # the new server stays current
        assert namespace_instances._is_current(new_proxy)
        assert namespace_instances.retained_owners() == {tag}
        await namespace_instances.claim(new, "paraview", new_proxy)  # the agent's next call
        assert old_backend.disconnects == 0
    finally:
        remove()
        views["open"] = False
    assert namespace_instances.retire_superseded() == 1
    await _settle()
    assert old_backend.disconnects == 1
    assert new_backend.disconnects == 0
    new_backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_a_failure_on_the_new_server_names_the_views_holding_the_old_one(
    tmp_path: Any,
) -> None:
    """The retained server may hold an exclusive resource: the failure says which view."""
    from clio_agent.errors import MCP_NAMESPACE_INSTANCE_HELD, MCPNamespaceInstanceHeldError
    from clio_agent.tools import mcp_executor

    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    views = {"open": True}
    remove = [
        namespace_instances.add_in_use_probe(
            lambda owner, ns: "open_views" if views["open"] and owner is old else ""
        ),
        namespace_instances.add_view_namer(
            lambda owner, ns: ["open_view view mapp_1 (conversation sess_1)"]
            if views["open"] and owner is old
            else []
        ),
    ]
    try:
        new, new_proxy, new_backend = _served(tmp_path)
        new_backend.start()
        await namespace_instances.claim(new, "paraview", new_proxy)
        assert namespace_instances.holding_views(new_proxy) == [
            "open_view view mapp_1 (conversation sess_1)"
        ]
        assert namespace_instances.holding_views(old_proxy) == []  # the view's own server
        error = mcp_executor._held_error(
            "paraview_open_view", "paraview", new_proxy, RuntimeError("node lock held")
        )
        assert isinstance(error, MCPNamespaceInstanceHeldError)
        assert str(error).startswith("paraview_open_view failed on the new paraview server: ")
        assert "node lock held" in str(error).split(MCP_NAMESPACE_INSTANCE_HELD)[0]
        assert "mapp_1" in str(error) and MCP_NAMESPACE_INSTANCE_HELD in str(error)
        assert error.details["views"] == ["open_view view mapp_1 (conversation sess_1)"]
        assert error.details["reason"] == MCP_NAMESPACE_INSTANCE_HELD
        assert mcp_executor._held_error("paraview_x", "paraview", None, RuntimeError()) is None

        import mcp_types

        failed = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="another node half is running")],
            is_error=True,
        )
        noted = mcp_executor._with_held_note(failed, "paraview_open_view", "paraview", new_proxy)
        assert noted.content[0].text == "another node half is running"
        assert noted.content[-1].text.startswith(f"({MCP_NAMESPACE_INSTANCE_HELD})")
        assert "mapp_1" in noted.content[-1].text
        from clio_agent.tools.mcp_result_text import result_to_text
        from clio_agent.tools.result_errors import structured_tool_result_error

        # The failure people see is the new server's own, whatever the views' names.
        shown = structured_tool_result_error(noted) or ""
        assert shown == "another node half is running"
        seen = result_to_text(noted)  # the model reads the failure, then the views
        assert seen.index("another node half is running") < seen.index("mapp_1")
        long_named = ["open_view view " + "v" * 600 + " (conversation sess_1)"]
        assert namespace_instances.holding_views(new_proxy)
        cause = "Traceback: NameError: name 'reader' is not defined"
        script_failed = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=cause)], is_error=True
        )
        import unittest.mock

        with unittest.mock.patch.object(
            namespace_instances, "holding_views", lambda proxy: long_named
        ):
            long_noted = mcp_executor._with_held_note(script_failed, "pv_x", "paraview", new_proxy)
        assert structured_tool_result_error(long_noted) == cause  # never cut off by the note
        from fastmcp.client.client import CallToolResult as ClientResult

        client_failed = ClientResult(  # the proxy's own result shape: a dataclass
            content=[mcp_types.TextContent(type="text", text="lock held")],
            structured_content=None,
            meta=None,
            data=None,
            is_error=True,
        )
        client_noted = mcp_executor._with_held_note(client_failed, "pv_x", "paraview", new_proxy)
        assert structured_tool_result_error(client_noted) == "lock held"
        assert "mapp_1" in result_to_text(client_noted)
        assert old_backend.disconnects == 0  # the view's server is never stopped for it
        views["open"] = False
        assert namespace_instances.holding_views(new_proxy) == []
    finally:
        for undo in remove:
            undo()
        views["open"] = False
    namespace_instances.retire_superseded()
    await _settle()
    new_backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_a_retained_server_that_is_claimed_again_is_not_retired(tmp_path: Any) -> None:
    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    views = {"open": True}
    remove = namespace_instances.add_in_use_probe(
        lambda owner, ns: "open_views" if views["open"] and owner is old else ""
    )
    try:
        new, new_proxy, new_backend = _served(tmp_path)
        new_backend.start()
        await namespace_instances.claim(new, "paraview", new_proxy)
        views["open"] = False
        await namespace_instances.claim(old, "paraview", old_proxy)  # the value was set back
        assert new_backend.disconnects == 1
        assert namespace_instances.retire_superseded() == 0
    finally:
        remove()
    assert old_backend.disconnects == 0
    old_backend._connect_task.cancel()  # type: ignore[union-attr]


async def test_retirement_from_another_thread_runs_on_the_claims_loop(tmp_path: Any) -> None:
    old, old_proxy, old_backend = _served(tmp_path)
    await namespace_instances.claim(old, "paraview", old_proxy)
    old_backend.start()
    views = {"open": True}
    remove = namespace_instances.add_in_use_probe(
        lambda owner, ns: "open_views" if views["open"] and owner is old else ""
    )
    try:
        new, new_proxy, _ = _served(tmp_path)
        await namespace_instances.claim(new, "paraview", new_proxy)
        views["open"] = False
        scheduled = await asyncio.to_thread(namespace_instances.retire_superseded)
        assert scheduled == 1
        for _ in range(200):
            if old_backend.disconnects:
                break
            await asyncio.sleep(0.01)
    finally:
        remove()
    assert old_backend.disconnects == 1
