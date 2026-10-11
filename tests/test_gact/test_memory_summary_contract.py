"""Memory overviews are source excerpts, distinct from model-written compaction."""

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.app import build_app
from clio_agent.gact.memory_tools import build_memory_summary_tool
from clio_agent.gact.types import Message, Part


def _message(session_id: str, index: int, text: str, kind: str = "text") -> Message:
    """Create an identifiable retained message for the source projection tests."""
    return Message(
        id=f"message_{index}",
        session_id=session_id,
        role="assistant" if index % 2 else "user",
        created_at=f"2026-10-10T12:00:0{index}+00:00",
        updated_at=f"2026-10-10T12:00:0{index}+00:00",
        parts=[Part(id=f"part_{index}", type=kind, text=text)],
    )


def test_summary_without_a_model_returns_recent_source_excerpts(tmp_path: Path) -> None:
    """No model is available; the overview still preserves source IDs and limits."""
    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None, agent=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "Fictional station check"}).json()["id"]
    rows = [_message(sid, i, f"Recorded decision {i}") for i in range(7)]
    rows[3] = _message(sid, 3, "Long source " * 50)
    rows[4] = _message(sid, 4, "", "tool_call")
    rows[5] = _message(sid, 5, "Recorded qualification", "thinking")
    app.state.messages[sid] = rows

    response = client.post(f"/v1/sessions/{sid}/memory/tools/read-session-summary", json={})

    assert response.status_code == 200, response.text
    assert app.state.agent is None
    summary = response.json()["summary"]
    assert summary["title"] == "Fictional station check"
    assert summary["message_count"] == 7
    assert summary["visible_message_ids"] == [row.id for row in rows]
    excerpts = summary["recent_excerpts"]
    assert [row["message_id"] for row in excerpts] == [
        "message_2",
        "message_3",
        "message_5",
        "message_6",
    ]
    assert excerpts[0]["excerpt"] == rows[2].parts[0].text
    assert excerpts[0]["created_at"] == rows[2].created_at
    assert len(excerpts[1]["excerpt"]) <= 360
    assert excerpts[1]["excerpt"].endswith("...")
    assert excerpts[2]["excerpt"] == "Recorded qualification"
    assert summary["metadata"]["source"] == "gact_visible_transcript_summary"
    assert summary["metadata"]["raw_transcript_included"] is False


def test_agent_summary_tool_and_route_read_the_same_persisted_messages(tmp_path: Path) -> None:
    """A service reload and the actual callable tool use the retained source ledger."""
    sessions_path = tmp_path / "sessions.json"
    original = build_app(sessions_path=sessions_path, arc=None, agent=None)
    sid = original.state.sessions.create(title="Retained decision", workspace_id="ws_default").id
    original.state.message_store.replace_session(sid, [_message(sid, 0, "Fernhill: 8:15")])
    restored = build_app(sessions_path=sessions_path, arc=None, agent=None)
    client = TestClient(restored)
    route = client.post(f"/v1/sessions/{sid}/memory/tools/read-session-summary", json={})
    assert route.status_code == 200, route.text
    tokens = [context.set_app(restored), context.set_session_id(sid)]
    try:
        result = build_memory_summary_tool(SimpleNamespace(id="main")).func(target_session_id=sid)
    finally:
        for token in reversed(tokens):
            context.reset(token)

    assert result["summary"] == route.json()["summary"]
    assert result["summary"]["recent_excerpts"][0]["excerpt"] == "Fernhill: 8:15"
    assert result["metadata"]["policy_decision"] == "allow_same_session"
    assert restored.state.memory_tool_audit[-1]["tool_name"] == "memory_read_session_summary"


def test_empty_session_has_no_invented_summary(tmp_path: Path) -> None:
    """No messages produces an explicit empty excerpt list."""
    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None, agent=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "Not started"}).json()["id"]
    response = client.post(f"/v1/sessions/{sid}/memory/tools/read-session-summary", json={})
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    assert summary["message_count"] == 0
    assert summary["recent_excerpts"] == []
    assert summary["visible_message_ids"] == []


def test_context_frame_reports_partial_coverage_and_source_descriptions(tmp_path: Path) -> None:
    """Frame reads expose the 50-item boundary rather than suggesting complete source text."""
    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None, agent=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "Frame coverage"}).json()["id"]
    app.state.context_frames[sid] = [
        {
            "id": "frame_1",
            "session_id": sid,
            "items": [
                {"kind": "message", "source_id": f"message_{i}", "text": "source body"}
                for i in range(55)
            ],
        }
    ]
    response = client.post(
        f"/v1/sessions/{sid}/memory/tools/read-context-frame", json={"frame_id": "frame_1"}
    )
    assert response.status_code == 200, response.text
    frame = response.json()["frame"]
    assert frame["metadata"]["items_total"] == 55
    assert frame["metadata"]["items_returned"] == 50
    assert frame["items"][-1]["source_id"] == "message_49"
    assert all("text" not in item for item in frame["items"])
    assert frame["metadata"]["source"] == "gact_context_frame"
