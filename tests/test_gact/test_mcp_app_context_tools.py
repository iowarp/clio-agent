"""A view's context reaches the agent from clio-core, and its own tools reach the view.

Decisions MA8 to MA14 of ``docs/design/sdk/mcp-apps-surfaces-plan.md``; the wire
shapes are the "App context and app tools" section of ``mcp-apps-contracts.md``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_context import (
    APP_CONTEXT_KEY,
    app_context_injection,
    stored_app_context,
)
from clio_agent.gact.mcp_app_tool_presentation import present, starting
from clio_agent.gact.mcp_app_tools import (
    APP_TOOL_CALL_EVENT,
    MAX_APP_TOOLS,
    AppToolTable,
    app_tool_table,
    attach_app_tools,
    call_app_tool,
    place_app_tools,
)
from clio_agent.gact.mcp_apps import MCPAppRecord
from clio_agent.gact.search_tools import search_result
from clio_agent.gact.tool_catalog import ToolCatalog
from clio_agent.gact.working_state import WORKING_STATE_SOURCES

_HIGHLIGHT = {
    "name": "set_highlight",
    "description": "Highlight one reading.",
    "inputSchema": {
        "type": "object",
        "properties": {"reading": {"type": "string"}},
        "required": ["reading"],
    },
}
_COLOR = {"name": "set_color", "inputSchema": {"type": "object", "properties": {}}}
_NOTE = {"name": "add_note", "inputSchema": {"type": "object", "properties": {}}}


def _app(tmp_path: Path) -> Any:
    return build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())


def _record(app: Any, sid: str) -> MCPAppRecord:
    return app.state.mcp_app_registry.register(
        session_id=sid,
        source_namespace="board",
        tool_name="board_open_board",
        resource_uri="ui://board/view",
        tool_input={"topic": "Cooling loop"},
        tool_result={"content": [{"type": "text", "text": "opened"}]},
    )


def _prefix(sid: str, record: MCPAppRecord) -> tuple[str, dict[str, str]]:
    return f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}", {"data_ref": record.data_ref}


def _bound(app: Any, sid: str) -> list[Any]:
    tokens = [_ctx.set_app(app), _ctx.set_session_id(sid)]
    return tokens


def _unbind(tokens: list[Any]) -> None:
    for token in reversed(tokens):
        _ctx.reset(token)


def test_reported_context_lives_in_the_session_record_in_clio_core(tmp_path: Path) -> None:
    """AQ19: the context is read back from the session's clio-core record."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        record = _record(app, sid)
        prefix, cap = _prefix(sid, record)
        report = {"content": [{"type": "text", "text": "Pump B selected"}]}
        assert client.put(f"{prefix}/model-context", params=cap, json=report).status_code == 200

        assert not hasattr(record, "model_context")
        app.state.sessions.flush()
        stored = json.loads(app.state.sessions._records.body(sid))
        entry = stored["metadata"][APP_CONTEXT_KEY][record.app_instance_id]
        assert entry["context"] == report
        assert entry["server"] == "board" and entry["tool"] == "board_open_board"
        assert stored_app_context(app, sid, record.app_instance_id)["bytes"] == entry["bytes"]

        too_big = {"text": "x" * (128 * 1024)}
        assert client.put(f"{prefix}/model-context", params=cap, json=too_big).status_code == 413
        assert stored_app_context(app, sid, record.app_instance_id)["context"] == report


def test_a_turn_carries_one_block_per_live_app_and_a_closed_app_none(tmp_path: Path) -> None:
    """AQ18 / MA8: the working state names the server, the opening tool and the context."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        quiet = _record(app, sid)  # reports nothing and offers nothing: no block
        record = _record(app, sid)
        prefix, cap = _prefix(sid, record)
        report = {"content": [{"type": "text", "text": "The person selected Pump B"}]}
        client.put(f"{prefix}/model-context", params=cap, json=report)

        block = app_context_injection(app, sid)
        assert "board view open (opened by board_open_board)" in block
        assert "The person selected Pump B" in block
        assert block.count("The person has the") == 1
        assert "mcp_app_context" in WORKING_STATE_SOURCES
        assert quiet.app_instance_id not in block

        assert client.delete(prefix, params=cap).status_code == 204
        assert app_context_injection(app, sid) == ""
        assert stored_app_context(app, sid, record.app_instance_id) is None


def test_view_tools_are_offered_with_limits_and_reasons(tmp_path: Path) -> None:
    """MA11 / MA14: names, skipped rows, and a list over a limit offers nothing."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        record = _record(app, sid)
        prefix, cap = _prefix(sid, record)
        bad_name = {"name": "has space"}
        body = client.put(
            f"{prefix}/app-tools", params=cap, json={"tools": [_HIGHLIGHT, bad_name]}
        ).json()
        assert body["offered"] == ["app__board__set_highlight"]
        assert body["skipped"] == [{"name": "has space", "reason": "mcp_app_tool_name_invalid"}]

        many = [{"name": f"t{i}"} for i in range(MAX_APP_TOOLS + 1)]
        over = client.put(f"{prefix}/app-tools", params=cap, json={"tools": many}).json()
        assert over["offered"] == [] and over["reason"] == "mcp_app_tools_over_limit"
        assert app_tool_table(app).offered(sid) == {}

        huge = {"name": "big", "inputSchema": {"description": "x" * (64 * 1024)}}
        over = client.put(f"{prefix}/app-tools", params=cap, json={"tools": [huge]}).json()
        assert over["reason"] == "mcp_app_tool_schema_too_large"

        bad = client.put(f"{prefix}/app-tools", params={"data_ref": "wrong"}, json={"tools": []})
        assert bad.status_code == 404


