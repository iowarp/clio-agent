"""#1305 owner ruling: connection lifetime = subagent lifetime.

A subagent's provider connection must die DETERMINISTICALLY the moment its
work is done -- never left to the idle-TTL sweep alone. This module pins the
generic seam (:mod:`clio_agent.providers.session_lifecycle`), its claude_code
consumer (:meth:`ClaudeStreamClientPool.release_session_resources`, the
non-blocking, in-flight-safe ABNORMAL-termination backstop --
:mod:`clio_agent.providers.claude_code_lifecycle`), and the FOUR GACT-level
terminal paths that dispatch it via the shared
``gact/turn_spawn.py::finalize_child_task_terminal`` helper (#1305 review
round F3: the completion fold, the cancel cascade, and both
``child_forward.py`` HITL-edge terminals). Each pin carries an inline
SABOTAGE note.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.gact.agent_tasks as agent_tasks
import clio_agent.gact.child_forward as child_forward
import clio_agent.gact.task_fold as task_fold
import clio_agent.gact.turn_spawn as turn_spawn
from clio_agent.gact.agent_tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    AgentTask,
    seed_agent_task,
)
from clio_agent.gact.agents.invoker import TaskEvent
from clio_agent.gact.app import build_app
from clio_agent.providers import claude_code_lifecycle as cc_lifecycle
from clio_agent.providers import claude_code_sessions as ccs
from clio_agent.providers import session_lifecycle


class _Agent:
    """Minimal host agent; folded tasks never launch an in-process child turn."""

    def forward(self, question: str, session_id: str, **_kwargs: object) -> object:
        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


def _task_event(task: AgentTask, *, session_id: str | None = None) -> TaskEvent:
    return TaskEvent(
        event_type=agent_tasks.AGENT_TASK_EVENTS[task.status],
        task_id=task.task_id,
        session_id=session_id or task.child_session_id,
        status=task.status,
        payload=asdict(task),
    )


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
# ClaudeStreamClientPool: session<->scope ownership bookkeeping (#1305).
# --------------------------------------------------------------------------- #
def test_entry_for_records_session_ownership_for_a_scoped_entry() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1")
    # SABOTAGE: drop the note_scope_owner call from entry_for -> both empty -> red
    assert pool._session_scopes.get("sess-1") == {"loop-a"}
    assert pool._scope_session.get("loop-a") == "sess-1"


def test_entry_for_records_nothing_without_a_gact_session_id() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a")  # no gact_session_id
    assert pool._session_scopes == {}
    assert pool._scope_session == {}


def test_entry_for_never_tracks_ownership_for_the_shared_base_entry() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(
        model="m", cwd="/w", thinking=None, gact_session_id="sess-1"
    )  # scope=None -> base
    assert pool._session_scopes == {}
    assert pool._scope_session == {}


# --------------------------------------------------------------------------- #
# ClaudeStreamClientPool.release_session_resources -- the claude_code leg.
# --------------------------------------------------------------------------- #
def test_release_session_resources_closes_and_drops_the_scoped_entry() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1")

    pool.release_session_resources("sess-1")

    # SABOTAGE: make release_session_resources a no-op -> the entry survives -> red.
    assert pool._entries.get(("m", "/w", None, "loop-a")) is None
    assert pool._session_scopes == {}
    assert pool._scope_session == {}


def test_release_session_resources_never_touches_a_sibling_sessions_scope() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1")
    sibling = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-b", gact_session_id="sess-2"
    )

    pool.release_session_resources("sess-1")

    # SABOTAGE: release EVERY scope-keyed entry regardless of owner -> sibling
    # is gone too -> red.
    assert pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-b") is sibling
    assert pool._session_scopes.get("sess-2") == {"loop-b"}


def test_release_session_resources_never_touches_the_shared_base_entry() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    base = pool.entry_for(model="m", cwd="/w", thinking=None)  # scope=None -> the shared base entry
    pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1")

    pool.release_session_resources("sess-1")

    # SABOTAGE: release the base entry too (it could be serving OTHER
    # sessions concurrently) -> red.
    assert pool.entry_for(model="m", cwd="/w", thinking=None) is base


def test_release_session_resources_is_a_noop_for_an_unknown_session() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.release_session_resources("nonexistent")  # must not raise


def test_release_via_scope_also_clears_the_1305_bookkeeping() -> None:
    """The PRE-EXISTING ``stateful_scope`` teardown path (``pool.release(scope)``)
    must ALSO clear the #1305 session<->scope bookkeeping, so a LATER
    ``release_session_resources`` call (e.g. the new GACT hook firing after
    the react loop's own scope teardown already ran) is a clean no-op --
    never a double-close, never a leaked mapping.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    pool.entry_for(model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1")

    pool.release("loop-a")  # the pre-existing scope-registry teardown path

    # SABOTAGE: drop forget_scope_owner from release() -> the mapping survives
    # (a leak) -> red.
    assert pool._session_scopes == {}
    assert pool._scope_session == {}
    pool.release_session_resources("sess-1")  # must be a clean no-op, not raise


# --------------------------------------------------------------------------- #
# Resurrection: a released scope reconnects cleanly on its next use.
# --------------------------------------------------------------------------- #
def test_released_session_resurrects_a_fresh_entry_on_next_use() -> None:
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )

    pool.release_session_resources("sess-1")
    fresh = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )

    # SABOTAGE: entry_for stops minting a NEW entry once a key was ever seen
    # (a stale reuse-after-release cache) -> `fresh is entry` -> red.
    assert fresh is not entry
    assert pool._session_scopes.get("sess-1") == {"loop-a"}  # re-tracked on the new use


