"""A question or plan pause must retain the actual tool transcript on reload."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.paused_transcript import persist_paused_transcript
from clio_agent.gact.plan_mode import maybe_pause_for_plan_exit
from clio_agent.gact.transcript import EventBusTranscriptPublisher
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import Message, Part
from clio_agent.gact.user_question_pause import maybe_pause_for_user


@pytest.mark.parametrize("pause", ["question", "plan"])
def test_pause_persists_tool_parts_without_synthesizing_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause: str
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="pause", mode="plan")
    if pause == "plan":
        plan = tmp_path / "plan.md"
        plan.write_text("# Exact plan\nKeep the observed tool output.\n", encoding="utf-8")
        pending = {
            "plan_file": str(plan),
            "pending_plan_exit": {"plan_file": str(plan), "summary": "Review this plan"},
        }
    else:
        pending = {"pending_ask_user": {"question": "Which view?", "kind": "freeform"}}
    app.state.sessions.update(session.id, metadata_patch=pending)
    user = Message(
        id="user_pause",
        session_id=session.id,
        role="user",
        parts=[],
        created_at=session.created_at,
        updated_at=session.updated_at,
    )
    state = TurnState(
        app=app,
        sid=session.id,
        sess=session,
        bus=app.state.bus,
        user_msg=user,
        user_text="Show the tool result",
        turn_agent_id="main",
        turn_id=user.id,
        trace_id="trace_pause",
        retry_attempt_id="",
        native_images=[],
        context_frame={"id": "frame_pause"},
    )
    state.transcript = app.state.turn_transcripts.open_turn(
        session.id, user.id, EventBusTranscriptPublisher(app.state.bus, session.id)
    )
    state.transcript.append_text_delta("main", "reasoning", "Keep this exact thought.")
    state.transcript.append_part(
        Part(
            id="result_pause",
            type="tool_result",
            tool_name="load_skill",
            call_id="call_pause",
            content=[Part(id="raw_pause", type="text", text='{"skill":"unchanged"}')],
            presentation={
                "summary": "Loaded skill",
                "blocks": [
                    {
                        "id": "procedure",
                        "type": "markdown",
                        "text": "# Procedure\n" + "Step\n" * 900,
                    }
                ],
            },
        )
    )
    expected = [part.model_dump() for part in state.transcript.snapshot()]
    monkeypatch.setattr(
        "clio_agent.gact.user_question_pause._finalize_context_frame", lambda *a, **k: None
    )
    monkeypatch.setattr("clio_agent.gact.enrichment._finalize_context_frame", lambda *a, **k: None)
    updates: list[Any] = []
    if pause == "plan":
        assert maybe_pause_for_plan_exit(state)
    else:
        assert maybe_pause_for_user(
            state, SimpleNamespace(), update_retry_attempt=lambda *a, **k: updates.append(k)
        )

    restored = app.state.message_store.load_session(session.id) or []
    assert len(restored) == 1, "the paused assistant must survive loss of the live ledger"
    message = restored[0]
    assert message.role == "assistant"
    assert message.turn_id == user.id
    assert message.stop_reason == "waiting_user"
    # finalize adds sequence metadata, but never substitutes or drops observed content.
    assert [(p.id, p.type, p.text, p.content) for p in message.parts] == [
        (p["id"], p["type"], p["text"], [Part(**v) for v in p["content"]]) for p in expected
    ]
    assert app.state.sessions.get(session.id).status == "waiting_user"
    assert app.state.turn_transcripts.get(session.id) is None
    assert [m.id for m in app.state.messages[session.id]] == [message.id]
    assert message.parts[-1].presentation == expected[-1]["presentation"]
    assert persist_paused_transcript(state) == message.id
    assert len(app.state.message_store.load_session(session.id)) == 1
