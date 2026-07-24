"""#1036 (epic #1031 Pillar 2): loop-inbox Producer B — the mid-turn user *steer*.

Covers the four seams the slice adds on top of #1035:

* A second POST while a turn runs is no longer a 409 — it returns 202 and does NOT
  start a second turn. The route no longer persists the message; it is persisted
  ONCE at CONSUMPTION — the mid-turn drain (``mid_turn_steer``) or, turn-ended-first,
  the idle new-turn — so a steer is never double-persisted (#1052).
* :func:`drain_active_session_inbox` surfaces a ``### steer`` grounding block from
  a ``user_message`` event, skipping the task once-gate + delegation terminal
  entirely (a steer is not a task).
* A steer that is never drained mid-turn (the turn ended first) is re-driven by
  the idle hook into EXACTLY ONE new turn; two residual steers still make ONE turn.
* The ``deferred_resumes`` stash is gone — an ask-user answer that arrives while
  busy still delivers (covered end-to-end in ``test_turn_runner_s1``).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.enrichment import PENDING_TASK_NOTIFICATION_MARKER
from clio_agent.gact.loop_inbox import (
    USER_STEER_MARKER,
    InboxEvent,
    drain_active_session_inbox,
    drain_inbox_to_new_turn,
    enqueue_user_steer,
    inbox_for,
)
from clio_agent.gact.runtime.globals import _gact_app_context

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _SlowAgent:
    """Keeps a turn in flight (no tool calls, so a steer is never drained mid-turn —
    it stays buffered for the idle-hook re-drive)."""

    def __init__(self, sleep_s: float = 1.0) -> None:
        self.sleep_s = sleep_s

    def forward(self, question: str, session_id: str):
        time.sleep(self.sleep_s)
        return type(
            "Pred", (), {"answer": "done", "selected_expert": "", "routing_rationale": ""}
        )()


def _new_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "t"}).json()["id"]


def _post(client: TestClient, sid: str, text: str):
    return client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": text}]},
    )


def _wait_busy(app: Any, sid: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not app.state.turn_runner.busy(sid):
        time.sleep(0.02)
    assert app.state.turn_runner.busy(sid), "turn never went in flight"


def _wait_idle(app: Any, sid: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and app.state.turn_runner.busy(sid):
        time.sleep(0.03)


def _user_msgs(client: TestClient, sid: str) -> list[dict[str, Any]]:
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    return [m for m in msgs if m["role"] == "user"]


def _turn_msgs(client: TestClient, sid: str) -> list[dict[str, Any]]:
    """User messages that OWN a turn (turn_id == id); a steer has an empty turn_id."""

    return [m for m in _user_msgs(client, sid) if m.get("turn_id")]


def _text_of(m: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in m.get("parts", []))


# --------------------------------------------------------------------------- #
# 1. Producer B — POST while busy → 202 steer, persisted, no 2nd turn          #
# --------------------------------------------------------------------------- #


def test_mid_turn_post_returns_202_persists_steer_no_second_turn(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post(client, sid, "first").status_code == 200
        _wait_busy(app, sid)
        running = app.state.turn_runner._in_flight.get(sid)

        resp = _post(client, sid, "steer me")
        assert resp.status_code == 202
        steer_id = resp.json()["message_id"]

        # #1052 persist-at-CONSUMPTION: the route no longer persists, so the steer is
        # NOT YET in GET /messages between the 202 and the next drain.
        assert steer_id not in {m["id"] for m in _user_msgs(client, sid)}
        # It is buffered for the running turn's next boundary / idle re-drive.
        assert inbox_for(app, sid).peek_nonempty()

        # Simulate the mid-turn tool boundary: the running turn drains its inbox and
        # persists the steer AT consumption (turn keeps its slot — no new turn).
        with _active_turn(app, sid):
            drain_active_session_inbox(app)

        by_id = {m["id"]: m for m in _user_msgs(client, sid)}
        assert steer_id in by_id, "the steer was not persisted at the drain"
        assert by_id[steer_id]["metadata"].get("mid_turn_steer") is True
        assert by_id[steer_id].get("turn_id", "") == "", "a steer must not mint a turn"
        assert _text_of(by_id[steer_id]) == "steer me"
        # Exactly ONE record of the steer text (no route+drain double-persist).
        assert sum(1 for m in _user_msgs(client, sid) if _text_of(m) == "steer me") == 1

        # No second turn: the running turn still owns the slot.
        assert app.state.turn_runner._in_flight.get(sid) is running


def test_route_no_longer_double_persists_steer(tmp_path: Path) -> None:
    """THE #1036 bug regression. Pre-fix, a steer whose turn ends before any drain
    was persisted TWICE — the route's ``mid_turn_steer`` record AND the idle
    new-turn — so ``_user_msgs`` held 3 (first-turn, route-steer, idle-turn).
    Persist-at-consumption makes it EXACTLY the idle-promoted new turn: 2 total."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.6))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post(client, sid, "first").status_code == 200
        _wait_busy(app, sid)
        assert _post(client, sid, "steer me").status_code == 202

        _wait_idle(app, sid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_turn_msgs(client, sid)) < 2:
            time.sleep(0.05)

        users = _user_msgs(client, sid)
        assert len(users) == 2, f"route double-persisted the steer: {len(users)} user messages"
        assert len(_turn_msgs(client, sid)) == 2, "the idle re-drive is the ONLY steer persist"
        assert sum(1 for m in users if _text_of(m) == "steer me") == 1
        assert not inbox_for(app, sid).peek_nonempty()


