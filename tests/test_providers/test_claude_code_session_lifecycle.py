"""#1305 owner ruling: connection lifetime = subagent lifetime.

A subagent's provider connection must die DETERMINISTICALLY the moment its
work is done -- never left to the idle-TTL sweep alone. This module pins the
generic seam (:mod:`clio_agent.providers.session_lifecycle`), its claude_code
consumer (:meth:`ClaudeStreamClientPool.release_session_resources`), and the
GACT-level dispatch point (``gact/task_fold.py::finish_agent_task_transition``
-- the single choke point every child agent-task completion path funnels
through). Each pin carries an inline SABOTAGE note.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.gact.agent_tasks as agent_tasks
import clio_agent.gact.task_fold as task_fold
from clio_agent.gact.agent_tasks import STATUS_COMPLETED, STATUS_RUNNING, AgentTask, seed_agent_task
from clio_agent.gact.agents.invoker import TaskEvent
from clio_agent.gact.app import build_app
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


# --------------------------------------------------------------------------- #
# gact/task_fold.py::finish_agent_task_transition -- the GACT-level dispatch
# point. This is the ONE choke point every child agent-task completion path
# (local done-callback AND transport fold) funnels through, already
# race-guarded exactly-once by outcome.applied + outcome.task.is_terminal.
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
