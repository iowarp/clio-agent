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
from clio_agent.gact.goal import GoalJudgement, dispatch_goal_at_finalize
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
        assert ack.status_code == 202, ack.text
        assert ack.json()["task_handle"]
        assert _write_hits() == []  # the ack thread never touched the store
        settle_turn_slot(client, sid)
        atoms = load_message_part_atoms(app.state.arc, sid)
        assert any(a[0]["role"] == "user" for a in atoms.values() if a), "user atoms never landed"
    assert _write_hits() == []


def test_post_judge_goal_persistence_runs_off_the_loop(tmp_path: Path, monkeypatch: Any) -> None:
    """The real Core dependency check and goal settlement both leave the loop responsive."""
    threads: list[str] = []

    def spy_emit(app: Any, sid: str, event_type: str, **kwargs: Any) -> dict[str, Any]:
        threads.append(f"{event_type}@{threading.current_thread().name}")
        return {}

    async def judge(*args: Any, **kwargs: Any) -> GoalJudgement:
        return GoalJudgement(met=True, reason="ok")

    app = build_app(sessions_path=tmp_path / "goal.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Goal settlement"}).json()["id"]
        goal_module.arm_goal(app, sid, condition="done", max_goal_iters=3)
        monkeypatch.setattr(goal_module, "run_llm_judge", judge)
        monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", spy_emit)
        loop_thread = threading.current_thread().name
        decision = asyncio.run(
            dispatch_goal_at_finalize(app, session_id=sid, turn_id="t", trace_id="tr")
        )
        assert decision is not None and decision.outcome == "met"
        assert threads and all(not thread.endswith(f"@{loop_thread}") for thread in threads), (
            threads
        )
        assert app.state.sessions.get(sid).metadata["goal"]["met"] is True


def test_compact_folds_the_arc_working_set_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compaction's ARC fold (``compaction._fold``) is the ``arc.summarize_segments``
    store RPC: it must run off the loop. The scope compaction resolves is fixed
    (``clio_react_record.arc_scope`` monkeypatched to a real ARC, mirroring turn-scoped
    resolution) with >=2 live segments so the fold runs end-to-end.
    """

    reset_guard_hits()
    agent = FakeClioAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent._run_chat_agent = lambda prompt, _ctx: "compact summary"  # the summarise step
    arc = app.state.arc
    scope = "scope_probe"

    on_loop: list[bool] = []
    real_summarize = arc.summarize_segments

    def _record(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_summarize(*args, **kwargs)

    arc.summarize_segments = _record  # type: ignore[method-assign]

    with TestClient(app) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "first")
        # one coherent step, as the recorder writes it: calls answered by call id
        arc.append_segment(sid, scope, "thought", {"text": "working"})
        for i, text in enumerate(("first live segment", "second live segment")):
            call = {"id": f"call_{i}", "name": "t", "args": {}}
            arc.append_segment(sid, scope, "tool_call", call)
            obs = {"call_id": f"call_{i}", "text": text, "is_error": False}
            arc.append_segment(sid, scope, "observation", obs)

        import clio_agent.gact.agents.clio_react_record as clio_react_record

        monkeypatch.setattr(clio_react_record, "arc_scope", lambda: (arc, sid, scope))

        response = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert response.status_code == 200, response.text
        [done] = response.json()["compactions"]
        assert (done["scope"], done["replaced_count"]) == (scope, 5)
    assert on_loop == [False], f"the ARC fold ran on the loop: {on_loop}"
    assert _write_hits() == []


def test_pre_model_stop_preserves_confirmed_user_input_off_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepted input survives before any worker starts, without a done-callback flush."""
    from clio_agent.gact.task_history import TaskHistory
    from tests.test_gact.input_capture import capture_inputs

    reset_guard_hits()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="ok"))
    with TestClient(app) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "first")
        settle_turn_slot(client, sid)
        captured = capture_inputs(app, monkeypatch)
        accepted = client.post(f"/v1/sessions/{sid}/messages", json={"text": "second"})
        assert accepted.status_code == 202, accepted.text
        identity = accepted.json()["message_id"]
        settle_turn_slot(client, sid)
        assert not app.state.turn_runner.busy(sid)
        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        original = app.state.task_history.mail_item(sid, identity)
        assert original is not None and original in captured
        assert original.id not in app.state.task_history.acknowledged_notifications(sid)
        restored = TaskHistory(app.state.arc)
        restored.restore()
        assert restored.mail_item(sid, identity) == original
        assert restored.read_content(original.content)["text"] == "second"
        assert restored.committed_notification_input(sid, identity) is None
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert any(row["id"] == identity for row in messages)
    assert _write_hits() == []
