"""The agent-context projection spans turns (agent-loop rebuild, phase 3).

clio-core holds the conversation: every turn's user message, steps, tool results and
answer are recorded on the scope's live plane, and each model call is the projection
of that plane. So a later turn sees the earlier turns as real messages (never a prose
blob), and every request is a prefix of the next one unless a recorded op landed.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import TOOL_USE_NOTE, ClioReAct
from clio_agent.gact.agents.clio_react_record import ContextWriteError
from tests._scripted_engine import Reply, calls, scripted_lm, wire

SESSION, SCOPE = "s1", "agentA"


@pytest.fixture
def arc(tmp_path: Any) -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@contextmanager
def _plane(arc: ARCMemory, **state: Any) -> Iterator[None]:
    app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc, **state))
    app_token = ctx.set_app(app)
    scope_token = ctx.set_react_scope(SCOPE)
    session_token = ctx.set_react_session(SESSION)
    try:
        yield
    finally:
        ctx.reset(session_token)
        ctx.reset(scope_token)
        ctx.reset(app_token)


def _agent() -> ClioReAct:
    return ClioReAct(
        "question -> answer", tools=[dspy.Tool(lambda q: f"hits for {q}", name="search")]
    )


def _two_turns(arc: ARCMemory) -> list[Any]:
    lm, engine = scripted_lm(
        [
            calls(("search", {"q": "ridgecrest"}), text="Looking."),
            Reply(text="Station P595 moved most."),
            Reply(text="It moved 20 cm."),
        ]
    )
    agent = _agent()
    with _plane(arc), dspy.context(lm=lm):
        agent(question="which station moved most?")
        agent(question="by how much?")
    return engine.requests


def test_a_later_turn_sees_the_earlier_turn_as_real_messages(arc: ARCMemory) -> None:
    requests = _two_turns(arc)
    assert wire(requests[2]) == [
        ("user", [("text", f"[clio: tool_use]\n{TOOL_USE_NOTE}")]),
        ("user", [("text", "which station moved most?")]),
        ("assistant", [("text", "Looking."), ("call", "call_0_0", "search", {"q": "ridgecrest"})]),
        ("tool", [("result", "call_0_0", "hits for ridgecrest", False)]),
        ("assistant", [("text", "Station P595 moved most.")]),
        ("user", [("text", "by how much?")]),
    ]


def test_the_user_message_keeps_its_attachments_across_turns(arc: ARCMemory) -> None:
    lm, engine = scripted_lm([Reply(text="A plot."), Reply(text="Blue.")])
    agent = ClioReAct("question, images: list[dspy.Image] -> answer", tools=[])
    image = dspy.Image("data:image/png;base64,aGk=")
    with _plane(arc), dspy.context(lm=lm):
        agent(question="what is this?", images=[image])
        agent(question="what color?", images=[])
    assert wire(engine.requests[1])[0] == ("user", [("text", "what is this?"), ("ImagePart",)])
    assert engine.requests[1].messages[0] == engine.requests[0].messages[0]


def test_a_lost_plane_write_fails_the_turn_typed(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("plane unwritable")

    monkeypatch.setattr(arc, "append_segment", _broken)
    lm, engine = scripted_lm([Reply(text="never")])
    with _plane(arc), dspy.context(lm=lm), pytest.raises(ContextWriteError) as err:
        _agent()(question="q")
    assert err.value.reason == "arc_context_write_failed"
    assert engine.requests == [], "the model must not be called on a context missing the turn"


def test_every_request_is_a_prefix_of_the_next_across_steps_and_turns(arc: ARCMemory) -> None:
    requests = _two_turns(arc)
    for before, after in zip(requests, requests[1:], strict=False):
        assert after.system == before.system
        assert after.messages[: len(before.messages)] == before.messages
        assert len(after.messages) > len(before.messages)


def test_an_agent_with_tools_is_told_once_that_a_step_may_call_several(
    arc: ARCMemory,
) -> None:
    """Owner, 2026-09-30: a step's calls run at the same time, yet agents loaded skills
    one step each -- a model round trip apiece. CLIO says so once (an injection the
    user sees), not in every request, and never to an agent without tools."""
    requests = _two_turns(arc)
    notes = [m for m in requests[2].messages if "[clio: tool_use]" in str(m.parts)]
    assert len(notes) == 1


def test_an_agent_without_tools_is_not_told_about_them(arc: ARCMemory) -> None:
    lm, engine = scripted_lm([Reply(text="hi")])
    with _plane(arc), dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[])(question="q")
    assert "[clio: tool_use]" not in str(engine.requests[0].messages)


def test_injections_are_recorded_once_before_the_user_message(arc: ARCMemory) -> None:
    """CLIO's own additions (todos, plan reminder, task results, memory hits) are
    recorded messages of their own -- never glued into the user's text -- and one
    whose content has not changed since it was last recorded is not repeated."""
    lm, engine = scripted_lm([Reply(text="one"), Reply(text="two"), Reply(text="three")])
    agent = ClioReAct("question -> answer", tools=[])
    todos_v1 = [("todos", "- [ ] load the data")]
    todos_v2 = [("todos", "- [x] load the data")]
    with _plane(arc), dspy.context(lm=lm):
        for question, injections in (("q1", todos_v1), ("q2", todos_v1), ("q3", todos_v2)):
            token = ctx.set_turn_injections(injections)
            try:
                agent(question=question)
            finally:
                ctx.reset(token)
    assert wire(engine.requests[2]) == [
        ("user", [("text", "[clio: todos]\n- [ ] load the data")]),
        ("user", [("text", "q1")]),
        ("assistant", [("text", "one")]),
        ("user", [("text", "q2")]),
        ("assistant", [("text", "two")]),
        ("user", [("text", "[clio: todos]\n- [x] load the data")]),
        ("user", [("text", "q3")]),
    ]


def test_steers_and_child_results_arrive_at_the_step_boundary(arc: ARCMemory) -> None:
    """What arrives mid-turn (a user steer, a finished child's result) reaches the model
    at the next step boundary as a message of its own -- after any step, whatever tool
    it ran -- and the tool observation it was once glued onto stays as it was."""
    arrivals = [[], [("steer", "use 2024 only"), ("task_results", "child: done")]]

    def drain() -> list[tuple[str, str]]:
        return arrivals.pop(0) if arrivals else []

    lm, engine = scripted_lm([calls(("search", {"q": "x"})), Reply(text="ok")])
    with _plane(arc, pending_loop_inbox_drain=drain), dspy.context(lm=lm):
        _agent()(question="q")
    assert wire(engine.requests[1])[-3:] == [
        ("tool", [("result", "call_0_0", "hits for x", False)]),
        ("user", [("text", "use 2024 only")]),
        ("user", [("text", "[clio: task_results]\nchild: done")]),
    ]


def _turn(arc: ARCMemory, turn: str, question: str, answer: str) -> None:
    arc.append_segment(SESSION, SCOPE, "user", {"text": question}, turn_id=turn)
    arc.append_segment(SESSION, SCOPE, "thought", {"text": answer}, turn_id=turn)


def _texts(arc: ARCMemory) -> list[str]:
    return [s.content["text"] for s in arc.render_working_set(SESSION, SCOPE)]


def test_a_rollback_removes_the_rolled_back_turns_from_the_agent_context(arc: ARCMemory) -> None:
    """Undo / rewind of the conversation: the agent's context follows the ledger."""
    from clio_agent.gact.context_rollback import roll_back_agent_context

    _turn(arc, "t1", "q1", "a1")
    _turn(arc, "t2", "q2", "a2")
    roll_back_agent_context(arc, SESSION, rolled_back={"t2"}, kept_user_turns=set())
    assert _texts(arc) == ["q1", "a1"]


def test_a_rewind_keeping_the_question_keeps_only_its_user_message(arc: ARCMemory) -> None:
    from clio_agent.gact.context_rollback import roll_back_agent_context

    _turn(arc, "t1", "q1", "a1")
    _turn(arc, "t2", "q2", "a2")
    roll_back_agent_context(arc, SESSION, rolled_back={"t2"}, kept_user_turns={"t2"})
    assert _texts(arc) == ["q1", "a1", "q2"]


def test_a_rollback_past_a_compaction_restores_what_it_replaced(arc: ARCMemory) -> None:
    """The compaction summary was written in a rolled-back turn: the context returns
    to what the model saw before it (the summarized turns come back, as they were)."""
    from clio_agent.gact.context_rollback import roll_back_agent_context

    _turn(arc, "t1", "q1", "a1")
    _turn(arc, "t2", "q2", "a2")
    ids = [s.id for s in arc.render_working_set(SESSION, SCOPE)]
    arc.summarize_segments(SESSION, SCOPE, ids, {"text": "summary"}, turn_id="t3")
    arc.append_segment(SESSION, SCOPE, "user", {"text": "q3"}, turn_id="t3")
    roll_back_agent_context(arc, SESSION, rolled_back={"t3"}, kept_user_turns=set())
    assert _texts(arc) == ["q1", "a1", "q2", "a2"]


def _ledger_row(row_id: str, role: str, text: str) -> Any:
    return types.SimpleNamespace(
        id=row_id, role=role, parts=[types.SimpleNamespace(type="text", text=text)]
    )


def test_a_scope_new_to_the_conversation_starts_from_its_earlier_turns(arc: ARCMemory) -> None:
    """An agent with nothing recorded yet (the user switched agents, or its store
    changed) joins the conversation: the transcript's earlier turns are carried
    over once, as the messages they were, and the agent is told they were."""
    ledger = [
        _ledger_row("u1", "user", "which station moved most?"),
        _ledger_row("a1", "assistant", "P595."),
        _ledger_row("u2", "user", "by how much?"),  # this turn's own message
    ]
    lm, engine = scripted_lm([Reply(text="About 20 cm.")])
    agent = ClioReAct("question -> answer", tools=[])
    with _plane(arc, messages={SESSION: ledger}), dspy.context(lm=lm):
        agent(question="by how much?")
        texts_after_first = [s.content.get("text") for s in arc.render_working_set(SESSION, SCOPE)]

    assert wire(engine.requests[0]) == [
        ("user", [("text", "which station moved most?")]),
        ("assistant", [("text", "P595.")]),
        (
            "user",
            [
                (
                    "text",
                    "[clio: earlier_turns]\nReconstructed 2 earlier context records from the saved transcript, "
                    "including recorded tool inputs and results. Provider continuation state is unavailable.",
                )
            ],
        ),
        ("user", [("text", "by how much?")]),
    ]
    assert texts_after_first.count("which station moved most?") == 1  # carried over once
