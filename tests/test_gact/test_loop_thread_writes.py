"""#1334 regression lock: no ARC store write is waited on from the server loop thread.

The guard (``arc.loop_guard``) sits at the persist seam every backend shares, so a real
turn through ``TestClient`` (the in-memory ARC backend) records every loop-thread write
it would have blocked on. These tests arrange ``reset_guard_hits()``, drive the real
paths, and assert ZERO write hits: any new writer on the loop turns them red with the
op + scope + thread it was caught on.

Sites covered (the #1334 inventory): the user message's transcript persist inside
``POST /messages`` (A), the turn prologue events (B), the post-judge goal persistence
(C), the session lifecycle observer (D), the compact / command / message-delete / a2ui
routes (E), and the finalize + failed-finalize envelopes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.arc.loop_guard as loop_guard
import clio_agent.gact.goal as goal_module
from clio_agent.arc.loop_guard import (
    LoopThreadStoreWrite,
    guard_hits,
    on_server_loop,
    reset_guard_hits,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.goal import dispatch_goal_at_finalize
from clio_agent.gact.part_atoms import load_message_part_atoms

from .conftest import complete_turn, settle_turn_slot
from .test_post_messages import FakeClioAgent, _create_session

# The builtin main needs the host tool executor to run (the same mark the message tests carry).
pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _write_hits() -> list[tuple[str, str, str, str]]:
    return [hit for hit in guard_hits() if hit[0] == "write"]


def test_the_apps_lifespan_registers_the_loop_the_guard_refuses(tmp_path: Path) -> None:
    """The lock's own premise (#1334 follow-up): the guard knows THIS app's loop.

    The guard now refuses writes by loop IDENTITY, so if ``build_app``'s lifespan stopped
    registering its loop, every test in this file would go green by accident -- a write on
    the loop would score as a harmless "private loop" write. Assert the registration
    directly, both while the app runs and after it tears down.
    """

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent())
    with TestClient(app):
        loop = app.state.mcp_app_loop

        async def _probe() -> bool:
            return on_server_loop()

        assert asyncio.run_coroutine_threadsafe(_probe(), loop).result(timeout=10) is True

        async def _write_on_the_loop() -> None:
            app.state.arc.append_segment("sess_probe", "probe", "observation", {"text": "x"})

        with pytest.raises(LoopThreadStoreWrite):
            asyncio.run_coroutine_threadsafe(_write_on_the_loop(), loop).result(timeout=10)
    assert loop not in loop_guard._SERVER_LOOPS
    assert loop not in loop_guard._DRAINING_LOOPS


def test_a_real_turn_never_writes_the_store_from_the_loop(tmp_path: Path) -> None:
    """The lock: one full turn (accept -> prologue -> forward -> finalize) = zero hits."""

    reset_guard_hits()
    agent = FakeClioAgent(answer="the answer")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = _create_session(client)
        assistant = complete_turn(client, sid, "hello")
        assert assistant["stop_reason"] != "error", assistant
        # The deferred user-message persist landed (off the loop) before the turn settled.
        atoms = load_message_part_atoms(app.state.arc, sid)
        roles = {atoms_of[0]["role"] for atoms_of in atoms.values() if atoms_of}
        assert roles == {"user", "assistant"}, atoms.keys()
        client.get(f"/v1/sessions/{sid}/messages")
        client.delete(f"/v1/sessions/{sid}")
    assert _write_hits() == []


def test_post_messages_ack_does_not_carry_the_transcript_persist(tmp_path: Path) -> None:
    """Site A: the POST returns with the ledger written and the atoms deferred."""

    reset_guard_hits()
    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent())
    with TestClient(app) as client:
        sid = _create_session(client)
        ack = client.post(
            f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "x"}]}
        )
        assert ack.status_code == 200, ack.text
        assert _write_hits() == []  # the ack thread never touched the store
        settle_turn_slot(client, sid)
        atoms = load_message_part_atoms(app.state.arc, sid)
        assert any(a[0]["role"] == "user" for a in atoms.values() if a), "user atoms never landed"
    assert _write_hits() == []


def test_post_judge_goal_persistence_runs_off_the_loop(monkeypatch: Any) -> None:
    """Site C: the goal.<outcome> event after the awaited judge never runs on the loop."""

    threads: list[str] = []

    def _spy_emit(app: Any, sid: str, event_type: str, **_kw: Any) -> dict[str, Any]:
        threads.append(f"{event_type}@{threading.current_thread().name}")
        return {}

    class _Sessions:
        def __init__(self) -> None:
            self.meta = {
                "goal": {
                    "goal_id": "g1",
                    "active": True,
                    "cleared": False,
                    "condition": "done",
                    "iters_elapsed": 0,
                    "max_goal_iters": 3,
                    "created_at": "2026-09-10T00:00:00+00:00",
                }
            }

        def get(self, sid: str) -> Any:
            return type("S", (), {"metadata": self.meta, "tokens_input": 0, "tokens_output": 0})()

        def update(self, sid: str, **kw: Any) -> None:
            patch = kw.get("metadata_patch") or {}
            self.meta.update(patch)

    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.sessions = _Sessions()
    app.state.loop_inboxes = {}

    async def _judge(*_a: Any, **_k: Any) -> Any:
        return type("V", (), {"met": True, "reason": "ok"})()

    monkeypatch.setattr(goal_module, "run_llm_judge", _judge)
    monkeypatch.setattr(goal_module, "_enqueue_goal_redrive", lambda *a, **k: None)
    monkeypatch.setattr(
        "clio_agent.gact.runtime.globals._emit_semantic_event", _spy_emit, raising=True
    )

    async def _run() -> Any:
        loop_thread = threading.current_thread().name
        decision = await dispatch_goal_at_finalize(app, session_id="s1", turn_id="t", trace_id="tr")
        return loop_thread, decision

    loop_thread, decision = asyncio.run(_run())
    assert decision is not None and decision.outcome == "met"
    assert threads and all(not t.endswith(f"@{loop_thread}") for t in threads), threads


@pytest.mark.parametrize("route", ["compact", "command", "delete_message"])
def test_routes_that_touch_the_ledger_never_write_from_the_loop(tmp_path: Path, route: str) -> None:
    """Sites D/E: session create/delete (lifecycle), compact, a command, a message delete."""

    reset_guard_hits()
    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent())
    with TestClient(app) as client:
        sid = _create_session(client)
        user = complete_turn(client, sid, "first")
        if route == "compact":
            client.post(f"/v1/sessions/{sid}/compact", json={})
        elif route == "command":
            client.post(f"/v1/sessions/{sid}/commands/help", json={"input": ""})
        else:
            client.delete(f"/v1/sessions/{sid}/messages/{user['id']}")
        client.delete(f"/v1/sessions/{sid}")
    assert _write_hits() == []


def test_compact_stores_the_arc_conversation_record_off_the_loop(tmp_path: Path) -> None:
    """Review find: ``POST /compact`` still wrote ARC's conversation record on the loop.

    The parametrized sweep above drives this route but cannot see the write: its
    ``FakeClioAgent`` carries no ``.arc``, so ``routes/sessions.py``'s whole ARC block
    is skipped -- and the ``conversations`` kind is not on the shared ``segments``
    persist seam the guard hooks, so the in-memory backend records no hit either. Bind
    a real ARC and assert the invariant DIRECTLY, with the guard's own predicate: the
    thread that issues the store call must have no running loop on it.

    Against the real clio-core store the missed write was not merely slow: the guard
    raised ``LoopThreadStoreWrite``, the handler's ``except Exception`` turned it into
    an HTTP 500 ``memory_update_failed``, and ``POST /compact`` failed outright.
    """

    reset_guard_hits()
    agent = FakeClioAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    arc = app.state.arc
    agent.arc = arc  # the route reads ``agent.arc``; the fake has none by default
    agent._run_chat_agent = lambda prompt, _ctx: "compact summary"  # the summarise step

    on_loop: list[bool] = []
    real_store = arc.store_conversation

    def _record(conversation: Any) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_store(conversation)

    arc.store_conversation = _record  # type: ignore[method-assign]
    with TestClient(app) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "first")
        response = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert response.status_code == 200, response.text
        events = app.state.memory_events[sid]
        assert events[-1]["arc_status"] == "stored", events[-1]
    assert on_loop == [False], f"the conversation record was stored on the loop: {on_loop}"
    assert _write_hits() == []


def test_a_turn_cancelled_before_its_prologue_still_persists_the_user_message(
    tmp_path: Path,
) -> None:
    """Review find: the deferred user-message persist must not die with the turn task.

    #1334 handed the user message's ARC persist to the turn (so ``POST /messages`` never
    waits on a store RPC), and the turn runs it first, in its off-loop prologue. A task
    cancelled before its FIRST step never runs its body at all -- not even a ``finally``
    -- so the job was silently dropped. The message then sat in the in-memory ledger and
    the local store with no atoms, and ``materialize_ledger`` takes its ``has_atoms``
    branch on any session that already completed a turn, so the message VANISHED from the
    transcript on the next rehydrate. ``spawn_user_turn``'s done-callback flushes it.
    """

    reset_guard_hits()
    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    with TestClient(app) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "first")  # the lane now HAS atoms: no backfill will run
        settle_turn_slot(client, sid)

        real_spawn = app.state.turn_runner.spawn

        def _spawn_then_cancel(coro: Any, *, sid: str, turn_id: str) -> Any:
            task = real_spawn(coro, sid=sid, turn_id=turn_id)
            task.cancel()  # before its first step: the coroutine body never runs
            return task

        app.state.turn_runner.spawn = _spawn_then_cancel
        ack = client.post(
            f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "second"}]}
        )
        assert ack.status_code == 200, ack.text
        user_id = ack.json()["message_id"]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if user_id in load_message_part_atoms(app.state.arc, sid):
                break
            client.get(f"/v1/sessions/{sid}")  # keep the loop turning so the flush lands
            time.sleep(0.05)
        atoms = load_message_part_atoms(app.state.arc, sid)
    assert user_id in atoms, f"the cancelled turn dropped its user message's atoms: {atoms.keys()}"
    assert _write_hits() == [], "the orphan flush must run off the loop"