async def test_released_session_resurrects_and_reconnects_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end resurrection through the REAL connect path (fake SDK): a
    released scope's next call reconnects cleanly and works."""
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
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )
    await entry._ensure_client(lambda: None, gact_session_id="sess-1")
    assert state["connected"] == 1

    pool.release_session_resources("sess-1")

    fresh = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )
    await fresh._ensure_client(lambda: None, gact_session_id="sess-1")
    # SABOTAGE: a released-but-not-truly-disconnected entry would report only
    # 1 connect total (the stale client silently reused) -> red.
    assert state["connected"] == 2


# --------------------------------------------------------------------------- #
# F1 (#1305 review round): release_session_resources MUST NOT block -- it
# runs on the server's own event loop (the task done-callback chain), and
# close_blocking's up-to-15s-per-entry wait would stall every other
# coroutine on that loop.
# --------------------------------------------------------------------------- #
def test_release_session_resources_uses_close_nonblocking_not_close_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: call ``self.release(scope)`` (close_blocking) instead of the
    non-blocking free function -> ``blocking_calls`` gets an entry -> red.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )
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
    entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )
    entry._mark_busy()  # genuinely mid-stream -- never reap-eligible

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ccs, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        ccs, "stream_audit", lambda event, **fields: rows.append({"event": event, **fields})
    )

    pool.release_session_resources("sess-1")

    assert pool._entries.get(("m", "/w", None, "loop-a")) is entry  # untouched
    assert entry._dead is False
    deferred = [r for r in rows if r.get("reason") == "session_release_deferred_in_flight"]
    assert deferred
    assert deferred[0]["category"] == "session_release_deferred"
    assert deferred[0]["session_id"] == "sess-1"


