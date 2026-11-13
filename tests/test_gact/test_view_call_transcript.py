"""A view's own call never lands inside a turn that happens to be open.

Found live (q-apps, session sess_221ad09e2e49): a surface's ``clio.mcp-app.v1`` opening
call ran while the agent was still answering, and its tool rows were appended to that
turn's parts, splitting the answer's text in the middle of a code span and recording the
view's call as the agent's. A view's call now writes its rows as its own message while
a turn is open, and keeps the ordinary no-turn path otherwise.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.tool_observer import _open_turn_transcript
from clio_agent.gact.view_call_transcript import VIEW_CALL_ORIGIN


class _Executor:
    """Fires the production tool observer and App observer, as the real executor does."""

    def __init__(self, app_ref: list[Any]) -> None:
        self.app_ref = app_ref
        self.definitions = {
            "clock_open_timeline": SimpleNamespace(
                meta={"ui": {"resourceUri": "ui://clock/view", "visibility": ["model", "app"]}}
            ),
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        app = self.app_ref[0]
        observer = app.state.pending_tool_observer
        observer(name, args, "started", None)
        result = {"content": [{"type": "text", "text": "opened"}], "structuredContent": {}}
        observer(name, args, "completed", None, result)
        app.state.pending_mcp_app_observer(name, args, self.definitions[name], result, "clock")
        return result


def _client(tmp_path: Path) -> tuple[TestClient, Any]:
    ref: list[Any] = []
    executor = _Executor(ref)
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=SimpleNamespace(_active_tool_executor=lambda: executor),
    )
    ref.append(app)
    return TestClient(app, base_url="http://127.0.0.1:8100"), app


def _open(client: TestClient, sid: str) -> dict[str, Any]:
    response = client.post(
        f"/v1/sessions/{sid}/mcp-apps/reopen",
        json={"call": {"server": "clock", "tool": "open_timeline"}, "save": False},
    )
    assert response.status_code == 200
    return response.json()


def _rows(parts: list[Any]) -> list[str]:
    return [part.type for part in parts if part.type in {"tool_call", "tool_result"}]


def test_a_view_call_during_an_open_turn_is_its_own_message(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        transcript = _open_turn_transcript(app, sid, "turn_streaming")
        before = len(app.state.messages.get(sid, []))
        try:
            assert "part" in _open(client, sid)
            assert _rows(transcript.snapshot()) == []  # the turn's parts are untouched
        finally:
            app.state.turn_transcripts.close(sid)
        added = app.state.messages.get(sid, [])[before:]
    assert len(added) == 1
    message = added[0]
    assert message.role == "assistant"
    assert message.metadata.get("origin") == VIEW_CALL_ORIGIN
    assert _rows(message.parts) == ["tool_call", "tool_result"]
    assert {part.tool_name for part in message.parts if part.tool_name} == {"clock_open_timeline"}


def test_a_view_call_with_no_turn_open_keeps_the_no_turn_path(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        before = len(app.state.messages.get(sid, []))
        assert "part" in _open(client, sid)
        added = app.state.messages.get(sid, [])[before:]
        live = list(app.state.live_assistant_parts.get(sid, []))
    assert added == []
    assert _rows(live) == ["tool_call", "tool_result"]
