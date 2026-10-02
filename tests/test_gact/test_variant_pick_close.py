"""A drafts pick that never comes: superseded, cancelled, expired -- around the REAL loop.

The user is shown the drafts of a human-judged run (``draft_alternatives`` with
``judge: user``) and does not pick: they send a new message, dismiss the question, or
the pick's window ends. Each is a typed terminal state of the run in clio-core
(``superseded`` / ``cancelled`` / ``expired``), a ``variant.closed`` event, a closed
question no later answer can resume, a cleared session pointer, and a notice the next
turn's agent is told. Real ClioReAct on real clio-core, one scripted engine recording
every request; the new turn runs through the turn's own forward seam
(``turn_forward._run_module``), the cancel through the real route.

SABOTAGE (each run manually, each went RED, then was restored):

* ``_run_module`` without ``turn_start_notices``: the supersede test's question stays
  pending, the run ``awaiting_pick``, and its later answer resumes the run.
* ``claim_question_transition`` ignoring ``metadata_patch``: the closed question carries
  no ``variant_resolution`` and closes as ``cancelled``, not ``superseded``.
* ``resolve_cancelled_question`` not closing the run: the cancel test's run stays
  ``awaiting_pick`` and the pointer stays set.
* the deadline timer not closing the run: the expiry test's run stays ``awaiting_pick``.
* ``_supersede`` without its cancelled/expired branch: the restart test's half-finished
  close is never completed (the run stays ``awaiting_pick``).
* ``preference_records`` keeping only selected runs: no record for a closed run.
* ``_release`` not clearing the pointer: ``variant_pending`` still names the run.
* ``prepare_question_answer`` accepting a message naming the drafts question: no 422.
* ``variant.closed`` dropped from the v3 projectors: no ``variant.closed`` frame.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.variant_drafts import build_draft_alternatives_tool
from clio_agent.gact.agents.variant_records import load_run, preference_records
from clio_agent.gact.elicitation_bridge import claim_question_transition
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3.event import event_to_v3
from clio_agent.gact.turn_forward import _run_module
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import Message
from tests._scripted_engine import Reply, calls, routed_lm
from tests.test_gact.test_variant_drafts import (
    ASK,
    RUN0,
    RUN1,
    SCOPE,
    World,
    _app,
    _bind,
    _forward,
    _line,
    _surface,
    _texts,
)

NEXT = "Actually, just summarize the shipment status."
SUMMARY = "The shipment is three days late."


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """One app on real clio-core with a session (as in ``test_variant_drafts``)."""
    app, arc, events = _app(tmp_path / "sessions.json")
    sid = app.state.sessions.create(workspace_id="ws_default", title="picks").id
    yield World(app, arc, sid, events, tmp_path / "sessions.json")


def _turn(app: Any, sid: str, lm: Any, text: str, turn: str) -> dspy.Prediction:
    """A new turn through the turn's forward seam (``_run_module``), real loop inside."""
    session = app.state.sessions.get(sid)
    user = Message(
        id=turn,
        session_id=sid,
        role="user",
        parts=[],
        created_at=session.created_at,
        updated_at=session.updated_at,
    )
    state = TurnState(
        app=app,
        sid=sid,
        sess=session,
        bus=app.state.bus,
        user_msg=user,
        user_text=text,
        turn_agent_id=SCOPE,
        turn_id=turn,
        trace_id=f"trace_{turn}",
        retry_attempt_id="",
        native_images=[],
    )
    state.enriched_text = text
    agent = ClioReAct("question -> answer", tools=[build_draft_alternatives_tool()])
    tokens = _bind(app, sid, turn)
    try:
        with dspy.context(lm=lm):
            return asyncio.run(_run_module(state, agent, lambda: False))
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def _paused(world: World, monkeypatch: pytest.MonkeyPatch, *, expires: int = 0) -> tuple:
    """Turn 1 drafts two alternatives for the user; its question is surfaced."""
    args = {"n": 2, "rubric": "clear and polite", "strategy": "best_of_n", "judge": "user"}
    if expires:
        args["expiresInSeconds"] = expires
    lm, engine = routed_lm(
        {
            SCOPE: [
                calls(("draft_alternatives", args), text="I'll draft a few versions."),
                Reply(text=SUMMARY),
            ],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
        }
    )
    paused = _forward(world.app, world.sid, lm, ASK, "turn_1")
    assert paused.termination_reason == "draft_alternatives_yield"
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)
    return lm, engine, question