def test_steer_persisted_exactly_once_on_midturn_drain(tmp_path: Path) -> None:
    """A POST-style steer (carrying a pre-minted id/stamp) drained mid-turn persists
    EXACTLY ONE ``mid_turn_steer`` message; the idle re-drive then persists NOTHING
    further (the atomic drain already emptied the inbox) — exactly-once across the
    two consumers."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.1))
    with TestClient(app) as client:
        sid = _new_session(client)
        enqueue_user_steer(
            app,
            sid,
            "drain me",
            {"foo": "bar"},
            steer_message_id="msg_user_steer1",
            steer_created_at="2026-07-24T00:00:00+00:00",
        )
        with _active_turn(app, sid):
            block = drain_active_session_inbox(app)

        assert USER_STEER_MARKER in block
        by_id = {m["id"]: m for m in _user_msgs(client, sid)}
        assert "msg_user_steer1" in by_id, "the pre-minted steer id was not persisted"
        assert by_id["msg_user_steer1"]["metadata"].get("mid_turn_steer") is True
        assert by_id["msg_user_steer1"].get("turn_id", "") == ""
        assert _text_of(by_id["msg_user_steer1"]) == "drain me"
        assert sum(1 for m in _user_msgs(client, sid) if _text_of(m) == "drain me") == 1

        # Second consumer (idle re-drive): inbox already empty → no further persist,
        # no new turn.
        before = len(_user_msgs(client, sid))
        drain_inbox_to_new_turn(app, sid)
        assert len(_user_msgs(client, sid)) == before
        assert _turn_msgs(client, sid) == []
        assert not inbox_for(app, sid).peek_nonempty()


# --------------------------------------------------------------------------- #
# 2. Drain surfaces a ### steer block (skips the task once-gate + terminal)    #
# --------------------------------------------------------------------------- #


@contextmanager
def _active_turn(app: Any, sid: str) -> Iterator[None]:
    with _gact_app_context(app):
        token = ctx.set_session_id(sid)
        try:
            yield
        finally:
            ctx.reset(token)


def test_drain_surfaces_steer_block_no_consume_no_terminal(tmp_path: Path) -> None:
    """A user_message event surfaces its OWN ``### steer`` block. No task machinery
    runs: if the once-gate/terminal were invoked they would need a task_id (empty
    here) and blow up — the branch must short-circuit before them."""

    called: dict[str, int] = {"consume": 0}

    def _boom_consume(_app: Any, _task_id: str) -> Any:
        called["consume"] += 1
        raise AssertionError("consume_notification must NOT run for a steer")

    import clio_agent.gact.agent_tasks as agent_tasks

    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        sid = app.state.sessions.create(workspace_id="ws_default", title="p").id
        inbox_for(app, sid).put(
            InboxEvent(kind="user_message", task_id="", text="please pivot to LA")
        )
        # Sabotage the task path: reaching it for a steer is the bug.
        monkey = pytest.MonkeyPatch()
        monkey.setattr(agent_tasks, "consume_notification", _boom_consume)
        try:
            with _active_turn(app, sid):
                block = drain_active_session_inbox(app)
        finally:
            monkey.undo()

    assert USER_STEER_MARKER in block
    assert "please pivot to LA" in block
    assert PENDING_TASK_NOTIFICATION_MARKER not in block, "a steer must not ride the task marker"
    assert called["consume"] == 0, "the steer branch invoked the task once-gate"
    # #1052 discriminator: this event carries NO steer_message_id (ask-user-resume
    # style) — the block surfaces but the drain persists NOTHING mid-turn.
    assert not any(
        m.role == "user" for m in app.state.messages.get(sid, [])
    ), "an empty-id steer must not be persisted at the drain"


def test_drain_empty_steer_text_surfaces_nothing(tmp_path: Path) -> None:
    """An empty/whitespace steer text yields no bare marker (nothing to surface)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        sid = app.state.sessions.create(workspace_id="ws_default", title="p").id
        inbox_for(app, sid).put(InboxEvent(kind="user_message", task_id="", text="   "))
        with _active_turn(app, sid):
            assert drain_active_session_inbox(app) == ""


