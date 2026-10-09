"""Tool timing survives live projections and reconnect transcript snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.protocol_v3 import event_to_v3
from clio_agent.gact.tool_observer import _make_tool_observer


@pytest.mark.parametrize("ok", [True, False])
def test_live_tool_events_include_execution_timestamps(ok: bool) -> None:
    """Old events use their recorded occurrence; newer ones retain observer timestamps."""
    start_event = Event(
        type="tool.call.started",
        session_id="s",
        payload={"call_id": "call", "tool": "probe"},
    )
    start_event.occurred_at = "2026-10-09T12:00:00Z"
    started = event_to_v3(start_event, workspace_id="w")
    completed = event_to_v3(
        Event(
            type="tool.call.completed",
            session_id="s",
            payload={
                "call_id": "call",
                "tool": "probe",
                "ok": ok,
                "duration_ms": 3_950,
                "completed_at": "2026-10-09T12:00:03.950Z",
            },
        ),
        workspace_id="w",
    )
    assert started["payload"]["started_at"] == "2026-10-09T12:00:00Z"
    assert completed["payload"]["completed_at"] == "2026-10-09T12:00:03.950Z"
    assert completed["payload"]["duration_ms"] == 3_950
    assert completed["payload"]["state"] == ("succeeded" if ok else "failed")


@pytest.mark.parametrize("error", [None, "converter failed"])
def test_real_observer_timing_survives_transcript_reload(tmp_path: Path, error: str | None) -> None:
    """Drive the real observer, ledger and v3 snapshot without a model invocation."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="Timing")
    observe = _make_tool_observer(app)
    observe("probe", {}, "started", None)
    observe("probe", {}, "completed", error, {"content": [{"type": "text", "text": "ready"}]})
    snapshot = (
        TestClient(app)
        .get(f"/v1/sessions/{session.id}/messages", headers={"X-GACT-Version": "0.3"})
        .json()
    )
    assert len(snapshot["tools"]) == 1
    tool = snapshot["tools"][0]
    assert tool["started_at"]
    assert tool["completed_at"] >= tool["started_at"]
    assert tool["duration_ms"] >= 0
    assert tool["state"] == ("failed" if error else "succeeded")


def test_zero_duration_is_preserved_in_reloaded_tool(tmp_path: Path) -> None:
    """A real zero must not become an unavailable duration through truthiness."""
    from clio_agent.gact.types import Message

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="Zero")
    app.state.messages[session.id] = [
        Message(
            id="msg",
            session_id=session.id,
            role="assistant",
            created_at="2026-10-09T12:00:00Z",
            updated_at="2026-10-09T12:00:01Z",
            parts=[
                Part(id="c", type="tool_call", call_id="call", tool_name="cached"),
                Part(id="r", type="tool_result", call_id="call", tool_name="cached", duration_ms=0),
            ],
        ),
    ]
    assert (
        transcript_entities(app.state.messages.get(session.id, []), session.id)["tools"][0][
            "duration_ms"
        ]
        == 0
    )


def test_old_untimed_result_does_not_gain_a_zero_duration() -> None:
    """An omitted old measurement remains distinct from an explicitly recorded zero."""
    assert "duration_ms" not in Part(id="old", type="tool_result").to_wire()
    assert Part(id="new", type="tool_result", duration_ms=0).to_wire()["duration_ms"] == 0
