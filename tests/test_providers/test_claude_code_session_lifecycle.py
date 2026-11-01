"""Provider pool algorithms and release after actual session-worker retirement.

Normal completion, failure and Stop share the conversation runner's retirement
boundary. The retained pool tests cover nonblocking eviction, in-flight safety
and provider dispatch independently of deleted task folds.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.turn_runner import TurnRunner
from clio_agent.providers import claude_code_lifecycle as cc_lifecycle
from clio_agent.providers import claude_code_sessions as ccs
from clio_agent.providers import session_lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["normal", "failure", "stop"])
async def test_turn_retirement_releases_provider_once_after_worker_exit(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled waiter cannot release provider ownership while its worker lives.

    A clean turn end keeps the connection for the session's next turn (the pool's B1
    contract); a failed or stopped turn releases it, once, after its worker exits.
    """
    app = SimpleNamespace(state=SimpleNamespace())
    entered, released = asyncio.Event(), asyncio.Event()
    work_release, worker_returned = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    calls: list[str] = []
    admitted = False
    sid = "retirement_child"

    def work() -> None:
        try:
            loop.call_soon_threadsafe(entered.set)
            if outcome == "stop" and not work_release.wait(5):
                raise TimeoutError("The test did not release the owned worker")
            if outcome == "failure":
                raise RuntimeError("Injected model failure")
        finally:
            worker_returned.set()

    def release(session_id: str) -> None:
        assert worker_returned.is_set()
        assert not any(
            thread.is_alive() and thread.name == "clio-session-" + sid
            for thread in threading.enumerate()
        )
        calls.append(session_id)
        loop.call_soon_threadsafe(released.set)

    async def run() -> None:
        await runner.run_sync(sid, work)

    async def admit(session_id: str) -> tuple[str, Coroutine[Any, Any, None]] | None:
        nonlocal admitted
        assert session_id == sid
        if admitted:
            return None
        admitted = True
        return "retirement_turn", run()

    monkeypatch.setattr(session_lifecycle, "release_session_resources", release)
    runner = TurnRunner(app, admit)
    runner.bind_loop(loop)
    runner.signal(sid)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if outcome == "stop":
            stopping = asyncio.create_task(runner.stop(sid))
            await asyncio.sleep(0)
            assert calls == [] and not stopping.done()
            work_release.set()
            await stopping
        if outcome != "normal":
            await asyncio.wait_for(released.wait(), 2)
        else:  # let the clean turn end before shutdown, which counts as a stop
            for _ in range(200):
                if not runner.busy(sid):
                    break
                await asyncio.sleep(0.01)
    finally:
        work_release.set()
        runner.close_admission()
        await runner.drain()
    assert calls == ([] if outcome == "normal" else [sid]) and not runner.busy(sid)


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    ccs._reset_sessions_for_tests()
    yield
    ccs._reset_sessions_for_tests()


@pytest.fixture
def _isolated_lifecycle_providers() -> Any:
    """Save + restore the REAL registered providers (production wiring --
    e.g. ``ClaudeStreamClientPool.release_session_resources``, registered
    once at module load) around a test that wants a clean, controlled
    provider list. ``reset_for_tests()`` must never permanently wipe the
    production registration for tests that run later in the same process.
    """
    saved = list(session_lifecycle._PROVIDERS)
    session_lifecycle.reset_for_tests()
    yield
    with session_lifecycle._GUARD:
        session_lifecycle._PROVIDERS.clear()
        session_lifecycle._PROVIDERS.extend(saved)


