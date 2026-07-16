"""Workspace-fleet reclamation (#930 S3/#933): TTL, LRU, drain, leases.

The reaper's hard guarantees, pinned with deterministic fake executors:
never reap a busy executor, never reap a TURN-leased root, reap idle-TTL
expirees and LRU overflow with typed reasons, and survive close failures.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from clio_agent.tools.reaper import WorkspaceExecutorReaper


class FakeExecutor:
    def __init__(self, idle_s: float = 0.0, busy: bool = False) -> None:
        self._idle_s = idle_s
        self.busy = busy
        self.closed = False
        self.close_calls = 0

    def idle_for(self) -> float:
        return self._idle_s

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class ExplodingExecutor(FakeExecutor):
    def close(self) -> None:
        super().close()
        raise RuntimeError("close blew up")


def _reaper(registry, leases=None, ttl=100.0, cap=2):
    return WorkspaceExecutorReaper(
        registry,
        threading.Lock(),
        leases=leases if leases is not None else {},
        ttl_s=ttl,
        max_resident=cap,
        tick_s=3600,  # never ticks on its own in tests
    )


def test_idle_ttl_reaps_and_removes() -> None:
    stale = FakeExecutor(idle_s=500)
    fresh = FakeExecutor(idle_s=5)
    registry = {"/ws/stale": stale, "/ws/fresh": fresh}
    reaped = _reaper(registry).reap_once()
    assert reaped == ["/ws/stale"]
    assert stale.closed and not fresh.closed
    assert set(registry) == {"/ws/fresh"}


def test_busy_executor_is_never_reaped() -> None:
    busy = FakeExecutor(idle_s=10_000, busy=True)
    registry = {"/ws/busy": busy}
    assert _reaper(registry).reap_once() == []
    assert not busy.closed and "/ws/busy" in registry


def test_leased_root_is_never_reaped() -> None:
    """The turn lease: idle BETWEEN tool calls inside a live turn never counts."""

    leased = FakeExecutor(idle_s=10_000)
    registry = {"/ws/turn": leased}
    leases = {"/ws/turn": 1}
    assert _reaper(registry, leases=leases).reap_once() == []
    assert not leased.closed
    # Lease released -> next pass reaps.
    leases.pop("/ws/turn")
    assert _reaper(registry, leases=leases).reap_once() == ["/ws/turn"]
    assert leased.closed


def test_lru_cap_evicts_stalest_unleased() -> None:
    registry = {
        "/ws/a": FakeExecutor(idle_s=50),
        "/ws/b": FakeExecutor(idle_s=30),
        "/ws/c": FakeExecutor(idle_s=10),
    }
    reaped = _reaper(registry, ttl=10_000, cap=2).reap_once()
    assert reaped == ["/ws/a"]  # stalest evicted; cap respected
    assert set(registry) == {"/ws/b", "/ws/c"}


class ProbeExplodingExecutor:
    """An executor whose reaper probes (busy/idle_for) raise."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def busy(self) -> bool:
        raise RuntimeError("probe blew up")

    def idle_for(self) -> float:
        raise RuntimeError("probe blew up")

    def close(self) -> None:
        self.closed = True


def test_probe_failure_never_orphans_popped_executors() -> None:
    """A registry entry whose busy/idle_for probe raises is skipped typed;
    executors already popped in the same pass are STILL closed. (Adversarial
    review finding: an abort mid-collection leaked popped-but-unclosed fleets.)"""

    stale = FakeExecutor(idle_s=500)
    broken = ProbeExplodingExecutor()
    # dict order guarantees the stale entry is popped BEFORE the broken probe.
    registry = {"/ws/stale": stale, "/ws/broken": broken}
    reaped = _reaper(registry).reap_once()
    assert reaped == ["/ws/stale"]
    assert stale.closed, "popped executor must be closed even when a later probe raises"
    assert not broken.closed
    assert set(registry) == {"/ws/broken"}, "unjudgeable entry stays resident, not popped"

    # Same guarantee on the LRU path: the broken probe is excluded from
    # eviction candidates instead of aborting the pass.
    registry2 = {
        "/ws/x": FakeExecutor(idle_s=50),
        "/ws/y": ProbeExplodingExecutor(),
        "/ws/z": FakeExecutor(idle_s=10),
    }
    reaped2 = _reaper(registry2, ttl=10_000, cap=2).reap_once()
    assert reaped2 == ["/ws/x"]
    assert set(registry2) == {"/ws/y", "/ws/z"}


