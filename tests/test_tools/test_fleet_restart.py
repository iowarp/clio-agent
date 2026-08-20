"""Drain-aware fleet restart (#1033): the primitive that makes ``grant_applied_live`` true.

A workspace-shared fleet child is spawned once and keeps its compile-time write territory, so
a mid-session write-root grant would never reach it until it respawned. ``request_restart``
closes+evicts an IDLE fleet now (lazy rebuild picks up the widened territory) and DEFERS a
busy/leased one to the reaper's idle pass — it must NEVER tear down a fleet with a call in
flight. It also wires the previously-unused ``close_child_channel`` seam so per-child net
listeners stop leaking on every teardown (reap OR restart).
"""

from __future__ import annotations

import threading

from clio_agent.tools.reaper import (
    RESTART_DEFERRED_BUSY,
    RESTART_NO_RESIDENT,
    RESTART_RESTARTED_LIVE,
    WorkspaceExecutorReaper,
)


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


class ProbeExplodingExecutor:
    """busy probe raises — the restart must fail SAFE (defer, never close)."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def busy(self) -> bool:
        raise RuntimeError("probe blew up")

    def idle_for(self) -> float:
        raise RuntimeError("probe blew up")

    def close(self) -> None:
        self.closed = True


def _reaper(registry, *, leases=None, closer=None, ttl=10_000.0, cap=100):
    closed_roots: list[str] = []

    def _default_closer(root: str) -> int:
        closed_roots.append(root)
        return 1

    reaper = WorkspaceExecutorReaper(
        registry,
        threading.Lock(),
        leases=leases if leases is not None else {},
        ttl_s=ttl,
        max_resident=cap,
        tick_s=3600,  # never ticks on its own
        channel_closer=closer or _default_closer,
    )
    reaper._closed_roots = closed_roots  # type: ignore[attr-defined] — test observation
    return reaper


# --------------------------------------------------------------------------- #
# request_restart: idle → live, busy/leased → deferred, empty → no resident
# --------------------------------------------------------------------------- #


def test_request_restart_idle_closes_evicts_and_closes_channels() -> None:
    executor = FakeExecutor(idle_s=5)
    registry = {"/ws/r": executor}
    reaper = _reaper(registry)

    assert reaper.request_restart("/ws/r") == RESTART_RESTARTED_LIVE
    assert executor.closed and executor.close_calls == 1
    assert "/ws/r" not in registry, "idle fleet must be evicted so the next spawn rebuilds it"
    assert reaper._closed_roots == ["/ws/r"], "per-child net channels must be closed on restart"


def test_request_restart_busy_defers_and_never_closes() -> None:
    busy = FakeExecutor(idle_s=10_000, busy=True)
    registry = {"/ws/b": busy}
    reaper = _reaper(registry)

    assert reaper.request_restart("/ws/b") == RESTART_DEFERRED_BUSY
    assert not busy.closed, "a busy executor must NEVER be closed mid-call"
    assert "/ws/b" in registry
    assert "/ws/b" in reaper._pending_restarts
    assert reaper._closed_roots == [], "no channel close while the fleet is still live"


def test_request_restart_leased_defers() -> None:
    leased = FakeExecutor(idle_s=10_000)
    registry = {"/ws/turn": leased}
    reaper = _reaper(registry, leases={"/ws/turn": 1})

    assert reaper.request_restart("/ws/turn") == RESTART_DEFERRED_BUSY
    assert not leased.closed
    assert "/ws/turn" in reaper._pending_restarts


def test_request_restart_no_resident_child() -> None:
    reaper = _reaper({})
    assert reaper.request_restart("/ws/missing") == RESTART_NO_RESIDENT
    assert reaper._pending_restarts == set()


def test_request_restart_probe_failure_fails_safe_deferred() -> None:
    broken = ProbeExplodingExecutor()
    registry = {"/ws/x": broken}
    reaper = _reaper(registry)

    assert reaper.request_restart("/ws/x") == RESTART_DEFERRED_BUSY
    assert not broken.closed, "an unjudgeable executor is deferred, never closed"
    assert "/ws/x" in reaper._pending_restarts


# --------------------------------------------------------------------------- #
# drain: a deferred restart fires on the reaper's next idle pass
# --------------------------------------------------------------------------- #


def test_deferred_restart_drains_on_idle_pass() -> None:
    executor = FakeExecutor(idle_s=5, busy=True)
    registry = {"/ws/d": executor}
    reaper = _reaper(registry, ttl=10_000.0)  # ttl high: only the restart pass can close it

    assert reaper.request_restart("/ws/d") == RESTART_DEFERRED_BUSY
    # Still busy: the drain pass must NOT close it.
    assert reaper.reap_once() == []
    assert not executor.closed and "/ws/d" in registry

    # Call finished → idle. The drain closes it (reason grant_restart), NOT idle_ttl
    # (ttl is far higher than idle), and closes its channels.
    executor.busy = False
    assert reaper.reap_once() == ["/ws/d"]
    assert executor.closed and "/ws/d" not in registry
    assert "/ws/d" not in reaper._pending_restarts
    assert reaper._closed_roots == ["/ws/d"]


def test_pending_restart_dropped_when_no_longer_resident() -> None:
    executor = FakeExecutor(idle_s=10_000, busy=True)
    registry = {"/ws/g": executor}
    reaper = _reaper(registry)
    assert reaper.request_restart("/ws/g") == RESTART_DEFERRED_BUSY

    # Something else evicted it (e.g. an LRU reap on another path); the pending flag
    # must clear on the next pass rather than trying to close a ghost.
    registry.pop("/ws/g")
    assert reaper.reap_once() == []
    assert reaper._pending_restarts == set()
    assert reaper._closed_roots == []


# --------------------------------------------------------------------------- #
# the leak fix: a plain idle reap ALSO closes the fleet's net channels
# --------------------------------------------------------------------------- #


def test_idle_reap_closes_net_channels() -> None:
    stale = FakeExecutor(idle_s=500)
    registry = {"/ws/s": stale}
    reaper = _reaper(registry, ttl=100.0)

    assert reaper.reap_once() == ["/ws/s"]
    assert stale.closed
    assert reaper._closed_roots == ["/ws/s"], "reaped fleet's per-child channels must be closed"


def test_channel_closer_error_never_aborts_teardown() -> None:
    def _boom(root: str) -> int:
        raise RuntimeError("channel close blew up")

    stale = FakeExecutor(idle_s=500)
    registry = {"/ws/s": stale}
    reaper = _reaper(registry, ttl=100.0, closer=_boom)

    # The executor still closes + evicts; the channel-close error is typed, not fatal.
    assert reaper.reap_once() == ["/ws/s"]
    assert stale.closed and registry == {}


def test_reap_close_error_still_closes_net_channels() -> None:
    """#1033 leak-fix (error path): if executor.close() raises, the fleet is still popped, so its
    per-child net channels MUST still close — else they leak toward _MAX_CHILD_CHANNELS while the
    next lazy rebuild orphans their ids. Symmetric with request_restart's close-error handling."""

    class CloseExplodingExecutor(FakeExecutor):
        def close(self) -> None:  # already popped from the registry; close raises
            self.close_calls += 1
            raise RuntimeError("close blew up")

    stale = CloseExplodingExecutor(idle_s=500)
    registry = {"/ws/s": stale}
    reaper = _reaper(registry, ttl=100.0)

    # close() raised → the root is not counted as cleanly reaped, but it IS evicted and its
    # per-child channels are closed anyway (the leak fix must cover the error path too).
    assert reaper.reap_once() == []
    assert stale.close_calls == 1 and registry == {}
    assert reaper._closed_roots == ["/ws/s"], (
        "channels must close even when executor.close() raises"
    )