# --------------------------------------------------------------------------- #
# B2 BLOCKER (#1305 round 3): release_session_resources_nonblocking must announce
# the dropped client synchronously, exactly like reap_idle_session_entry does for
# the idle-TTL path (pinned in test_claude_code_idle_reap.py), so the Claude Code
# engine resets that session's kept conversations.
# --------------------------------------------------------------------------- #
def test_release_session_resources_forces_the_next_stateful_send_to_a_full_resend() -> None:
    """Without this, the engine still thinks its conversation is live on the
    now-dead subprocess, and the next send would plan a DELTA (append-only tail)
    shipped to a fresh subprocess that never saw the prefix -- a silent
    conversation-coherence bug, not merely a reconnect.

    SABOTAGE: drop ``entry.announce_dropped()`` from
    ``release_session_resources_nonblocking`` (the non-blocking close announces too
    late) -> the next ``plan()`` is a delta, and this goes red.
    """
    from dspy.lm15 import Message, Request

    from clio_agent.providers import claude_code_engine

    registry = claude_code_engine._CONVERSATIONS
    registry.clear_for_tests()
    try:
        pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
        key = ("sess-1", "main#react", "m", "/w", "")
        registry.opened(
            key, "sdk-session-1", Request(model="claude_code/m", messages=(Message.user("a"),)), "s"
        )

        entry = pool.entry_for(session_id="sess-1")
        entry._bound_session = "sess-1"  # what a real connect records

        pool.release_session_resources("sess-1")

        extended = Request(
            model="claude_code/m",
            messages=(Message.user("a"), Message.assistant("ok"), Message.user("b")),
        )
        send = registry.plan(key, extended, "s")
        assert (send.handle, send.reason) == (None, "session_evicted")
    finally:
        registry.clear_for_tests()


def test_release_session_resources_emits_a_typed_row_distinct_from_idle_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit reason for a #1305 lifecycle release must be its OWN typed
    reason, never ``idle_reaped`` -- misattributing a deterministic release
    as an idle-TTL reap would misrepresent WHY the connection actually died.
    """
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(session_id="sess-1")

    pool.release_session_resources("sess-1")

    released = [r for r in rows if r.get("reason") == "session_lifecycle_released"]
    assert released  # SABOTAGE: drop the stream_audit call -> empty -> red
    assert released[0]["category"] == "session_lifecycle_release"
    assert not any(r.get("reason") == "idle_reaped" for r in rows)


# --------------------------------------------------------------------------- #
# ClaudeStreamClientPool.release_session_resources -- the claude_code leg.
# --------------------------------------------------------------------------- #
def test_release_session_resources_closes_and_drops_the_entry() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(session_id="sess-1")

    pool.release_session_resources("sess-1")

    # SABOTAGE: make release_session_resources a no-op -> the entry survives -> red.
    assert pool._entries.get("sess-1") is None


def test_release_session_resources_never_touches_a_sibling_session() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(session_id="sess-1")
    sibling = pool.entry_for(session_id="sess-2")

    pool.release_session_resources("sess-1")

    # SABOTAGE: release every entry regardless of key -> sibling is gone too -> red.
    assert pool.entry_for(session_id="sess-2") is sibling


def test_release_session_resources_is_a_noop_for_an_unknown_session() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.release_session_resources("nonexistent")  # must not raise


# --------------------------------------------------------------------------- #
# Resurrection: a released session reconnects cleanly on its next use.
# --------------------------------------------------------------------------- #
def test_released_session_resurrects_a_fresh_entry_on_next_use() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(session_id="sess-1")

    pool.release_session_resources("sess-1")
    fresh = pool.entry_for(session_id="sess-1")

    # SABOTAGE: entry_for stops minting a NEW entry once a key was ever seen
    # (a stale reuse-after-release cache) -> `fresh is entry` -> red.
    assert fresh is not entry


async def test_released_session_resurrects_and_reconnects_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end resurrection through the REAL connect path (fake SDK): a
    released session's next call reconnects cleanly and works."""
    import sys
    from types import ModuleType

    state = {"connected": 0}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            state["connected"] += 1

        async def disconnect(self) -> None:
            return None

    fake_sdk = ModuleType("claude_agent_sdk")
    monkeypatch.setattr(fake_sdk, "ClaudeAgentOptions", FakeOptions, raising=False)
    monkeypatch.setattr(fake_sdk, "ClaudeSDKClient", FakeClient, raising=False)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(session_id="sess-1")
    await entry._ensure_client(lambda: None, gact_session_id="sess-1", model="m")
    assert state["connected"] == 1

    pool.release_session_resources("sess-1")

    fresh = pool.entry_for(session_id="sess-1")
    await fresh._ensure_client(lambda: None, gact_session_id="sess-1", model="m")
    # SABOTAGE: a released-but-not-truly-disconnected entry would report only
    # 1 connect total (the stale client silently reused) -> red.
    assert state["connected"] == 2