def test_release_session_resources_closes_an_idle_entry_alongside_a_busy_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-flight guard is per-entry, not all-or-nothing for the session:
    an idle entry still gets released even when a SIBLING scope for the
    SAME session is genuinely busy."""
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    busy_entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-busy", gact_session_id="sess-1"
    )
    idle_entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-idle", gact_session_id="sess-1"
    )
    busy_entry._mark_busy()

    pool.release_session_resources("sess-1")

    assert pool._entries.get(("m", "/w", None, "loop-busy")) is busy_entry
    assert pool._entries.get(("m", "/w", None, "loop-idle")) is None
    assert idle_entry._dead is True


# --------------------------------------------------------------------------- #
# F6b: the orphaned-entry window -- a release landing between entry_for()
# and the caller's own connect must refuse (typed, retryable), never
# silently reconnect a slot+CLI invisible to sweep/close.
# --------------------------------------------------------------------------- #
async def test_dead_entry_refuses_a_connect_after_being_released_mid_flight() -> None:
    """SABOTAGE: drop the ``self._dead`` check from ``_ensure_client`` -> the
    late caller silently reconnects on the orphaned entry -> no exception ->
    this goes red.
    """
    pool = ccs.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(
        model="m", cwd="/w", thinking=None, scope="loop-a", gact_session_id="sess-1"
    )
    # Simulate the release landing in the window between entry_for() and the
    # caller actually starting stream()/_ensure_client() (a genuine
    # cross-thread race: the release runs on the server loop, the caller may
    # be on a different executor thread).
    pool.release_session_resources("sess-1")

    assert entry._dead is True
    with pytest.raises(RuntimeError, match=cc_lifecycle.DEAD_ENTRY_MARKER):
        await entry._ensure_client(lambda: None)


def test_dead_entry_marker_is_a_recognized_transient_reason() -> None:
    """The retry layer must classify a dead-entry refusal as transient (so
    the LM retry loop re-issues on a fresh entry_for() instead of failing
    the turn) -- pins the lm.io_logging marker-sync contract F6b relies on.
    """
    from clio_agent.lm.io_logging import _is_transient_provider_error

    assert _is_transient_provider_error(RuntimeError(cc_lifecycle.dead_entry_error_message()))


# --------------------------------------------------------------------------- #
# F2 (strand fix, structural pin): STREAM_END must be queued strictly BEFORE
# the abnormal-end reset in _pump's finally -- a cross-thread lifecycle
# release stopping the owner loop mid-reset must never be able to prevent
# END from ever being queued (permanently stranding the consumer's
# unbounded ``chunks.get()`` worker thread).
# --------------------------------------------------------------------------- #
async def test_pump_queues_stream_end_before_the_abnormal_end_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: swap the two statements back to the pre-#1305 order (reset
    first, END second) -> the recorded call order comes out
    ["reset", "end"] instead of ["end", "reset"] -> this goes red.
    """
    import queue as queue_module
    import sys
    from types import ModuleType

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
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    entry = ccs._StreamClientEntry(lambda: object())

    calls: list[str] = []
    original_areset_client = entry._areset_client

    async def spy_areset_client() -> None:
        calls.append("reset")
        await original_areset_client()

    monkeypatch.setattr(entry, "_areset_client", spy_areset_client)

    # queue.SimpleQueue is a C type that refuses direct attribute
    # reassignment ("immutable type"), and the real ``queue`` module is
    # shared process-wide (asyncio/concurrent.futures use it internally too)
    # -- subclass SimpleQueue and swap ONLY the ``queue`` NAME BOUND inside
    # claude_code_sessions's own module namespace (a proxy object), leaving
    # the real stdlib module (and everyone else's binding to it) untouched.
    class _SpySimpleQueue(queue_module.SimpleQueue):
        def put(self, item: Any, *a: Any, **kw: Any) -> Any:  # noqa: D102
            if item[0] is ccs._STREAM_END:
                calls.append("end")
            return super().put(item, *a, **kw)

    class _QueueModuleProxy:
        SimpleQueue = _SpySimpleQueue

    monkeypatch.setattr(ccs, "queue", _QueueModuleProxy)

    with pytest.raises(RuntimeError, match="boom"):
        out: list[Any] = []
        async for msg in entry.stream(
            payload="p", session_id="s", timeout=None, on_construct=lambda: None
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


# --------------------------------------------------------------------------- #
# #1305 review round F3: the shared terminal-effects helper
# (turn_spawn.finalize_child_task_terminal) is what dispatches release --
# task_fold.finish_agent_task_transition is only ONE of FOUR paths that call
# it (the completion fold); the other three (the cancel cascade and both
# child_forward.py HITL-edge terminals) previously bypassed it entirely.
# --------------------------------------------------------------------------- #
def test_finish_agent_task_transition_releases_the_childs_session_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subagent reaching STATUS_COMPLETED must have its provider connection
    released deterministically -- RED before the fix (no such call existed;
    a child's connection lingered until the idle-TTL sweep alone).
    """
    calls: list[str] = []
    monkeypatch.setattr(
        session_lifecycle, "release_session_resources", lambda sid: calls.append(sid)
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        completed = replace(
            running,
            status=STATUS_COMPLETED,
            result={"message_ref": "m1", "answer_excerpt": "ok", "workflow_state": {}},
            notify_pending=True,
            updated_at="2026-09-03T00:00:00+00:00",
        )

        outcome = task_fold.fold_agent_task_event(app, _task_event(completed))

        assert outcome.applied is True
        # SABOTAGE: drop the release_session_resources call from
        # finish_agent_task_transition -> calls stays empty -> red.
        assert calls == [running.child_session_id]


def test_finish_agent_task_transition_releases_on_failure_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FAILED terminal (not just COMPLETED) also releases -- the hook is
    keyed on ``is_terminal``, never on success specifically."""
    calls: list[str] = []
    monkeypatch.setattr(
        session_lifecycle, "release_session_resources", lambda sid: calls.append(sid)
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        failed = replace(
            running,
            status="failed",
            error_reason="agent_error",
            notify_pending=True,
            updated_at="2026-09-03T00:00:01+00:00",
        )

        task_fold.fold_agent_task_event(app, _task_event(failed))

        assert calls == [running.child_session_id]


def test_finish_agent_task_transition_never_double_releases_a_race_loser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate terminal fold (already-terminal race loser) must not
    re-trigger the release -- mirrors the existing exactly-once guard."""
    calls: list[str] = []
    monkeypatch.setattr(
        session_lifecycle, "release_session_resources", lambda sid: calls.append(sid)
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        completed = replace(
            running,
            status=STATUS_COMPLETED,
            result={"message_ref": "m1", "answer_excerpt": "ok", "workflow_state": {}},
            notify_pending=True,
            updated_at="2026-09-03T00:00:00+00:00",
        )

        first = task_fold.fold_agent_task_event(app, _task_event(completed))
        second = task_fold.fold_agent_task_event(app, _task_event(completed))

        assert first.applied is True
        assert second.applied is False and second.reason == "already_terminal"
        # SABOTAGE: call release_session_resources even for a no-op race loser
        # -> calls has two entries -> red.
        assert calls == [running.child_session_id]


def test_cancelled_child_task_releases_its_session_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the cancel cascade (``turn_spawn._cancel_one_child_task``)
    previously bypassed the completion fold entirely -- a cancelled child's
    provider connection was left ENTIRELY to the idle-TTL sweep, with no
    SubagentStop hook either. RED before the fix: neither fired.

    SABOTAGE: drop the ``finalize_child_task_terminal`` call from
    ``_cancel_one_child_task`` -> ``calls`` stays empty -> red.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        session_lifecycle, "release_session_resources", lambda sid: calls.append(sid)
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )
        reg = app.state.agent_task_registry

        updated = turn_spawn._cancel_one_child_task(app, reg, running)

        assert updated is not None
        assert updated.status == STATUS_CANCELLED
        assert calls == [running.child_session_id]


def test_fail_child_task_releases_its_session_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: ``child_forward.fail_child_task`` (the HITL-edge "cannot proceed"
    terminal) previously fired SubagentStop but never released.

    SABOTAGE: drop the ``finalize_child_task_terminal`` call -> ``calls``
    stays empty -> red.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        session_lifecycle, "release_session_resources", lambda sid: calls.append(sid)
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )

        child_forward.fail_child_task(
            app, running, running.child_session_id, "child_question_forward_failed", "async"
        )

        assert calls == [running.child_session_id]


def test_complete_forwarded_task_releases_its_session_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: ``child_forward._complete_forwarded_task`` (a forwarded HITL
    question answered + honored with no follow-on turn) previously fired
    SubagentStop but never released.

    SABOTAGE: drop the ``finalize_child_task_terminal`` call -> ``calls``
    stays empty -> red.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        session_lifecycle, "release_session_resources", lambda sid: calls.append(sid)
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        running = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_RUNNING,
        )

        child_forward._complete_forwarded_task(app, running)

        assert calls == [running.child_session_id]