# --------------------------------------------------------------------------- #
# ClioAgent.request_fleet_restart delegation → the next executor rebuilds live
# --------------------------------------------------------------------------- #


def test_agent_request_fleet_restart_evicts_then_rebuilds(monkeypatch) -> None:
    from clio_agent.tools.execution import tool_workspace_context
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    agent = _bare_agent()
    lock, executors, leases = agent._workspace_state()
    agent._workspace_reaper = WorkspaceExecutorReaper(
        executors, lock, leases=leases, tick_s=3600, channel_closer=lambda root: 0
    )

    built: list[str] = []

    class RebuiltExecutor:
        def __init__(self, gateway) -> None:
            self.gateway = gateway
            self.closed = False
            self.busy = False

        def idle_for(self) -> float:
            return 0.0

        def close(self) -> None:
            self.closed = True

    def fake_build(*, cwd=None, set_catalog=False, blueprint_id=""):
        built.append(cwd)
        return f"gateway:{cwd}"

    monkeypatch.setattr(agent, "_build_tool_gateway", fake_build)
    monkeypatch.setattr(
        "clio_agent.agent.create_sync_tool_executor", lambda gw, **k: RebuiltExecutor(gw)
    )
    monkeypatch.setattr("clio_agent.agent.namespace_proxies", lambda gw: {})

    with tool_workspace_context("/ws/r"):
        first = agent._active_tool_executor()  # builds + caches the fleet
        assert agent.request_fleet_restart("/ws/r") == RESTART_RESTARTED_LIVE
        assert first.closed, "the idle fleet was closed by the restart"
        second = agent._active_tool_executor()  # rebuilds with the widened territory

    assert second is not first, "a restarted fleet must be rebuilt, not handed out closed"
    assert built == ["/ws/r", "/ws/r"], "the rebuild re-invokes the gateway builder (new roots)"


def test_agent_request_fleet_restart_without_reaper_is_typed_no_resident() -> None:
    from tests.test_gact.test_workspace_tool_executor import (
        _bare_agent,  # type: ignore[attr-defined]
    )

    agent = _bare_agent()  # a bare stub has no reaper — a typed skip, never a silent success
    assert agent.request_fleet_restart("/ws/x") == RESTART_NO_RESIDENT
    assert agent.request_fleet_restart("") == RESTART_NO_RESIDENT