def test_close_failure_is_survived_and_root_stays_removed() -> None:
    """A close() explosion is typed (trace) and does not kill the pass —
    but the executor left the registry so it cannot be handed out again."""

    bad = ExplodingExecutor(idle_s=500)
    good = FakeExecutor(idle_s=500)
    registry = {"/ws/bad": bad, "/ws/good": good}
    reaped = _reaper(registry).reap_once()
    assert reaped == ["/ws/good"]
    assert registry == {}
    assert bad.close_calls == 1


def test_agent_getter_rebuilds_reaped_executor(monkeypatch) -> None:
    """After a reap, the next tool use rebuilds the fleet lazily instead of
    handing out a closed executor."""

    from clio_agent.tools.execution import tool_workspace_context
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    agent = _bare_agent()
    built: list[str] = []

    def fake_build(*, cwd=None, set_catalog=False):
        assert cwd is not None
        built.append(cwd)
        return f"gateway:{cwd}"

    class ClosedThenOpen:
        def __init__(self, gateway):
            self.gateway = gateway
            self.closed = False

    def fake_create(gateway, **kwargs):
        return ClosedThenOpen(gateway)

    monkeypatch.setattr(agent, "_build_tool_gateway", fake_build)
    monkeypatch.setattr("clio_agent.agent.create_sync_tool_executor", fake_create)
    monkeypatch.setattr("clio_agent.agent.namespace_proxies", lambda gw: {})

    with tool_workspace_context("/ws/reap"):
        first = agent._active_tool_executor()
        first.closed = True  # the reaper closed it
        second = agent._active_tool_executor()
    assert second is not first, "a closed (reaped) executor must be rebuilt"
    assert built == ["/ws/reap", "/ws/reap"]


def test_reaper_protocol_on_real_sync_executor() -> None:
    """The reaper's protocol (busy / idle_for / closed / close) must hold on the
    REAL SyncMCPToolExecutor, not just fakes — the live gate failed exactly here
    (every tick: "'SyncMCPToolExecutor' object has no attribute 'busy'") while
    the fake-only suite stayed green."""

    from fastmcp import FastMCP

    from clio_agent.tools.execution import SyncMCPToolExecutor

    gate = threading.Event()
    release = threading.Event()

    server = FastMCP("reaper-contract")

    @server.tool
    def block(text: str) -> str:
        gate.set()
        assert release.wait(timeout=30), "test never released the in-flight call"
        return text

    executor = SyncMCPToolExecutor(server)
    try:
        # Fresh executor: not busy, idle clock runs, protocol attrs are real.
        assert executor.busy is False
        assert executor.idle_for() >= 0.0
        assert executor.closed is False

        # An in-flight call marks the executor busy → the reaper must skip it.
        registry = {"/ws/real": executor}
        caller = threading.Thread(
            target=lambda: executor.call_tool("block", {"text": "hi"}), daemon=True
        )
        caller.start()
        assert gate.wait(timeout=30), "tool call never started"
        assert executor.busy is True
        assert _reaper(registry, ttl=0.0).reap_once() == []
        assert executor.closed is False and "/ws/real" in registry

        # Drained: idle-TTL reap closes the real executor.
        release.set()
        caller.join(timeout=30)
        assert executor.busy is False
        assert _reaper(registry, ttl=0.0).reap_once() == ["/ws/real"]
        assert executor.closed is True
        assert registry == {}
    finally:
        release.set()
        if not executor.closed:
            executor.close()


