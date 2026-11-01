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
from clio_agent.gact.agents.clio_react import ClioReAct
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
    ctx.set_primary_agent_id(SCOPE)
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


def test_no_tool_use_note_is_recorded(arc: ARCMemory) -> None:
    """The batching guidance rides the tool rules and the per-turn reminder of the text
    engine, not a recorded note, so the conversation carries none."""
    requests = _two_turns(arc)
    assert all("[clio: tool_use]" not in str(r.messages) for r in requests)


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


def queue_step_arrivals(app: Any, sid: str) -> tuple[str, str]:
    """Confirm real human and child-result mail without using the retired local inbox."""
    from clio_agent.arc.task_types import MailItem
    from clio_agent.gact.native_work import now
    from clio_agent.gact.task_publication import publish_task_notice
    from tests.test_gact.task_records import record_delegation_fixture

    history = app.state.task_history
    feedback_id = "step-feedback"
    with history.serialized():
        history.record_mail(
            MailItem(
                id=feedback_id,
                recipient_session=sid,
                sender_human=app.state.human_principal,
                kind="human",
                content=history.write_content(sid, {"text": "use 2024 only"}),
                created_at=now(),
            )
        )
        child = record_delegation_fixture(
            app, sid, handle="task_step_child", completed=True, result={"answer": "child: done"}
        )
        publish_task_notice(history, child)
    return feedback_id, "outcome:" + child.handle + ":" + sid


def test_steers_and_child_results_arrive_at_the_step_boundary(
    clio_core_plane: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual Core arrivals enter the next model input after the original tool result."""
    app, sid = clio_core_plane, ctx.active_session_id()
    monkeypatch.setattr(app.state.conversations, "execution_gate", lambda _sid: "test_direct_loop")
    identities: list[str] = []

    def search(q: str) -> str:
        identities.extend(queue_step_arrivals(app, sid))
        return f"hits for {q}"

    lm, engine = scripted_lm([calls(("search", {"q": "x"})), Reply(text="ok"), Reply(text="next")])
    agent = ClioReAct("question -> answer", tools=[dspy.Tool(search, name="search")])
    with dspy.context(lm=lm):
        agent(question="q")
        agent(question="next task")
    observed = wire(engine.requests[1])
    assert observed[-3] == ("tool", [("result", "call_0_0", "hits for x", False)])
    assert observed[-2][0] == "user" and observed[-2][1][0][1].endswith("use 2024 only")
    assert observed[-1][0] == "user" and "child: done" in observed[-1][1][0][1]
    assert "[clio: task_result]" in observed[-1][1][0][1]
    assert "use 2024 only" not in repr(engine.requests[0].messages)
    assert set(identities) <= app.state.task_history.acknowledged_notifications(sid)
    assert not set(identities) & {row.id for row in app.state.task_history.pending_mail(sid)}
    assert repr(engine.requests[2].messages).count("child: done") == 1


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
                    "[clio: context_recovery]\nRecovered this session's context from saved transcript; "
                    "provider continuation is unavailable. No operation was replayed.",
                )
            ],
        ),
        ("user", [("text", "by how much?")]),
    ]
    assert texts_after_first.count("which station moved most?") == 1  # carried over once


def test_message_receipt_projects_the_existing_confirmed_core_input(
    clio_core_plane: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.arc.task_types import MailItem
    from clio_agent.gact.native_work import now
    from clio_agent.gact.session_store import project_accepted_mail_messages
    from clio_agent.gact.types import Message, Part

    app, sid = clio_core_plane, ctx.active_session_id()
    monkeypatch.setattr(app.state.conversations, "execution_gate", lambda _sid: "test_direct_loop")
    history = app.state.task_history
    message = Message(
        id="feedback-receipt",
        session_id=sid,
        role="user",
        created_at=now(),
        updated_at=now(),
        parts=[Part(type="text", text="Use dataset B")],
    )
    item = history.record_mail(
        MailItem(
            id=message.id,
            recipient_session=sid,
            sender_human=app.state.human_principal,
            kind="human",
            created_at=now(),
            content=history.write_content(
                sid,
                {
                    "message_id": message.id,
                    "message": message.model_dump(mode="json"),
                    "text": "Use dataset B",
                },
            ),
        )
    )
    project_accepted_mail_messages(app, history, sid, (item,))
    client = TestClient(app)
    path = f"/v1/sessions/{sid}/messages/{message.id}"
    assert client.get(path).json()["metadata"]["recipient_input"] is None
    lm, engine = scripted_lm([Reply(text="Using B")])
    with dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[])(question="Continue")
    record = history.committed_notification_input(sid, message.id)
    assert record is not None and "Use dataset B" in repr(engine.requests[0].messages)
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["recipient_input"]["id"] == record.id
    assert response.json()["metadata"]["recipient_input"]["turn_id"] == record.turn_id
