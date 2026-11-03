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
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import make_arc_store
from clio_agent.arc.task_types import QuestionReply
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import read_steps
from clio_agent.gact.agents.variant_drafts import (
    build_draft_alternatives_tool,
)
from clio_agent.gact.agents.variant_records import load_run, preference_records
from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3.event import event_to_v3
from clio_agent.gact.question_surface import question_row
from clio_agent.runtime.lm_activity import set_live_chunk_emitter
from tests._scripted_engine import Reply, calls, routed_lm, summarize
from tests.test_gact.task_records import record_assignment_fixture

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
    lifespans: ExitStack

    @property
    def client(self) -> TestClient:
        """Use the request portal owned by this application's running lifespan."""
        return self.app.state.test_client


def _app(sessions_path: Path, *, lifespans: ExitStack) -> tuple[Any, ARCMemory, list[Any]]:
    arc = ARCMemory(store=make_arc_store(backend="cte"))
    app = build_app(sessions_path=sessions_path, arc=arc)
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit

    def emit(event: Any) -> Any:
        events.append(event)
        return real_emit(event)

    app.state.semantic_event_sink.emit = emit
    app.state.test_client = lifespans.enter_context(TestClient(app))
    return app, arc, events


@pytest.fixture(name="world")
def variant_world(tmp_path: Path) -> Iterator[World]:
    with ExitStack() as lifespans:
        app, arc, events = _app(tmp_path / "sessions.json", lifespans=lifespans)
        sid = app.state.sessions.create(workspace_id="ws_default", title="drafts").id
        record_assignment_fixture(app, sid)
        yield World(app, arc, sid, events, tmp_path / "sessions.json", lifespans)


