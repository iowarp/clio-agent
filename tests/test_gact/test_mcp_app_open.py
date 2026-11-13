"""Opening an MCP App from a call, for an A2UI surface (MA20) or a reopen (MA19)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_calls import REOPEN_REASONS, SESSION_KEY
from clio_agent.gact.mcp_apps import MCPAppRegistry


class _Executor:
    """A tool executor whose calls fire the production App observer, as the real one does."""

    def __init__(self, app_ref: list[Any]) -> None:
        self.app_ref = app_ref
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions: list[str] = []
        self.fail: Exception | None = None
        self.error_result = False
        self.definitions = {
            "clock_open_timeline": SimpleNamespace(
                meta={"ui": {"resourceUri": "ui://clock/view", "visibility": ["model", "app"]}}
            ),
            "clock_set_time": SimpleNamespace(meta={"ui": {"visibility": ["model", "app"]}}),
            "clock_model_only": SimpleNamespace(meta={"ui": {"visibility": ["model"]}}),
            "clock_plain": SimpleNamespace(meta={}),
            "other_open": SimpleNamespace(meta={"ui": {"resourceUri": "ui://other/view"}}),
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        from clio_agent.gact import context as gact_context  # noqa: PLC0415

        # A call made for a view belongs to the session as a turn's call does.
        self.sessions.append(gact_context.active_session_id())
        if self.fail is not None:
            raise self.fail
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": "bad time" if self.error_result else "opened"}],
            "structuredContent": {"time": args.get("time", 0)},
        }
        if self.error_result:
            result["isError"] = True
        app = self.app_ref[0]
        app.state.pending_mcp_app_observer(
            name, args, self.definitions[name], result, name.split("_", 1)[0]
        )
        return result


def _client(tmp_path: Path) -> tuple[TestClient, _Executor, Any]:
    ref: list[Any] = []
    executor = _Executor(ref)
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=SimpleNamespace(_active_tool_executor=lambda: executor),
    )
    ref.append(app)
    return TestClient(app, base_url="http://127.0.0.1:8100"), executor, app


def test_open_admits_the_view_without_adding_it_to_the_transcript(tmp_path: Path) -> None:
    client, executor, app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        response = client.post(
            f"/v1/sessions/{sid}/mcp-apps/open",
            json={"server": "clock", "tool": "open_timeline", "arguments": {"time": 3}},
        )
        assert response.status_code == 200
        body = response.json()
        part = body["part"]
        registry = app.state.mcp_app_registry
        assert isinstance(registry, MCPAppRegistry)
        record = registry.get(sid, part["app_instance_id"], part["data_ref"])
        assert part["type"] == "mcp_app"
        assert part["resource_uri"] == "ui://clock/view"
        assert part["source_server"] == "clock"
        assert part["tool_name"] == "clock_open_timeline"
        assert part["display_mode"] == "inline"
        assert body["saved_call"] == {
            "server": "clock",
            "tool": "open_timeline",
            "arguments": {"time": 3},
        }
        assert record.tool_input == {"time": 3}
        # The view tool is model-and-app; set_time is app-callable; model_only is not.
        assert body["server_tools"] == ["open_timeline", "plain", "set_time"]
        assert executor.calls == [("clock_open_timeline", {"time": 3})]
        assert executor.sessions == [sid]
        assert not getattr(app.state, "live_assistant_parts", {}).get(sid)
        # The surface holds the call; it is not saved on the session (MA18 is for transcript views).
        assert SESSION_KEY not in (app.state.sessions.get(sid).metadata or {})


def _open(client: TestClient, sid: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/v1/sessions/{sid}/mcp-apps/open", json=body)
    assert response.status_code == 200
    return response.json()


def test_every_failure_is_a_typed_reason_with_the_saved_call(tmp_path: Path) -> None:
    client, executor, _app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]

        missing = _open(client, sid, server="clock", tool="nope", arguments={})
        assert missing["error"]["reason"] == "tool_unavailable"
        assert missing["saved_call"] == {"server": "clock", "tool": "nope", "arguments": {}}

        no_view = _open(client, sid, server="clock", tool="set_time")
        assert no_view["error"]["reason"] == "tool_has_no_view"

        executor.error_result = True
        errored = _open(client, sid, server="clock", tool="open_timeline", arguments={"time": -1})
        assert errored["error"] == {"reason": "tool_error", "detail": "bad time"}

        executor.error_result = False
        executor.fail = PermissionError("tool call denied by permission gate")
        denied = _open(client, sid, server="clock", tool="open_timeline")
        assert denied["error"]["reason"] == "permission_denied"
        assert "denied by permission gate" in denied["error"]["detail"]

        for outcome in (missing, no_view, errored, denied):
            assert outcome["error"]["reason"] in REOPEN_REASONS


def test_open_validates_the_request_and_the_session(tmp_path: Path) -> None:
    client, _executor, _app = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        assert (
            client.post(f"/v1/sessions/{sid}/mcp-apps/open", json={"server": "clock"}).status_code
            == 422
        )
        assert (
            client.post(
                f"/v1/sessions/{sid}/mcp-apps/open",
                json={"server": "clock", "tool": "open_timeline", "arguments": [1]},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/sessions/sess_missing/mcp-apps/open",
                json={"server": "clock", "tool": "open_timeline"},
            ).status_code
            == 404
        )


def test_agent_calls_still_add_the_view_to_the_transcript(tmp_path: Path) -> None:
    """The capture applies only to the opening call; the agent's own calls are unchanged."""

    client, executor, app = _client(tmp_path)
    from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context

    with client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        with _gact_app_context(app), _tool_session_context(sid):
            executor.call_tool_result("clock_open_timeline", {"time": 1})
        parts = app.state.live_assistant_parts[sid]
        assert parts[-1].to_wire()["type"] == "mcp_app"
        assert SESSION_KEY in (app.state.sessions.get(sid).metadata or {})