def test_a_late_withdrawal_never_clears_a_newer_mount() -> None:
    table = AppToolTable()
    record = SimpleNamespace(
        session_id="s", app_instance_id="a", source_namespace="board", tool_name="open"
    )
    table.set_tools(record, [_HIGHLIGHT], holder="tab")
    table.set_tools(record, [], holder="card")  # the card's frame unloads after the tab mounted
    assert list(table.offered("s")) == ["app__board__set_highlight"]
    table.set_tools(record, [], holder="tab")
    assert table.offered("s") == {}


def test_a_call_reaches_the_view_and_returns_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MA13: the event, the posted result, and the text the agent gets."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        record = _record(app, sid)
        prefix, cap = _prefix(sid, record)
        client.put(f"{prefix}/app-tools", params=cap, json={"tools": [_HIGHLIGHT]})
        published: list[Any] = []
        seen = threading.Event()
        original = app.state.bus.publish

        def publish(event: Any) -> None:
            if event.type == APP_TOOL_CALL_EVENT:
                published.append(event)
                seen.set()
            original(event)

        monkeypatch.setattr(app.state.bus, "publish", publish)
        answers: list[str] = []

        def agent_call() -> None:
            tokens = _bound(app, sid)
            try:
                answers.append(call_app_tool("app__board__set_highlight", {"reading": "outlet"}))
            finally:
                _unbind(tokens)

        worker = threading.Thread(target=agent_call)
        worker.start()
        assert seen.wait(30)
        payload = published[0].payload
        assert payload["name"] == "set_highlight"
        assert payload["arguments"] == {"reading": "outlet"}
        assert published[0].transient is True
        result = {"content": [{"type": "text", "text": "Highlighted Outlet temperature."}]}
        posted = client.post(
            f"{prefix}/app-tool-results/{payload['call_id']}", params=cap, json={"result": result}
        )
        assert posted.status_code == 200
        worker.join(30)
        assert answers == ["Highlighted Outlet temperature."]
        again = client.post(
            f"{prefix}/app-tool-results/{payload['call_id']}", params=cap, json={"result": result}
        )
        assert again.status_code == 404


def test_a_call_to_a_closed_view_returns_at_once_naming_its_opening_tool(
    tmp_path: Path,
) -> None:
    """AQ21: no surface holds the app, so nothing waits."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        record = _record(app, sid)
        prefix, cap = _prefix(sid, record)
        client.put(f"{prefix}/app-tools", params=cap, json={"tools": [_HIGHLIGHT]})
        client.put(f"{prefix}/app-tools", params=cap, json={"tools": []})  # the frame unloaded
        tokens = _bound(app, sid)
        try:
            text = call_app_tool("app__board__set_highlight", {"reading": "inlet"})
        finally:
            _unbind(tokens)
        assert "the board view is not open" in text
        assert "board_open_board" in text


def _program(tools: dict[str, Any]) -> Any:
    program = SimpleNamespace(tools=tools, _clio_unlisted=frozenset({"search_tools"}))
    program._clio_deferred_catalog = ToolCatalog()
    program._clio_tool_refresh = None
    return program


def test_view_tools_join_and_leave_the_deferred_catalog(tmp_path: Path) -> None:
    """MA12 / AQ22: offered as deferred tools of the server; a changed list follows."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        record = _record(app, sid)
        prefix, cap = _prefix(sid, record)
        client.put(f"{prefix}/app-tools", params=cap, json={"tools": [_HIGHLIGHT, _COLOR]})
        program = _program({"search_tools": object()})
        tokens = _bound(app, sid)
        try:
            added = attach_app_tools(program)
            assert added == ["app__board__set_color", "app__board__set_highlight"]
            catalog = program._clio_deferred_catalog
            assert catalog.by_owner()["board"]
            assert "app__board__set_highlight" in program._clio_unlisted
            assert "search_tools" not in program._clio_unlisted
            text, found = search_result(catalog, "highlight a reading")
            assert found["tools"][0]["name"] == "app__board__set_highlight"
            assert '"reading"' in text

            client.put(f"{prefix}/app-tools", params=cap, json={"tools": [_HIGHLIGHT, _NOTE]})
            refresher = __import__(
                "clio_agent.gact.mcp_app_tools", fromlist=["AppToolRefresher"]
            ).AppToolRefresher()
            assert refresher(program.tools) is True
            assert place_app_tools(program) == ["app__board__add_note"]
            names = set(catalog.names())
            assert "app__board__add_note" in names
            assert "app__board__set_color" not in names
            assert "app__board__set_color" in program.tools  # answers a late call
        finally:
            _unbind(tokens)