# --------------------------------------------------------------------------- #
# F1 (#1305 review round): release_session_resources MUST NOT block -- it
# runs on the server's own event loop (the task done-callback chain), and
# close_blocking's up-to-15s wait would stall every other coroutine on that
# loop.
# --------------------------------------------------------------------------- #
def test_release_session_resources_uses_close_nonblocking_not_close_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: call ``entry.close_blocking()`` instead of the non-blocking
    method -> ``blocking_calls`` gets an entry -> red.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(session_id="sess-1")
    blocking_calls: list[int] = []
    nonblocking_calls: list[int] = []
    monkeypatch.setattr(entry, "close_blocking", lambda: blocking_calls.append(1))
    monkeypatch.setattr(entry, "close_nonblocking", lambda: nonblocking_calls.append(1))

    pool.release_session_resources("sess-1")

    assert blocking_calls == []
    assert nonblocking_calls == [1]


# --------------------------------------------------------------------------- #
# F2a: the in-flight guard -- a genuinely mid-stream entry is left untouched.
# --------------------------------------------------------------------------- #
def test_release_session_resources_defers_a_genuinely_in_flight_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: drop the ``entry.idle_for() is None`` guard from
    ``release_session_resources_nonblocking`` -> the busy entry is popped +
    closed anyway -> both assertions below go red.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(session_id="sess-1")
    entry._mark_busy()  # genuinely mid-stream -- never reap-eligible

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )

    pool.release_session_resources("sess-1")

    assert pool._entries.get("sess-1") is entry  # untouched
    assert entry._dead is False
    deferred = [r for r in rows if r.get("reason") == "session_release_deferred_in_flight"]
    assert deferred
    assert deferred[0]["category"] == "session_release_deferred"
    assert deferred[0]["session_id"] == "sess-1"


# --------------------------------------------------------------------------- #
# F6b: the orphaned-entry window -- a release landing between entry_for()
# and the caller's own connect must refuse (typed, retryable), never
# silently reconnect a slot+CLI invisible to the pool.
# --------------------------------------------------------------------------- #
async def test_dead_entry_refuses_a_connect_after_being_released_mid_flight() -> None:
    """SABOTAGE: drop the ``self._dead`` check from ``_ensure_client`` -> the
    late caller silently reconnects on the orphaned entry -> no exception ->
    this goes red.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(session_id="sess-1")
    # Simulate the release landing in the window between entry_for() and the
    # caller actually starting stream()/_ensure_client() (a genuine
    # cross-thread race: the release runs on the server loop, the caller may
    # be on a different executor thread).
    pool.release_session_resources("sess-1")

    assert entry._dead is True
    with pytest.raises(RuntimeError, match=cc_lifecycle.DEAD_ENTRY_MARKER):
        await entry._ensure_client(lambda: None, model="m")


# --------------------------------------------------------------------------- #
# F2 (strand fix, structural pin): STREAM_END must be queued strictly BEFORE
# the abnormal-end reset in _pump's finally -- a cross-thread lifecycle
# release stopping the owner loop mid-reset must never be able to prevent
# END from ever being published to the asynchronous stream consumer.
# --------------------------------------------------------------------------- #
async def test_pump_queues_stream_end_before_the_abnormal_end_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: swap the two statements back to the pre-#1305 order (reset
    first, END second) -> the recorded call order comes out
    ["reset", "end"] instead of ["end", "reset"] -> this goes red.
    """
    import sys
    from types import ModuleType

    from clio_agent.providers import stream_queue

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            pass

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            raise RuntimeError("boom")  # forces an abnormal (clean=False) end

        async def receive_response(self) -> Any:
            return
            yield  # pragma: no cover - never reached

    fake_sdk = ModuleType("claude_agent_sdk")
    monkeypatch.setattr(fake_sdk, "ClaudeAgentOptions", FakeOptions, raising=False)
    monkeypatch.setattr(fake_sdk, "ClaudeSDKClient", FakeClient, raising=False)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    entry = ccs._StreamClientEntry()

    calls: list[str] = []
    original_areset_client = entry._areset_client

    async def spy_areset_client() -> None:
        calls.append("reset")
        await original_areset_client()

    monkeypatch.setattr(entry, "_areset_client", spy_areset_client)

    class SpyStreamQueue(stream_queue.AsyncStreamQueue[tuple[Any, Any]]):
        def put(self, item: tuple[Any, Any]) -> None:
            """Record publication order while retaining the real queue behavior."""
            if item[0] is ccs._STREAM_END:
                calls.append("end")
            super().put(item)

    monkeypatch.setattr(stream_queue, "AsyncStreamQueue", SpyStreamQueue)

    with pytest.raises(RuntimeError, match="boom"):
        out: list[Any] = []
        async for msg in entry.stream(
            payload="p",
            native_blocks=[],
            session_id="s",
            idle_timeout=None,
            on_construct=lambda: None,
        ):
            out.append(msg)

    assert calls == ["end", "reset"]