def test_turn_lease_wiring_pins_root_during_context(monkeypatch, tmp_path) -> None:
    """gact's _tool_session_context leases the workspace root for the turn."""

    from types import SimpleNamespace

    from clio_agent.gact.runtime import globals as rt_globals
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    observed: dict[str, int] = {}
    agent = _bare_agent()
    ws = SimpleNamespace(root_path=str(tmp_path))
    sess = SimpleNamespace(workspace_id="w1")
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda sid: sess),
            workspaces=SimpleNamespace(get=lambda wid: ws),
        )
    )
    from clio_agent.gact import context as _ctx

    token = _ctx.set_app(app)
    try:
        with rt_globals._tool_session_context("sess_x"):
            observed.update(agent._workspace_state()[2])
    finally:
        _ctx.reset(token)
    assert observed == {str(tmp_path): 1}, "lease not held during the turn context"
    _lock, _executors, leases = agent._workspace_state()
    assert leases == {}, "lease not released after the turn"


def test_delegated_expert_holds_workspace_lease_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A delegated expert must remain leased while its worker uses bound tools."""

    import asyncio
    from types import SimpleNamespace

    from clio_agent.gact import context as _ctx
    from clio_agent.gact import turn_delegation
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    root = str(tmp_path)
    executor = FakeExecutor(idle_s=10_000)
    agent = _bare_agent()
    _lock, executors, leases = agent._workspace_state()
    executors[root] = executor
    session = SimpleNamespace(workspace_id="workspace-1")
    workspace = SimpleNamespace(root_path=root)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda _sid: session),
            workspaces=SimpleNamespace(get=lambda _wid: workspace),
        )
    )
    state: Any = SimpleNamespace(
        app=app,
        sid="session-1",
        turn_id="turn-1",
        trace_id="trace-1",
        workflow_schema=None,
        invocation_agent_id="orchestrator",
    )
    agent_def: Any = SimpleNamespace(
        id="catalog",
        metadata={},
        parent_id="orchestrator",
        default_provider="",
        default_model="",
    )
    observed: dict[str, object] = {}

    def run_child(*_args: Any, **_kwargs: Any) -> Any:
        observed["leases"] = dict(leases)
        observed["reaped"] = _reaper(
            executors,
            leases=leases,
            ttl=0.0,
        ).reap_once()
        return SimpleNamespace(
            answer="",
            reasoning="",
            next_expert="finish",
            next_task="",
            workflow_state={},
        )

    async def await_work(_state: Any, work: Any) -> Any:
        return await work

    monkeypatch.setattr(
        "clio_agent.gact.app._blueprint_runner_for_agent",
        lambda _agent_def: object(),
    )
    monkeypatch.setattr(turn_delegation, "_run_dynamic_agent_compat", run_child)
    monkeypatch.setattr(turn_delegation, "await_turn_work", await_work)
    monkeypatch.setattr(turn_delegation, "_emit_semantic_event", lambda *_a, **_k: None)
    monkeypatch.setattr(turn_delegation, "_prediction_workflow_state", lambda *_a, **_k: {})
    monkeypatch.setattr(turn_delegation, "_runtime_declared_child_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(
        turn_delegation,
        "_agent_definition_uses_blueprint_runtime",
        lambda _agent_def: False,
    )

    token = _ctx.set_app(app)
    try:
        asyncio.run(turn_delegation.run_dynamic_agent_sync(state, agent_def, "search"))
    finally:
        _ctx.reset(token)

    assert observed["leases"] == {root: 1}
    assert observed["reaped"] == []
    assert executor.closed is False
    assert leases == {}, "lease must be released after the delegated worker settles"


def test_forward_turn_pins_workspace_across_sequential_delegations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catalog-to-runtime gaps remain pinned until the whole turn settles."""

    import asyncio
    from types import SimpleNamespace

    from clio_agent.gact import context as _ctx
    from clio_agent.gact import turn_forward
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    root = str(tmp_path)
    executor = FakeExecutor(idle_s=10_000)
    agent = _bare_agent()
    _lock, executors, leases = agent._workspace_state()
    executors[root] = executor
    session = SimpleNamespace(workspace_id="workspace-1")
    workspace = SimpleNamespace(root_path=root)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda _sid: session),
            workspaces=SimpleNamespace(get=lambda _wid: workspace),
        )
    )
    state: Any = SimpleNamespace(app=app, sid="session-1")
    observed: list[tuple[str, dict[str, int], list[str]]] = []

    async def run_catalog_then_runtime(_state: Any) -> object:
        for stage in ("catalog", "runtime"):
            reaped = _reaper(
                executors,
                leases=leases,
                ttl=0.0,
            ).reap_once()
            observed.append((stage, dict(leases), reaped))
            await asyncio.sleep(0)
        return object()

    monkeypatch.setattr(turn_forward, "_forward_turn_leased", run_catalog_then_runtime)
    token = _ctx.set_app(app)
    try:
        asyncio.run(turn_forward.forward_turn(state))
    finally:
        _ctx.reset(token)

    assert observed == [
        ("catalog", {root: 1}, []),
        ("runtime", {root: 1}, []),
    ]
    assert executor.closed is False
    assert leases == {}
    assert _reaper(executors, leases=leases, ttl=0.0).reap_once() == [root]
    assert executor.closed is True