# --------------------------------------------------------------------------- #
# 3. Idle-hook re-drive — residual steers become EXACTLY ONE new turn          #
# --------------------------------------------------------------------------- #


def test_residual_steer_redrives_one_new_turn_at_idle(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.6))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post(client, sid, "first").status_code == 200
        _wait_busy(app, sid)
        steer_resp = _post(client, sid, "steer once")
        assert steer_resp.status_code == 202
        steer_id = steer_resp.json()["message_id"]

        _wait_idle(app, sid)
        # The idle hook re-drove the residual steer as its own new turn.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_turn_msgs(client, sid)) < 2:
            time.sleep(0.05)
        # GET /messages is newest-first, so the re-driven turn leads.
        turns = _turn_msgs(client, sid)
        assert len(turns) == 2, "residual steer did not re-drive as exactly one new turn"
        assert _text_of(turns[0]) == "steer once"
        # #1052: the turn-ended-first idle re-drive REUSES the id the 202 already
        # returned, so the client-held handle resolves to exactly one message in this
        # path too (no phantom 202 id) — the blocker the review caught.
        assert turns[0]["id"] == steer_id, "idle re-drive did not reuse the pre-minted 202 id"
        # #1052: exactly 2 user messages (first turn + promoted steer turn) — the
        # route left NO duplicate for the idle re-drive to double.
        assert len(_user_msgs(client, sid)) == 2
        # Inbox fully drained (exactly-once).
        assert not inbox_for(app, sid).peek_nonempty()


def test_two_residual_steers_make_one_turn(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.8))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post(client, sid, "first").status_code == 200
        _wait_busy(app, sid)
        s1_id = _post(client, sid, "s1").json()["message_id"]
        s2_id = _post(client, sid, "s2").json()["message_id"]

        _wait_idle(app, sid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_turn_msgs(client, sid)) < 2:
            time.sleep(0.05)
        # Two steers, but only ONE additional turn (their texts concatenated).
        # GET /messages is newest-first, so the re-driven turn leads.
        turns = _turn_msgs(client, sid)
        assert len(turns) == 2, "two residual steers must re-drive as ONE turn, not two"
        assert _text_of(turns[0]) == "s1\n\ns2"
        # #1052 documented multi-steer edge: when N steers coalesce into ONE turn, that
        # single message can carry only one id, so it mints a FRESH one rather than
        # silently claiming either steer's 202 id (the coalesced ids are un-resolvable).
        assert turns[0]["id"] not in {s1_id, s2_id}
        # #1052: exactly 2 user messages (first turn + the ONE promoted steer turn) —
        # the route persisted no per-steer duplicate.
        assert len(_user_msgs(client, sid)) == 2
        assert not inbox_for(app, sid).peek_nonempty()


def test_drain_to_new_turn_preserves_child_wakes(tmp_path: Path) -> None:
    """A non-steer child-completion wake buffered at the turn boundary is NOT
    re-driven as a turn and NOT dropped: it is put back for the next drain."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.1))
    with TestClient(app) as client:
        sid = _new_session(client)
        # Idle session, agent present. Buffer a child-completion wake only.
        inbox_for(app, sid).put(InboxEvent(kind="child_completed", task_id="t_child"))
        drain_inbox_to_new_turn(app, sid)
        # No new turn staged (no steers), and the child wake survives for later.
        assert _turn_msgs(client, sid) == []
        remaining = inbox_for(app, sid).drain()
        assert [(e.kind, e.task_id) for e in remaining] == [("child_completed", "t_child")]


# --------------------------------------------------------------------------- #
# 4. enqueue_user_steer helper                                                 #
# --------------------------------------------------------------------------- #


def test_enqueue_user_steer_carries_text_and_metadata(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        sid = app.state.sessions.create(workspace_id="ws_default", title="p").id
        enqueue_user_steer(app, sid, "hello", {"ask_user_resume": True, "question_id": "q9"})
        events = inbox_for(app, sid).drain()
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "user_message"
        assert ev.task_id == ""
        assert ev.text == "hello"
        assert ev.metadata == {"ask_user_resume": True, "question_id": "q9"}