def _bind(app: Any, sid: str, turn: str) -> list[Any]:
    from clio_agent.arc.task_types import AssignmentDetails

    tokens = [ctx.set_app(app), ctx.set_session_id(sid)]
    current = app.state.task_history.latest_assignment(sid)
    assert current is not None
    details = AssignmentDetails.model_validate(app.state.task_history.read_content(current.details))
    ctx.set_turn_identity(
        app=app,
        session_id=sid,
        turn_id=turn,
        trace_id="trace:" + turn,
        assignment_handle=current.handle,
        context_epoch=details.context_epoch,
    )
    ctx.set_primary_agent_id(SCOPE)
    return [
        *tokens,
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
    """Read the Question already posted by the short control; do not pause a turn."""
    rows = [
        app.state.task_history.task_snapshot(handle)
        for handle in app.state.task_history.tree_handles(sid)
    ]
    questions = [
        task
        for task in rows
        if task is not None and task.kind == "Question" and task.terminal_at is None
    ]
    assert len(questions) == 1
    return question_row(app, questions[0].handle)


def _answer(app: Any, question: Any, pick: str, comment: str = "") -> None:
    """Confirm the human reply through the sole Core Question owner."""
    app.state.questions.answer(
        question.id,
        QuestionReply(
            answer=comment, selected_options=(pick,), responder_human=app.state.human_principal
        ),
    )


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
        assert "Produce only ONE candidate here." in engine.by_scope[scope][0].system
        assert not any(d in t for t in seen for d in ("Draft A", "Draft B", "Draft C"))
    # selection by reward: the judge scored every try (no threshold reached), 0.9 won
    assert len(engine.by_scope[""]) == 3
    assert "Evaluate ONE candidate independently" in engine.by_scope[""][0].system
    assert "Do not penalize a candidate for containing only one" in engine.by_scope[""][0].system
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


def test_n_is_capped_by_config(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_VARIANTS_MAX_N", "2")
    lm, engine = routed_lm(
        {
            SCOPE: [_draft(5), Reply(text="Choose a draft; I can continue independent work.")],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
        }
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")

    run = load_run(world.app, world.sid, _only_run(world))
    assert (run.n, run.n_requested, len(run.tries)) == (2, 5, 2)
    assert RUN2 not in engine.by_scope
    [note] = [e.payload for e in world.events if e.event_type == "variant.try"][:1]
    assert note["n"] == 2
    assert note["rubric"] == "clear and polite"


def test_five_poems_are_five_single_candidate_attempts(world: World) -> None:
    """A plural request stays intact, while every real try receives a singular boundary."""
    question = "Make drafts with the tool of 5 different poems for a friend."
    script = {SCOPE: [_draft(5), Reply(text="Choose a draft; I can continue independent work.")]}
    for index in range(5):
        script[f"{SCOPE}#run{index}"] = [Reply(text=f"Poem {index + 1}")]
    lm, engine = routed_lm(script)
    _forward(world.app, world.sid, lm, question, "turn_poems")
    run = load_run(world.app, world.sid, _only_run(world))
    assert run.n == run.n_requested == len(run.tries) == 5
    for index in range(5):
        [request] = engine.by_scope[f"{SCOPE}#run{index}"]
        assert "Return exactly one complete draft" in request.system
        assert question in _texts(request)
        assert run.try_at(index).text == f"Poem {index + 1}"


def test_planning_deltas_are_reasoning_instead_of_candidate_content(world: World) -> None:
    """Native planner text and provider reasoning stay off the draft's text lane."""
    from clio_agent.gact.agents.variant_events import _delta_emitter
    from clio_agent.gact.agents.variant_records import VariantRun

    tokens = _bind(world.app, world.sid, "turn_deltas")
    try:
        run = VariantRun(
            variants_id="var_deltas",
            session_id=world.sid,
            agent_id=SCOPE,
            turn_id="turn_deltas",
            origin="draft_alternatives",
            strategy="best_of_n",
            judge="user",
            n=2,
            n_requested=2,
            rubric="one poem",
        )
        emit = _delta_emitter(run, 0)

        async def stream() -> None:
            for field in ("next_thought", "reasoning", "provider_thinking:model", "answer"):
                await emit(field, SCOPE, field)

        asyncio.run(stream())
    finally:
        for token in reversed(tokens):
            ctx.reset(token)
    assert [(row["kind"], row["delta"]) for row in _variant_events(world, "variant.try.delta")] == [
        ("thinking", "next_thought"),
        ("thinking", "reasoning"),
        ("thinking", "provider_thinking:model"),
        ("text", "answer"),
    ]


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
        loop.close()  # the test's own loop: release its selector
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


@pytest.mark.parametrize("strategy", ["best_of_n", "refine"])
def test_user_drafts_post_a_question_and_the_model_continues(
    world: World, monkeypatch: pytest.MonkeyPatch, strategy: str
) -> None:
    """A human choice is asynchronous; the same model can perform its next action."""
    script = {
        SCOPE: [_draft(2, strategy=strategy), Reply(text="I can continue independent work.")],
        RUN0: [Reply(text="Draft A")],
    }
    if strategy == "best_of_n":
        script[RUN1] = [Reply(text="Draft B")]
    lm, engine = routed_lm(script)
    prediction = _forward(world.app, world.sid, lm, ASK, "turn_1")
    assert prediction.answer == "I can continue independent work."
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)
    assert question.status == "pending"
    assert len(engine.by_scope[SCOPE]) == 2
    expected = [RUN0, RUN1] if strategy == "best_of_n" else [RUN0]
    assert [option.value for option in question.options] == expected
    assert [option.description for option in question.options] == ["Draft A", "Draft B"][
        : len(expected)
    ]
    task = world.app.state.task_history.task_snapshot(question.id)
    assert (
        task.owner.assignment_id == world.app.state.task_history.latest_assignment(world.sid).handle
    )
    assert task.terminal_at is None


def test_selected_user_draft_is_collected_without_a_new_model_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact stored choice determines the winner, including after Core replay."""
    from clio_agent.gact.agents.variant_resume import resume_question
    from clio_agent.gact.task_history import TaskHistory

    lm, engine = routed_lm(
        {
            SCOPE: [_draft(2), Reply(text="Waiting for your choice.")],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
        }
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)
    _answer(world.app, question, RUN1)
    restored = TaskHistory(world.arc)
    restored.restore()
    assert restored.task_snapshot(question.id) == world.app.state.task_history.task_snapshot(
        question.id
    )
    before = len(engine.requests)
    tokens = _bind(world.app, world.sid, "turn_2")
    try:
        agent = ClioReAct("question -> answer", tools=[build_draft_alternatives_tool()])
        with dspy.context(lm=lm):
            selected = resume_question(agent, {"question": ASK}, question.id)
    finally:
        for token in reversed(tokens):
            ctx.reset(token)
    assert selected.answer == "Draft B" and selected.termination_reason == "variant_selected"
    assert len(engine.requests) == before
    run = load_run(world.app, world.sid, question.metadata["variants_id"])
    assert run.status == "selected" and run.selected_index == 1
    assert [item.text for item in run.tries] == ["Draft A", "Draft B"]
    [record] = preference_records(world.app, world.sid)
    assert (record.pick, record.selected_index) == (1, 1)


@pytest.mark.parametrize("picks", [[], [RUN0, RUN1]])
def test_user_choice_requires_exactly_one_draft(
    world: World, monkeypatch: pytest.MonkeyPatch, picks: list[str]
) -> None:
    lm, _engine = routed_lm(
        {
            SCOPE: [_draft(2), Reply(text="Choose a draft.")],
            RUN0: [Reply(text="Draft A")],
            RUN1: [Reply(text="Draft B")],
        }
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)
    response = world.client.post(
        f"/v1/sessions/{world.sid}/questions/{question.id}/answer",
        json={"selected_options": picks, "answer": ""},
    )
    assert response.status_code == 422, response.text
    assert world.app.state.task_history.task_snapshot(question.id).raw_status == "pending"