def test_cancelled_waiter_keeps_delegated_worker_workspace_leased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the asyncio waiter must not unpin a live sync worker."""

    import asyncio
    from types import SimpleNamespace

    from clio_agent.gact import context as _ctx
    from clio_agent.gact import turn_delegation
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    root = str(tmp_path)
    executor = FakeExecutor(idle_s=10_000)
    agent = _bare_agent()
    _lock, executors, leases = agent._workspace_state()
    executors[root] = executor
    session = SimpleNamespace(workspace_id="workspace-1")
    workspace = SimpleNamespace(root_path=root)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=agent,
            sessions=SimpleNamespace(get=lambda _sid: session),
            workspaces=SimpleNamespace(get=lambda _wid: workspace),
        )
    )
    state: Any = SimpleNamespace(app=app, sid="session-1")
    agent_def: Any = SimpleNamespace(id="runtime")
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def run_child(*_args: Any, **_kwargs: Any) -> Any:
        worker_started.set()
        try:
            assert release_worker.wait(timeout=10), "test did not release delegated worker"
            return SimpleNamespace(answer="")
        finally:
            worker_finished.set()

    async def await_work(_state: Any, work: Any) -> Any:
        return await work

    monkeypatch.setattr(
        "clio_agent.gact.app._blueprint_runner_for_agent",
        lambda _agent_def: object(),
    )
    monkeypatch.setattr(turn_delegation, "_run_dynamic_agent_compat", run_child)
    monkeypatch.setattr(turn_delegation, "await_turn_work", await_work)

    async def drive() -> None:
        task = asyncio.create_task(turn_delegation.run_dynamic_agent_sync(state, agent_def, "run"))
        assert await asyncio.to_thread(worker_started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert leases == {root: 1}
        assert _reaper(executors, leases=leases, ttl=0.0).reap_once() == []
        assert executor.closed is False

        release_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 5)
        for _ in range(100):
            if not leases:
                break
            await asyncio.sleep(0.01)
        assert leases == {}

    token = _ctx.set_app(app)
    try:
        asyncio.run(drive())
    finally:
        release_worker.set()
        _ctx.reset(token)

    assert _reaper(executors, leases=leases, ttl=0.0).reap_once() == [root]
    assert executor.closed is True
