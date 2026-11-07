"""A live view (SSE subscriber) keeps its conversation's MCP fleet; it leaves on idle after."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

from clio_agent.gact import view_fleet
from clio_agent.tools.reaper import WorkspaceExecutorReaper


class _Executor:
    busy = False
    closed = False

    def __init__(self) -> None:
        self.idle_since = time.monotonic() - 1000.0  # long idle: past any TTL

    def idle_for(self) -> float:
        return time.monotonic() - self.idle_since

    def close(self) -> None:
        self.closed = True


class _Agent:
    """The fleet surface of ``ClioAgent``: registry, leases, reaper."""

    def __init__(self, ttl_s: float) -> None:
        self._workspace_executor_lock = threading.Lock()
        self.executors: dict[str, Any] = {"/ws": _Executor()}
        self.leases: dict[str, int] = {}
        self._workspace_reaper = WorkspaceExecutorReaper(
            self.executors,
            self._workspace_executor_lock,
            leases=self.leases,
            ttl_s=ttl_s,
            channel_closer=lambda _root: 0,
        )

    def _workspace_state(self) -> tuple[threading.Lock, dict[str, Any], dict[str, int]]:
        return self._workspace_executor_lock, self.executors, self.leases

    @contextmanager
    def lease_workspace_fleet(self, root: str) -> Iterator[None]:
        with self._workspace_executor_lock:
            self.leases[root] = self.leases.get(root, 0) + 1
        try:
            yield
        finally:
            with self._workspace_executor_lock:
                self.leases[root] -= 1
                if not self.leases[root]:
                    del self.leases[root]


def _app(agent: _Agent) -> Any:
    session = SimpleNamespace(workspace_id="ws")
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda sid: session if sid == "s" else None),
            workspaces=SimpleNamespace(get=lambda wid: SimpleNamespace(root_path="/ws")),
        )
    )


def _settle() -> None:
    deadline = time.monotonic() + 5
    while view_fleet.held_views() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_view_holds_the_fleet_and_the_idle_window_restarts_when_it_goes() -> None:
    agent = _Agent(ttl_s=0.5)
    app = _app(agent)
    reaper = agent._workspace_reaper

    async def scenario() -> None:
        async with view_fleet.viewing(app, "s"):
            async with view_fleet.viewing(app, "s"):  # a second tab on the same conversation
                assert agent.leases == {"/ws": 1}
                assert reaper.reap_once() == []  # idle far past the TTL, but viewed
            for _ in range(500):  # the inner view's release lands off the loop
                if view_fleet.held_views() == {"s": 1}:
                    break
                await asyncio.sleep(0.01)
            assert agent.leases == {"/ws": 1} and view_fleet.held_views() == {"s": 1}
            assert reaper.reap_once() == []

    asyncio.run(scenario())
    _settle()
    deadline = time.monotonic() + 5
    while agent.leases and time.monotonic() < deadline:
        time.sleep(0.01)
    assert agent.leases == {}
    # The view just went: the fleet stays one idle window, then the reaper takes it.
    assert reaper.reap_once() == []
    time.sleep(0.6)
    assert reaper.reap_once() == ["/ws"]


def test_a_view_that_leaves_before_its_hold_lands_releases_it() -> None:
    agent = _Agent(ttl_s=60)
    app = _app(agent)

    async def scenario() -> None:
        task = asyncio.create_task(view_fleet.viewing(app, "s").__aenter__())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    deadline = time.monotonic() + 5
    while (agent.leases or view_fleet.held_views()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert agent.leases == {} and view_fleet.held_views() == {}


def test_a_refused_lease_is_typed_and_the_view_goes_on(caplog: Any) -> None:
    agent = _Agent(ttl_s=60)

    @contextmanager
    def refused(_root: str) -> Iterator[None]:
        raise RuntimeError("CLIO is updating connected-data access")
        yield  # pragma: no cover

    agent.lease_workspace_fleet = refused  # type: ignore[method-assign]

    async def scenario() -> None:
        async with view_fleet.viewing(_app(agent), "s"):
            assert view_fleet.held_views() == {}

    asyncio.run(scenario())
    assert view_fleet.VIEW_HOLD_REFUSED in caplog.text
