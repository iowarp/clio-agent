"""``draft_alternatives`` and the human judge, around the REAL ClioReAct on real clio-core.

Every test drives the real loop (the main agent and each try are ``ClioReAct`` forwards)
on a real clio-core ARC through one scripted engine that records every request, routed
per scope so parallel tries each get their own script. What is asserted is DSPy's
documented behaviour through the composition: N attempts, selection by reward (BestOfN),
the threshold stop, and Refine's feedback reaching the next try -- plus clio's own part:
each try on its own scope, the pause on clio-core (surviving a restart), the line that
continues, the preference record and the events.

SABOTAGE (each run manually, each went RED, then was restored):

* ``_Drafts.parallel`` with ``num_threads=1`` (tries one after another): the overlap
  hold in ``test_lm_judged_drafts_run_at_once_and_bestofn_keeps_the_highest_reward``
  times out.
* ``_Drafts.select`` returning the last try instead of BestOfN's choice: both LM-judged
  selection tests fail (the 0.9 / the threshold-reaching try is not the answer).
* ``record_winner`` called without ``cut_id`` in ``_Settle.settle``: the base keeps the
  ``draft_alternatives`` step and a second copy of the question.
* ``variant_lines.record_advice`` skipped in ``_Drafts.one_try``: the refine test's
  second try carries no advice.
* ``resume_pending`` reading the pick from the first option instead of the answer: the
  restart test continues from "Draft A".
* ``capped_n`` returning ``n`` unchanged: the cap test sees five tries.
"""

from __future__ import annotations

import asyncio
import threading
import types
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import make_arc_store
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import read_steps
from clio_agent.gact.agents.variant_drafts import (
    UserJudgedVariant,
    build_draft_alternatives_tool,
)
from clio_agent.gact.agents.variant_records import load_run, preference_records
from clio_agent.gact.app import build_app
from clio_agent.gact.elicitation_bridge import claim_question_transition
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3.event import event_to_v3
from clio_agent.gact.runtime.type_parsing import parse_module_variant
from clio_agent.gact.transcript import EventBusTranscriptPublisher
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import Message
from clio_agent.gact.user_question_pause import maybe_pause_for_user
from clio_agent.runtime.lm_activity import set_live_chunk_emitter
from tests._scripted_engine import Reply, calls, routed_lm, summarize

SCOPE = "writer"
ASK = "Write the email to Dana about the delayed shipment."
RUN0, RUN1, RUN2 = f"{SCOPE}#run0", f"{SCOPE}#run1", f"{SCOPE}#run2"


@dataclass
class World:
    """One app on real clio-core with a session and a captured highway."""

    app: Any
    arc: ARCMemory
    sid: str
    events: list[Any]
    sessions_path: Path


def _app(sessions_path: Path) -> tuple[Any, ARCMemory, list[Any]]:
    arc = ARCMemory(store=make_arc_store(backend="cte"))
    app = build_app(sessions_path=sessions_path, arc=arc)
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit

    def emit(event: Any) -> Any:
        events.append(event)
        return real_emit(event)

    app.state.semantic_event_sink.emit = emit
    return app, arc, events


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    app, arc, events = _app(tmp_path / "sessions.json")
    sid = app.state.sessions.create(workspace_id="ws_default", title="drafts").id
    yield World(app, arc, sid, events, tmp_path / "sessions.json")


def _bind(app: Any, sid: str, turn: str) -> list[Any]:
    return [
        ctx.set_app(app),
        ctx.set_session_id(sid),
        ctx.set_turn_id_token(turn),
        ctx.set_react_scope(SCOPE, "react"),
        ctx.set_react_session(sid),
    ]


