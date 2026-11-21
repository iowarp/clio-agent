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


def test_an_executor_without_stops_has_generation_zero() -> None:
    assert namespace_instances.generation(object(), "paraview") == 0
    wrapper = type("Sync", (), {})()
    inner = _Executor()
    wrapper._async_executor = inner  # type: ignore[attr-defined]
    namespace_instances._bump(inner, "paraview")
    assert namespace_instances.generation(wrapper, "paraview") == 1
    assert weakref.ref(inner)() is inner
