"""A restart's recovery names the turn it continues, so that reply reads as resumed.

The interrupted reply offered Retry although the restart had already resumed its work
in place, so Retry asked again (continuity grid CC-O1). The recovery input now carries
``resumes_turn`` (the interrupted turn: the latest confirmed input's iteration), the
recovery turn's input message carries it into its reply's metadata, and a client hides
Retry on the turn a later reply resumes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import AssignmentDetails, ContextBoundary, RecipientInput
from clio_agent.gact.native_work import now
from clio_agent.gact.question_events import QuestionEvents
from clio_agent.gact.restart_interruptions import RESUMES_TURN, resumed_turn_metadata
from tests.test_arc.test_conversation_owner_contracts import create_environment


@pytest.mark.asyncio
async def test_the_recovery_input_names_the_interrupted_turn(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """SABOTAGE: drop ``resumes_turn`` from ``conversation_recovery.recover`` and the
    recovery content no longer names ``msg_user_cut``."""
    app, owner, root = await create_environment(arc, tmp_path)
    history, sid = owner.history, root.owner.session_id

    def interrupted() -> None:
        arc.append_segment(sid, "main", "thought", {"text": "Retained checkpoint"})
        boundary = ContextBoundary(session_id=sid, scope="main", logical_time=0)
        details = history.write_content(
            sid, AssignmentDetails(context=boundary).model_dump(mode="json")
        )
        history.record_input(
            RecipientInput(
                id="input-cut",
                session_id=sid,
                assignment_id=root.handle,
                iteration_id="msg_user_cut:span:0",
                turn_id="msg_user_cut",
                boundary=boundary,
                notification_ids=("msg_user_cut",),
                content=(history.write_content(sid, {"text": "Write the story"}),),
                committed_at=now(),
            )
        )
        history.record_snapshot(
            root.model_copy(update={"activity": "busy", "details": details}),
            observation_id="interrupted-model-segment",
        )

    await history.io.run(interrupted)
    app.state.question_events = QuestionEvents(app)
    owner.runner.close_admission()
    owner.start()
    owner._restoring = True
    try:
        await owner.restore()
        pending = await history.io.run(history.pending_mail, sid)
        recovery = [item for item in pending if item.id.startswith("recovery:" + root.handle)]
        assert len(recovery) == 1
        content = await history.io.run(history.read_content, recovery[0].content)
        assert content[RESUMES_TURN] == "msg_user_cut"
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


def test_the_recovery_turn_carries_the_link_to_its_reply() -> None:
    assert resumed_turn_metadata({RESUMES_TURN: "msg_user_cut", "other": 1}) == {
        RESUMES_TURN: "msg_user_cut"
    }
    assert resumed_turn_metadata({RESUMES_TURN: ""}) == {}
    assert resumed_turn_metadata(None) == {}


def test_the_link_reaches_the_client_and_nothing_else_does() -> None:
    from clio_agent.gact.protocol.v3.message import message_to_v3
    from clio_agent.gact.types import Message, Part

    projected = message_to_v3(
        Message(
            id="msg_asst_resumed",
            turn_id="turn_recovery",
            session_id="sess_resume",
            role="assistant",
            created_at="2026-11-16T00:28:12+00:00",
            updated_at="2026-11-16T00:29:59+00:00",
            parts=[Part(id="answer", type="text", text="Running it again.")],
            metadata={RESUMES_TURN: "msg_user_cut", "tools_called": [{"name": "shell_bash"}]},
        )
    )
    assert projected["metadata"] == {RESUMES_TURN: "msg_user_cut"}


def test_the_turn_that_takes_in_the_recovery_carries_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery need not trigger the turn (a task notice can): committing it does."""
    from types import SimpleNamespace

    from clio_agent.gact import context as gact_context
    from clio_agent.gact.restart_interruptions import note_resumed_turn, resumed_by_turn

    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    contents = {"recovery": {RESUMES_TURN: "msg_user_cut"}, "notice": {"text": "done"}}
    history = SimpleNamespace(read_content=lambda ref: contents[ref])
    items = [
        SimpleNamespace(kind="task_result", content="notice"),
        SimpleNamespace(kind="control", content="recovery"),
    ]
    note_resumed_turn(history, items, "turn_resumed")
    assert resumed_by_turn("turn_other") == {}
    assert resumed_by_turn("turn_resumed") == {RESUMES_TURN: "msg_user_cut"}
    assert resumed_by_turn("turn_resumed") == {}  # read once, at finalize