def _forward(app: Any, sid: str, lm: Any, question: str, turn: str) -> dspy.Prediction:
    """One turn of the main agent: the real loop, with draft_alternatives among its tools."""
    agent = ClioReAct("question -> answer", tools=[build_draft_alternatives_tool()])
    tokens = _bind(app, sid, turn)
    try:
        with dspy.context(lm=lm):
            return agent(question=question)
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def _surface(app: Any, sid: str, turn: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """What the turn does after a yield: the real pause mints the choice question."""
    monkeypatch.setattr(
        "clio_agent.gact.user_question_pause._finalize_context_frame", lambda *a, **k: None
    )
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
        user_text=ASK,
        turn_agent_id=SCOPE,
        turn_id=turn,
        trace_id=f"trace_{turn}",
        retry_attempt_id="",
        native_images=[],
        context_frame={"id": f"frame_{turn}"},
    )
    state.transcript = app.state.turn_transcripts.open_turn(
        sid, turn, EventBusTranscriptPublisher(app.state.bus, sid)
    )

    def no_retry(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    assert maybe_pause_for_user(state, types.SimpleNamespace(), update_retry_attempt=no_retry)
    question_id = app.state.sessions.get(sid).metadata["pending_user_question_id"]
    return app.state.user_questions[question_id]


def _answer(app: Any, question: Any, pick: str, comment: str = "") -> None:
    """The user's answer, through the one atomic transition every answer route takes."""
    answered = claim_question_transition(
        app, question.id, "answered", answer=comment, selected_options=[pick], answered_by="human"
    )
    assert answered is not None


def _score(value: float) -> Reply:
    return Reply(text=f"[[ ## score ## ]]\n{value}\n\n[[ ## completed ## ]]")


def _draft(n: int, *, strategy: str = "best_of_n", judge: str = "user") -> Reply:
    args = {"n": n, "rubric": "clear and polite", "strategy": strategy, "judge": judge}
    return calls(("draft_alternatives", args), text="I'll draft a few versions.")


def _line(arc: ARCMemory, sid: str, scope: str = SCOPE) -> list[tuple[str, str]]:
    """The scope's conversation as (role, text), CLIO's own notes left out."""
    rows = []
    for role, parts in summarize(read_steps(arc, sid, scope)):
        text = "".join(p[1] for p in parts if p[0] == "text")
        calls_ = [p[2] for p in parts if p[0] == "call"]
        if text.startswith("[clio: tool_use]"):
            continue
        rows.append((role, text or ",".join(calls_)))
    return rows


def _texts(request: Any) -> list[str]:
    return [p[1] for _role, parts in summarize(request.messages) for p in parts if p[0] == "text"]


def _variant_events(world: World, kind: str) -> list[dict[str, Any]]:
    return [e.payload for e in world.events if e.event_type == kind]


# --------------------------------------------------------------------------- #
# LM judge: DSPy's BestOfN over parallel tries                                #
# --------------------------------------------------------------------------- #
def test_lm_judged_drafts_run_at_once_and_bestofn_keeps_the_highest_reward(world: World) -> None:
    lm, engine = routed_lm(
        {
            SCOPE: [_draft(3, judge="lm")],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
            RUN2: [Reply(text="Draft C")],
            "": [_score(0.2), _score(0.9), _score(0.5)],  # the judge, in BestOfN's order
        },
        overlap=3,  # each try's first request waits until all three are in flight
    )
    pred = _forward(world.app, world.sid, lm, ASK, "turn_1")

    assert pred.answer == "Draft B"
    assert pred.termination_reason == "variant_selected"
    # N attempts, each on its own scope, all in flight together (the barrier released)
    assert [len(engine.by_scope[s]) for s in (RUN0, RUN1, RUN2)] == [1, 1, 1]
    for scope in (RUN0, RUN1, RUN2):
        seen = _texts(engine.by_scope[scope][0])
        assert ASK in seen
        assert not any(d in t for t in seen for d in ("Draft A", "Draft B", "Draft C"))
    # selection by reward: the judge scored every try (no threshold reached), 0.9 won
    assert len(engine.by_scope[""]) == 3
    # the winner's line continues the conversation: one question, its answer, no drafts call
    assert _line(world.arc, world.sid) == [("user", ASK), ("assistant", "Draft B")]
    [record] = preference_records(world.app, world.sid)
    assert record.judge == "lm"
    assert record.strategy == "best_of_n"
    assert record.rubric == "clear and polite"
    assert [c.text for c in record.candidates] == ["Draft A", "Draft B", "Draft C"]
    assert record.scores == ((0, 0.2), (1, 0.9), (2, 0.5))
    assert record.selected_index == 1
    assert all(c.tokens == {"input": 10, "output": 5, "total": 15} for c in record.candidates)


def test_bestofn_stops_at_the_threshold(world: World) -> None:
    lm, engine = routed_lm(
        {
            SCOPE: [_draft(3, judge="lm")],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
            RUN2: [Reply(text="Draft C")],
            "": [_score(1.0)],  # reaches the threshold: BestOfN stops judging
        }
    )
    pred = _forward(world.app, world.sid, lm, ASK, "turn_1")

    assert pred.answer == "Draft A"
    assert len(engine.by_scope[""]) == 1
    run = load_run(world.app, world.sid, _only_run(world))
    assert [t.score for t in run.tries] == [1.0, None, None]
    assert [t.status for t in run.tries] == ["completed"] * 3


def _only_run(world: World) -> str:
    ids = {p["variants_id"] for p in _variant_events(world, "variant.try")}
    assert len(ids) == 1
    return ids.pop()


# --------------------------------------------------------------------------- #
# The human judge: a pause on clio-core                                       #
# --------------------------------------------------------------------------- #
def test_a_user_pick_resumes_with_the_picks_line_even_after_a_restart(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, engine = routed_lm(
        {SCOPE: [_draft(2)], RUN0: [Reply(text="Draft A")], RUN1: [Reply(text="Draft B")]}
    )
    paused = _forward(world.app, world.sid, lm, ASK, "turn_1")
    assert paused.termination_reason == "draft_alternatives_yield"
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)

    assert question.kind == "choice"
    assert question.allow_freeform is True
    assert [(o.value, o.description) for o in question.options] == [
        (RUN0, "Draft A"),
        (RUN1, "Draft B"),
    ]
    variants_id = question.metadata["variants_id"]
    assert load_run(world.app, world.sid, variants_id).status == "awaiting_pick"

    # a restart: a new process on the same session registry and the same clio-core
    app2, arc2, _events = _app(world.sessions_path)
    restored = app2.state.user_questions[question.id]
    _answer(app2, restored, RUN1)
    resumed = _forward(app2, world.sid, routed_lm({})[0], "[Answer] Draft 2", "turn_2")

    assert resumed.answer == "Draft B"
    assert resumed.termination_reason == "variant_selected"
    assert len(engine.requests) == 3  # the resume called no model
    assert _line(arc2, world.sid) == [("user", ASK), ("assistant", "Draft B")]
    [record] = preference_records(app2, world.sid)
    assert (record.judge, record.pick, record.selected_index) == ("user", 1, 1)
    assert [c.text for c in record.candidates] == ["Draft A", "Draft B"]
    assert app2.state.sessions.get(world.sid).metadata["variant_pending"] == {}


def test_refine_with_a_user_comment_tells_the_next_try_the_advice(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, engine = routed_lm(
        {
            SCOPE: [_draft(3, strategy="refine")],
            RUN0: [Reply(text="Draft one, long and formal.")],
            RUN1: [Reply(text="Short draft.")],
        }
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")
    first = _surface(world.app, world.sid, "turn_1", monkeypatch)
    assert [o.value for o in first.options] == [RUN0]  # Refine drafts one at a time

    _answer(world.app, first, RUN0, comment="Make it shorter.")
    again = _forward(world.app, world.sid, lm, "[Answer] refine", "turn_2")
    assert again.termination_reason == "variant_pick_yield"

    [second_try] = engine.by_scope[RUN1]
    seen = _texts(second_try)
    assert "[clio: variant_advice]\nMake it shorter." in seen  # the feedback reaches it
    assert "Draft one, long and formal." in seen  # forked from the pick
    assert seen.count(ASK) == 1  # it continues the pick's line, no second task

    second = _surface(world.app, world.sid, "turn_2", monkeypatch)
    assert [o.value for o in second.options] == [RUN0, RUN1]
    _answer(world.app, second, RUN1)  # a pick without a comment accepts it
    done = _forward(world.app, world.sid, lm, "[Answer] accept", "turn_3")

    assert done.answer == "Short draft."
    assert _line(world.arc, world.sid) == [
        ("user", ASK),
        ("assistant", "Draft one, long and formal."),
        ("user", "[clio: variant_advice]\nMake it shorter."),
        ("assistant", "Short draft."),
    ]
    [record] = preference_records(world.app, world.sid)
    assert record.strategy == "refine"
    assert record.advice == ("Make it shorter.",)
    assert record.candidates[1].forked_from == 0
    assert (record.pick, record.comment, record.selected_index) == (1, "", 1)


def test_lm_judged_refine_tells_the_next_try_its_advice(world: World) -> None:
    lm, engine = routed_lm(
        {
            SCOPE: [_draft(2, strategy="refine", judge="lm")],
            RUN0: [Reply(text="Draft one, long and formal.")],
            RUN1: [Reply(text="Short draft.")],
            # the judge and clio's advice call, in DSPy Refine's order
            "": [_score(0.3), Reply(text="Cut it to two sentences."), _score(1.0)],
        }
    )
    pred = _forward(world.app, world.sid, lm, ASK, "turn_1")

    assert pred.answer == "Short draft."
    advice = "[clio: variant_advice]\nCut it to two sentences."
    assert advice in _texts(engine.by_scope[RUN1][0])
    # the winner's line as it ran: the advice it was given, the task, its answer
    assert _line(world.arc, world.sid) == [
        ("user", advice),
        ("user", ASK),
        ("assistant", "Short draft."),
    ]
    [record] = preference_records(world.app, world.sid)
    assert (record.origin, record.strategy, record.judge) == ("draft_alternatives", "refine", "lm")
    assert record.scores == ((0, 0.3), (1, 1.0))
    assert record.advice == ("Cut it to two sentences.",)


def test_a_user_judged_subagent_pauses_and_resumes_the_same_way(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn strategy (or blueprint) with ``judge: user`` wraps the child in the pause."""
    spec = parse_module_variant(
        {"variant": "best_of_n", "n": 2, "judge": "user", "reward": {"instructions": "short"}},
        agent_id=SCOPE,
    )
    child = UserJudgedVariant(ClioReAct("question -> answer", tools=[]), spec, agent_id=SCOPE)
    lm, engine = routed_lm({RUN0: [Reply(text="Draft A")], RUN1: [Reply(text="Draft B")]})

    def forward(question: str, turn: str) -> Any:
        tokens = _bind(world.app, world.sid, turn)
        try:
            with dspy.context(lm=lm):
                return child(question=question)
        finally:
            for token in reversed(tokens):
                ctx.reset(token)

    paused = forward(ASK, "turn_1")
    assert paused.termination_reason == "variant_pick_yield"
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)
    _answer(world.app, question, RUN0, comment="the first is fine")
    done = forward("[Answer]", "turn_2")

    assert done.answer == "Draft A"
    assert len(engine.requests) == 2
    assert _line(world.arc, world.sid) == [("user", ASK), ("assistant", "Draft A")]
    [record] = preference_records(world.app, world.sid)
    assert (record.origin, record.pick, record.comment) == (
        "module_variant",
        0,
        "the first is fine",
    )


def test_n_is_capped_by_config(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_VARIANTS_MAX_N", "2")
    lm, engine = routed_lm(
        {SCOPE: [_draft(5)], RUN0: [Reply(text="Draft A")], RUN1: [Reply(text="Draft B")]}
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")

    run = load_run(world.app, world.sid, _only_run(world))
    assert (run.n, run.n_requested, len(run.tries)) == (2, 5, 2)
    assert RUN2 not in engine.by_scope
    [note] = [e.payload for e in world.events if e.event_type == "variant.try"][:1]
    assert note["n"] == 2


def test_a_drafts_answer_must_pick_exactly_one(world: World) -> None:
    from fastapi import HTTPException

    from clio_agent.gact.elicitation_schema import check_elicitation_answer

    row = types.SimpleNamespace(
        id="q", session_id=world.sid, metadata={"variants_id": "var_x"}, kind="choice"
    )
    with pytest.raises(HTTPException) as err:
        check_elicitation_answer(
            cast(Any, row), types.SimpleNamespace(selected_options=[], answer="hm")
        )
    assert err.value.status_code == 422


# --------------------------------------------------------------------------- #
# What the UI gets                                                            #
# --------------------------------------------------------------------------- #
def test_tries_stream_to_their_tabs_and_project_to_v3(world: World) -> None:
    lane: list[tuple[str, str]] = []

    async def main_lane(text: str, agent_id: str | None, field: str) -> None:
        lane.append((field, text))

    loop = asyncio.new_event_loop()
    pump = threading.Thread(target=loop.run_forever, daemon=True)
    pump.start()
    lm, _engine = routed_lm(
        {
            SCOPE: [_draft(2, judge="lm")],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
            "": [_score(0.1), _score(0.8)],
        }
    )
    try:
        context = __import__("contextvars").copy_context()
        context.run(set_live_chunk_emitter, loop, main_lane)
        pred = context.run(_forward, world.app, world.sid, lm, ASK, "turn_1")
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        pump.join()
    assert pred.answer == "Draft B"

    # the drafts never stream into the turn's answer lane, each streams into its tab
    assert not any("Draft" in text for _f, text in lane)
    deltas = _variant_events(world, "variant.try.delta")
    assert {(d["try_index"], d["delta"]) for d in deltas} == {(0, "Draft A"), (1, "Draft B")}

    variants_id = _only_run(world)
    frames = [
        event_to_v3(Event(type="semantic.event", session_id=world.sid, payload=e.to_dict("sse")))
        for e in world.events
        if e.event_type.startswith("variant.")
    ]
    tries = [f for f in frames if f["type"] == "variant.try.upserted"]
    assert {f["entity_id"] for f in tries} == {f"{variants_id}:0", f"{variants_id}:1"}
    done = [f["payload"] for f in tries if f["payload"]["state"] == "completed"]
    assert {(p["try_index"], p["text"], p.get("score")) for p in done} >= {
        (0, "Draft A", 0.1),
        (1, "Draft B", 0.8),
    }
    [selected] = [f for f in frames if f["type"] == "variant.selected"]
    assert selected["entity_id"] == variants_id
    assert selected["payload"]["selected_index"] == 1
    assert selected["payload"]["text"] == "Draft B"
    assert {f["type"] for f in frames} == {
        "variant.try.upserted",
        "variant.try.delta",
        "variant.selected",
    }
    # a try's own step events carry its tab
    stamped = [
        e.payload
        for e in world.events
        if e.event_type.startswith("expert.") and "variants_id" in e.payload
    ]
    assert {p["try_index"] for p in stamped} == {0, 1}