# --------------------------------------------------------------------------- #
# providers/session_lifecycle.py: the generic dispatcher.
# --------------------------------------------------------------------------- #
def test_register_session_lifecycle_provider_is_idempotent_by_identity(
    _isolated_lifecycle_providers: Any,
) -> None:
    calls: list[str] = []

    def release(sid: str) -> None:
        calls.append(sid)

    session_lifecycle.register_session_lifecycle_provider(release)
    session_lifecycle.register_session_lifecycle_provider(release)  # same object again

    session_lifecycle.release_session_resources("s1")

    # SABOTAGE: append unconditionally in register_...provider -> called twice -> red.
    assert calls == ["s1"]


def test_release_session_resources_is_best_effort_across_providers(
    _isolated_lifecycle_providers: Any,
) -> None:
    """One provider's release failure must not stop another's, and must never
    propagate into the caller (a task-completion path)."""
    calls: list[str] = []

    def bad(_sid: str) -> None:
        raise RuntimeError("boom")

    def good(sid: str) -> None:
        calls.append(sid)

    session_lifecycle.register_session_lifecycle_provider(bad)
    session_lifecycle.register_session_lifecycle_provider(good)

    session_lifecycle.release_session_resources("s1")  # must not raise

    # SABOTAGE: let one provider's exception abort the dispatch loop -> `good`
    # never runs -> red.
    assert calls == ["s1"]


def test_release_session_resources_noop_for_empty_session_id(
    _isolated_lifecycle_providers: Any,
) -> None:
    calls: list[str] = []
    session_lifecycle.register_session_lifecycle_provider(lambda sid: calls.append(sid))

    session_lifecycle.release_session_resources("")

    assert calls == []


def test_stream_client_pool_release_is_registered_with_session_lifecycle() -> None:
    """The static module-load registration actually wired the pool's own
    release method into the generic dispatcher (production wiring, not
    reachable through ``_isolated_lifecycle_providers``)."""
    assert ccs._STREAM_CLIENT_POOL.release_session_resources in session_lifecycle._PROVIDERS


def test_release_session_resources_logs_typed_reason_with_zero_providers(
    _isolated_lifecycle_providers: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """F9: a dispatch with ZERO registered providers must be distinguishable
    (typed, logged) from a provider running and finding nothing to release
    for a given session -- both are silent from the caller's perspective, but
    only one should look like "nobody is listening at all".

    SABOTAGE: drop the ``if not providers:`` branch -> no debug row is
    logged when the provider list is empty -> this goes red.
    """
    import logging

    with caplog.at_level(logging.DEBUG, logger="clio_agent.providers.session_lifecycle"):
        session_lifecycle.release_session_resources("sess-orphan")

    assert any(
        session_lifecycle.NO_PROVIDERS_REGISTERED_REASON in record.getMessage()
        for record in caplog.records
    )