def _closed_events(world: World) -> list[dict[str, Any]]:
    return [e.payload for e in world.events if e.event_type == "variant.closed"]


def _runs(app: Any, sid: str) -> list[dict[str, Any]]:
    response = TestClient(app).get(f"/v1/sessions/{sid}/variant-runs")
    assert response.status_code == 200, response.text
    return response.json()["runs"]


def _answer_route(app: Any, sid: str, question_id: str, pick: str) -> Any:
    return TestClient(app).post(
        f"/v1/sessions/{sid}/questions/{question_id}/answer",
        json={"selected_options": [pick], "answer": ""},
    )


def _interaction(app: Any, sid: str, question_id: str) -> dict[str, Any]:
    response = TestClient(app).get(
        f"/v1/sessions/{sid}/interactions", params={"include_recent_resolved": "true"}
    )
    assert response.status_code == 200, response.text
    [row] = [r for r in response.json()["interactions"] if r["id"] == f"question:{question_id}"]
    return row


def _notice(engine: Any) -> list[str]:
    return [t for t in _texts(engine.by_scope[SCOPE][-1]) if t.startswith("[clio: variant_")]


def _no_drafts_in(rows: list[tuple[str, str]]) -> None:
    assert not any(d in text for _role, text in rows for d in ("Draft A", "Draft B"))


# --------------------------------------------------------------------------- #
# A new message instead of a pick                                             #
# --------------------------------------------------------------------------- #
def test_a_new_message_supersedes_the_pick_and_no_later_answer_resumes_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, engine, question = _paused(world, monkeypatch)
    variants_id = question.metadata["variants_id"]

    done = _turn(world.app, world.sid, lm, NEXT, "turn_2")

    assert done.answer == SUMMARY
    # the run is closed in clio-core, typed, naming the message that moved on
    run = load_run(world.app, world.sid, variants_id)
    assert (run.status, run.closed_reason) == ("superseded", "variant_pick_superseded")
    assert (run.superseded_by_message_id, run.question_id) == ("turn_2", question.id)
    assert run.pick is None and run.selected_index is None and run.closed_at
    # the question is closed through the one atomic transition, saying why
    closed_q = world.app.state.user_questions[question.id]
    assert closed_q.status == "cancelled"
    assert closed_q.metadata["variant_resolution"] == "superseded"
    assert closed_q.metadata["superseded_by_message_id"] == "turn_2"
    meta = world.app.state.sessions.get(world.sid).metadata
    assert meta["variant_pending"] == {}
    assert meta["variant_notice"] == {}  # told to this turn's agent, then cleared
    assert meta["pending_user_question_id"] == ""
    assert meta["pending_ask_user"]["resolved_status"] == "cancelled"
    # the agent of the new turn is told the drafts were dropped, and never sees them
    [notice] = _notice(engine)
    assert notice == (
        f"[clio: variant_closed]\nThe 2 draft(s) shown to the user (run {variants_id}) "
        "were never picked: the user sent a new message instead. None of them is part "
        "of the conversation."
    )
    assert not any(d in t for t in _texts(engine.by_scope[SCOPE][-1]) for d in ("Draft A",))
    # a late answer cannot splice the pick into the past: 409, nothing resumes
    late = _answer_route(world.app, world.sid, question.id, RUN1)
    assert late.status_code == 409
    again = _forward(world.app, world.sid, routed_lm({SCOPE: [Reply(text="ok")]})[0], "hi", "t3")
    assert again.termination_reason != "variant_selected"
    line = _line(world.arc, world.sid)
    _no_drafts_in(line)
    assert line[-4:] == [
        ("user", NEXT),
        ("assistant", SUMMARY),
        ("user", "hi"),
        ("assistant", "ok"),
    ]
    # the UI: one variant.closed, its v3 frame, the record, the endpoint, the interaction
    [closed] = _closed_events(world)
    assert closed["status"] == "superseded"
    assert closed["reason"] == "variant_pick_superseded"
    assert closed["superseded_by_message_id"] == "turn_2"
    assert closed["question_id"] == question.id
    assert [(c["try_index"], c["scope"], c["text"]) for c in closed["candidates"]] == [
        (0, RUN0, "Draft A"),
        (1, RUN1, "Draft B"),
    ]
    [record] = preference_records(world.app, world.sid)
    assert (record.status, record.pick, record.selected_index) == ("superseded", None, None)
    assert record.closed_reason == "variant_pick_superseded"
    assert [c.text for c in record.candidates] == ["Draft A", "Draft B"]
    [served] = _runs(world.app, world.sid)
    assert served["status"] == "superseded"
    assert served["closed_reason"] == "variant_pick_superseded"
    assert served["superseded_by_message_id"] == "turn_2"
    assert served["closed_at"] == run.closed_at
    assert [t["text"] for t in served["tries"]] == ["Draft A", "Draft B"]
    row = _interaction(world.app, world.sid, question.id)
    assert row["status"] == "cancelled"
    assert row["actions"] == []
    assert row["payload"]["metadata"]["variant_resolution"] == "superseded"


