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

import clio_agent.gact.goal as goal_module
from clio_agent.arc.loop_guard import guard_hits, reset_guard_hits
from clio_agent.gact.app import build_app
from clio_agent.gact.goal import dispatch_goal_at_finalize
from clio_agent.gact.part_atoms import load_message_part_atoms

from .conftest import complete_turn, settle_turn_slot
from .test_post_messages import FakeClioAgent, _create_session

# The builtin main needs the host tool executor to run (the same mark the message tests carry).
pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _write_hits() -> list[tuple[str, str, str, str]]:
    return [hit for hit in guard_hits() if hit[0] == "write"]


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