def test_presentation_names_the_view_tool_and_its_outcome() -> None:
    row = present(
        {"reading": "outlet"},
        "Highlighted.",
        {
            "server": "board",
            "tool": "set_highlight",
            "outcome": "returned",
            "text": "Highlighted Outlet temperature.",
            "arguments": {"reading": "outlet"},
        },
    )
    assert row["action"] == "Set highlight"
    assert row["blocks"][0]["text"] == "board view"
    assert {"id": "sent", "type": "text", "label": "Sent", "text": "reading: outlet"} in row[
        "blocks"
    ]
    assert row["blocks"][-1]["text"] == "Highlighted Outlet temperature."
    assert "status" not in row

    closed = present(
        {},
        "",
        {
            "server": "board",
            "tool": "set_highlight",
            "outcome": "not_open",
            "text": "app__board__set_highlight did not run: the board view is not open.",
        },
    )
    assert closed["status"] == "failed"
    assert closed["summary"] == "The view is not open"
    running = starting("board", "set_highlight")({"reading": "inlet"})
    assert running["summary"] == "Waiting for the view"


def test_a_call_after_reopening_waits_for_the_new_view_to_list_its_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view was closed and opened again: the call waits for the new view's first list
    and runs there; a new view without the tool says it no longer offers it."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "board"}).json()["id"]
        old = _record(app, sid)
        prefix, cap = _prefix(sid, old)
        client.put(f"{prefix}/app-tools", params=cap, json={"tools": [_HIGHLIGHT]})
        assert client.delete(prefix, params=cap).status_code == 204
        new = _record(app, sid)
        new_prefix, new_cap = _prefix(sid, new)
        tokens = _bound(app, sid)
        try:
            assert "still loading for: board" in __import__(
                "clio_agent.gact.mcp_app_tools", fromlist=["loading_note"]
            ).loading_note(app, sid)
        finally:
            _unbind(tokens)

        calls: list[Any] = []
        seen = threading.Event()
        original = app.state.bus.publish

        def publish(event: Any) -> None:
            if event.type == APP_TOOL_CALL_EVENT:
                calls.append(event)
                seen.set()
            original(event)

        monkeypatch.setattr(app.state.bus, "publish", publish)
        answers: list[str] = []

        def agent_call() -> None:
            tokens = _bound(app, sid)
            try:
                answers.append(call_app_tool("app__board__set_highlight", {"reading": "inlet"}))
            finally:
                _unbind(tokens)

        worker = threading.Thread(target=agent_call)
        worker.start()
        worker.join(1.0)
        assert worker.is_alive() and not calls  # waiting for the new view, not answered
        client.put(f"{new_prefix}/app-tools", params=new_cap, json={"tools": [_HIGHLIGHT]})
        assert seen.wait(30)
        assert calls[0].payload["app_instance_id"] == new.app_instance_id
        result = {"content": [{"type": "text", "text": "Highlighted Inlet temperature."}]}
        client.post(
            f"{new_prefix}/app-tool-results/{calls[0].payload['call_id']}",
            params=new_cap,
            json={"result": result},
        )
        worker.join(30)
        assert answers == ["Highlighted Inlet temperature."]

        # Closed again, reopened by a view that lists no such tool.
        assert client.delete(new_prefix, params=new_cap).status_code == 204
        third = _record(app, sid)
        third_prefix, third_cap = _prefix(sid, third)
        answers.clear()
        worker = threading.Thread(target=agent_call)
        worker.start()
        worker.join(1.0)
        client.put(f"{third_prefix}/app-tools", params=third_cap, json={"tools": [_NOTE]})
        worker.join(30)
        assert answers and "no longer offers set_highlight" in answers[0]