def test_variant_closed_projects_to_its_v3_frame(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, _engine, question = _paused(world, monkeypatch)
    _turn(world.app, world.sid, lm, NEXT, "turn_2")

    [event] = [e for e in world.events if e.event_type == "variant.closed"]
    frame = event_to_v3(
        Event(type="semantic.event", session_id=world.sid, payload=event.to_dict("sse"))
    )
    assert frame["type"] == "variant.closed"
    assert frame["entity_id"] == question.metadata["variants_id"]
    payload = frame["payload"]
    assert {k: payload[k] for k in ("status", "reason", "question_id", "judge", "n")} == {
        "status": "superseded",
        "reason": "variant_pick_superseded",
        "question_id": question.id,
        "judge": "user",
        "n": 2,
    }
    assert payload["superseded_by_message_id"] == "turn_2"
    assert [c["text"] for c in payload["candidates"]] == ["Draft A", "Draft B"]


def test_the_pick_answer_turn_is_not_a_new_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn an answer starts resumes the run; it never supersedes it."""
    _lm, engine, question = _paused(world, monkeypatch)
    answered = _answer_route(world.app, world.sid, question.id, RUN1)
    assert answered.status_code == 200, answered.text

    resumed = _turn(world.app, world.sid, routed_lm({})[0], "[Answer] Draft 2", "turn_2")

    assert (resumed.answer, resumed.termination_reason) == ("Draft B", "variant_selected")
    assert load_run(world.app, world.sid, question.metadata["variants_id"]).status == "selected"
    assert _closed_events(world) == []


def test_a_message_naming_the_drafts_question_must_pick_through_the_answer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lm, _engine, question = _paused(world, monkeypatch)
    response = TestClient(world.app).post(
        f"/v1/sessions/{world.sid}/messages",
        json={
            "parts": [{"type": "text", "text": "the second one"}],
            "metadata": {"answers_question_id": question.id},
        },
    )
    assert response.status_code == 422, response.text
    assert "drafts_question_needs_pick" in response.text, response.text
    assert world.app.state.user_questions[question.id].status == "pending"


# --------------------------------------------------------------------------- #
# Dismissed, expired                                                          #
# --------------------------------------------------------------------------- #
def test_a_dismissed_pick_closes_the_run_cancelled_and_the_conversation_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, engine, question = _paused(world, monkeypatch)
    variants_id = question.metadata["variants_id"]

    cancelled = TestClient(world.app).post(
        f"/v1/sessions/{world.sid}/questions/{question.id}/cancel"
    )

    assert cancelled.status_code == 200, cancelled.text
    run = load_run(world.app, world.sid, variants_id)
    assert (run.status, run.closed_reason, run.question_id) == (
        "cancelled",
        "variant_pick_cancelled",
        question.id,
    )
    session = world.app.state.sessions.get(world.sid)
    assert session.status == "idle"
    assert session.metadata["variant_pending"] == {}
    assert session.metadata["pending_user_question_id"] == ""
    assert session.metadata["variant_notice"]["status"] == "cancelled"
    [closed] = _closed_events(world)
    assert (closed["status"], closed["reason"]) == ("cancelled", "variant_pick_cancelled")
    assert "superseded_by_message_id" not in closed
    [served] = _runs(world.app, world.sid)
    assert (served["status"], served["closed_reason"]) == ("cancelled", "variant_pick_cancelled")
    assert _interaction(world.app, world.sid, question.id)["status"] == "cancelled"
    assert _answer_route(world.app, world.sid, question.id, RUN0).status_code == 409

    done = _turn(world.app, world.sid, lm, NEXT, "turn_2")

    assert done.answer == SUMMARY
    [notice] = _notice(engine)
    assert "were dismissed by the user without a pick" in notice
    assert world.app.state.sessions.get(world.sid).metadata["variant_notice"] == {}
    assert len(_closed_events(world)) == 1  # the turn found it closed: no second close
    _no_drafts_in(_line(world.arc, world.sid))
    [record] = preference_records(world.app, world.sid)
    assert (record.status, record.pick) == ("cancelled", None)


def test_an_expired_pick_closes_the_run_expired(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, engine, question = _paused(world, monkeypatch, expires=1)
    assert question.expires_at  # the window the agent asked for
    timer = world.app.state.ask_user_deadlines[question.id]

    timer.join()  # the real deadline timer fires and runs to its end

    assert world.app.state.user_questions[question.id].status == "expired"
    run = load_run(world.app, world.sid, question.metadata["variants_id"])
    assert (run.status, run.closed_reason) == ("expired", "variant_pick_expired")
    session = world.app.state.sessions.get(world.sid)
    assert session.status == "idle"
    assert session.metadata["variant_pending"] == {}
    [closed] = _closed_events(world)
    assert closed["status"] == "expired"
    [served] = _runs(world.app, world.sid)
    assert served["status"] == "expired"
    assert _interaction(world.app, world.sid, question.id)["status"] == "expired"

    done = _turn(world.app, world.sid, lm, NEXT, "turn_2")

    assert done.answer == SUMMARY
    [notice] = _notice(engine)
    assert "were never picked: the pick expired" in notice


def test_no_window_means_no_deadline(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _lm, _engine, question = _paused(world, monkeypatch)
    assert question.expires_at == ""
    assert question.id not in getattr(world.app.state, "ask_user_deadlines", {})


# --------------------------------------------------------------------------- #
# Restarts                                                                    #
# --------------------------------------------------------------------------- #
def test_a_restart_between_the_pause_and_the_new_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lm, _engine, question = _paused(world, monkeypatch)
    variants_id = question.metadata["variants_id"]

    app2, _arc2, events2 = _app(world.sessions_path)  # a new process, same clio-core
    lm2, engine2 = routed_lm({SCOPE: [Reply(text=SUMMARY)]})
    _turn(app2, world.sid, lm2, NEXT, "turn_2")

    assert load_run(app2, world.sid, variants_id).status == "superseded"
    assert [e.payload["status"] for e in events2 if e.event_type == "variant.closed"] == [
        "superseded"
    ]
    assert "were never picked: the user sent a new message instead" in _notice(engine2)[0]

    app3, _arc3, _events3 = _app(world.sessions_path)  # and once more: all of it durable
    assert app3.state.user_questions[question.id].status == "cancelled"
    assert _answer_route(app3, world.sid, question.id, RUN0).status_code == 409
    [served] = _runs(app3, world.sid)
    assert (served["status"], served["superseded_by_message_id"]) == ("superseded", "turn_2")
    assert app3.state.sessions.get(world.sid).metadata["variant_pending"] == {}


def test_a_close_cut_short_by_a_restart_is_finished_at_the_next_turn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question was dismissed, the process died before the run was closed."""
    _lm, _engine, question = _paused(world, monkeypatch)
    assert claim_question_transition(world.app, question.id, "cancelled") is not None
    variants_id = question.metadata["variants_id"]
    assert load_run(world.app, world.sid, variants_id).status == "awaiting_pick"

    app2, _arc2, _events2 = _app(world.sessions_path)
    lm2, engine2 = routed_lm({SCOPE: [Reply(text=SUMMARY)]})
    _turn(app2, world.sid, lm2, NEXT, "turn_2")

    run = load_run(app2, world.sid, variants_id)
    assert (run.status, run.closed_reason) == ("cancelled", "variant_pick_cancelled")
    assert app2.state.sessions.get(world.sid).metadata["variant_pending"] == {}
    assert "were dismissed by the user without a pick" in _notice(engine2)[0]
