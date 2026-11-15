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

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_observer_reasons import recorded_mcp_app_observer_skips
from clio_agent.gact.tool_observer import _open_turn_transcript
from clio_agent.gact.view_call_transcript import VIEW_CALL_ORIGIN
from clio_agent.tools.tool_observation import notify_tool_observer
from clio_agent.tools.view_only_results import VIEW_ONLY_CONTENT_NOT_STORED

CHUNK = "QUJD" * 50_000


class _Executor:
    """Fires the production tool observer and App observer, as the real executor does."""

    def __init__(self, app_ref: list[Any]) -> None:
        self.app_ref = app_ref
        self.definitions = {
            "clock_open_timeline": SimpleNamespace(
                meta={"ui": {"resourceUri": "ui://clock/view", "visibility": ["model", "app"]}}
            ),
            "clock_read_bytes": SimpleNamespace(meta={"ui": {"visibility": ["app"]}}),
            "clock_table": SimpleNamespace(meta={"ui": {"visibility": ["model", "app"]}}),
            "clock_poll": SimpleNamespace(
                meta={"ui": {"resourceUri": "ui://clock/view", "visibility": ["app"]}}
            ),
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        app = self.app_ref[0]
        observer = app.state.pending_tool_observer
        if name in {"clock_read_bytes", "clock_table"}:  # the executor's notification seam
            result = {"content": [{"type": "text", "text": CHUNK}], "structuredContent": None}
            if name == "clock_table":
                result["structuredContent"] = {"rows": [CHUNK, CHUNK, CHUNK]}
            notify_tool_observer(observer, name, args, "started", None)
            notify_tool_observer(observer, name, args, "completed", None, result)
            return result
        if name == "clock_poll":  # a view-only tool that names the view it feeds
            result = {"content": [{"type": "text", "text": "rev 2"}], "structuredContent": {}}
            notify_tool_observer(observer, name, args, "completed", None, result)
            app.state.pending_mcp_app_observer(name, args, self.definitions[name], result, "clock")
            return result
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


def test_a_call_only_the_view_sees_writes_no_row_and_keeps_a_record(tmp_path: Path) -> None:
    """MC-F3: the PDF viewer's 512 KB reads and 30 s polls were one stored row each."""
    client, app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        part = _open(client, sid)["part"]
        rows_before = _rows(app.state.live_assistant_parts.get(sid, []))
        messages_before = len(app.state.messages.get(sid, []))
        transcript = _open_turn_transcript(app, sid, "turn_streaming")
        turn_rows = _rows(transcript.snapshot())  # it adopts the no-turn rows of the open
        try:
            response = client.post(
                f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}/tools/call",
                params={"data_ref": part["data_ref"]},
                json={"name": "read_bytes", "arguments": {"offset": 0}},
            )
            assert _rows(transcript.snapshot()) == turn_rows
        finally:
            app.state.turn_transcripts.close(sid)
        rows_after = _rows(app.state.live_assistant_parts.get(sid, []))
        added = app.state.messages.get(sid, [])[messages_before:]
        ledger = [
            row
            for row in app.state.tool_call_ledger.get(sid, [])
            if row["name"] == "clock_read_bytes"
        ]
    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == CHUNK  # the view gets it whole
    assert rows_after == rows_before and added == []  # no transcript row, held or live
    (row,) = ledger  # the call is still recorded, compactly
    record = row["result"]["structuredContent"]["view_only_result"]
    assert record["reason"] == VIEW_ONLY_CONTENT_NOT_STORED
    assert record["tool"] == "clock_read_bytes" and record["bytes"] > len(CHUNK)
    assert CHUNK not in str(row)


def test_a_large_result_the_model_sees_is_a_row_with_its_head_and_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GG-F3: the row and the trace keep a bounded copy of any oversized result."""
    monkeypatch.setattr(
        "clio_agent.tools.servers.shell_spill_store.spill_directory",
        lambda _root, session_id=None: tmp_path / "tool-output",
    )
    client, app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        part = _open(client, sid)["part"]
        response = client.post(
            f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}/tools/call",
            params={"data_ref": part["data_ref"]},
            json={"name": "table", "arguments": {}},
        )
        rows = [
            p for p in app.state.live_assistant_parts.get(sid, []) if p.tool_name == "clock_table"
        ]
        ledger = [r for r in app.state.tool_call_ledger.get(sid, []) if r["name"] == "clock_table"]
    assert response.status_code == 200
    assert response.json()["structuredContent"]["rows"][0] == CHUNK  # the view's copy is whole
    result = next(p for p in rows if p.type == "tool_result")
    assert result.structured_content is None
    assert "[clio: tool_result_stored_in_file]" in result.content[0].text
    assert CHUNK not in result.model_dump_json() and CHUNK not in str(ledger)
    (stored,) = list((tmp_path / "tool-output").iterdir())
    assert CHUNK in stored.read_text()


def test_a_view_only_call_naming_its_view_opens_no_new_view(tmp_path: Path) -> None:
    """A view's poll of a view-only tool that names its ``ui://`` resource feeds the view
    that made it: it registers no app and writes no view card (KiCad ledger, KQ10)."""
    client, app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        part = _open(client, sid)["part"]

        def views() -> list[Any]:
            live = app.state.live_assistant_parts.get(sid, [])
            stored = [p for m in app.state.messages.get(sid, []) for p in m.parts]
            return [p for p in [*live, *stored] if p.type == "mcp_app"]

        before = len(views())
        for _ in range(3):
            response = client.post(
                f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}/tools/call",
                params={"data_ref": part["data_ref"]},
                json={"name": "poll", "arguments": {"since": 1}},
            )
            assert response.status_code == 200
            assert response.json()["content"][0]["text"] == "rev 2"
        after = len(views())
    assert after == before
    skipped = [
        row
        for row in recorded_mcp_app_observer_skips()
        if row["reason"] == "mcp_app_skipped_view_only_call" and row.get("tool") == "clock_poll"
    ]
    assert len(skipped) >= 3
