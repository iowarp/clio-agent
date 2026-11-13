"""Saved opening calls and reopening for MCP Apps (plan MA18, MA19).

The fake executor below stands in for ``SyncMCPToolExecutor``: its
``call_tool_result`` invokes the installed MCP App observer exactly where
``tools/execution.py::_call_tool_inner`` does after a successful call, so the
save and the replay capture run through the production observer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import mcp_app_calls
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_calls import (
    SESSION_KEY,
    saved_call,
    saved_calls,
    set_saved_display_mode,
)
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context

_VIEW = "ui://boards/view"


class _Executor:
    """A gated executor fixture whose successful calls reach the App observer."""

    def __init__(self) -> None:
        self.app: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: dict[str, Any] | None = None
        self.raise_exc: BaseException | None = None
        self.definitions: dict[str, Any] = {
            "boards_open_board": SimpleNamespace(
                title="Open a board",
                meta={"ui": {"resourceUri": _VIEW, "visibility": ["model", "app"]}},
            ),
            "boards_plain": SimpleNamespace(meta={}),
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(args)))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.fail_with is not None:
            return self.fail_with
        result = {
            "content": [{"type": "text", "text": f"board {args.get('title')}"}],
            "structuredContent": {"title": args.get("title"), "call": len(self.calls)},
            "_meta": {"fixture.private": {"secret": "private-result-only"}},
        }
        self.app.state.pending_mcp_app_observer(
            name, args, self.definitions[name], result, name.partition("_")[0]
        )
        return result

    def read_resource(self, namespace: str | None, uri: str) -> list[dict[str, Any]]:
        return [{"uri": uri, "mimeType": "text/html;profile=mcp-app", "text": "<p>board</p>"}]


@pytest.fixture
def stack(tmp_path: Path):
    executor = _Executor()
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=SimpleNamespace(_active_tool_executor=lambda: executor),
    )
    executor.app = app
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "Boards"}).json()["id"]
        yield SimpleNamespace(app=app, client=client, executor=executor, sid=sid)


def _open(stack: Any, args: dict[str, Any] | None = None) -> str:
    """Open a view as the agent's tool call would, returning its app instance id."""

    with _gact_app_context(stack.app), _tool_session_context(stack.sid):
        stack.executor.call_tool_result("boards_open_board", args or {"title": "Q3", "count": 4})
    records = stack.app.state.mcp_app_registry.records_for_session(stack.sid)
    return records[-1].app_instance_id


def _reopen(stack: Any, app_instance_id: str) -> dict[str, Any]:
    response = stack.client.post(
        f"/v1/sessions/{stack.sid}/mcp-apps/reopen", json={"app_instance_id": app_instance_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_opening_call_is_saved_without_private_result(stack: Any) -> None:
    app_id = _open(stack)

    entry = saved_call(stack.app, stack.sid, app_id)
    assert entry is not None
    assert {k: entry[k] for k in ("server", "tool", "arguments", "display_mode")} == {
        "server": "boards",
        "tool": "open_board",
        "arguments": {"title": "Q3", "count": 4},
        "display_mode": "inline",
    }
    assert entry["resource_uri"] == _VIEW
    assert entry["label"] == "Open a board"
    # Stored on the session record (clio-core), and the client sees it there.
    wire = stack.client.get(f"/v1/sessions/{stack.sid}").json()
    assert wire["metadata"][SESSION_KEY][app_id]["tool"] == "open_board"
    assert "private-result-only" not in json.dumps(wire)
    assert "<p>board</p>" not in json.dumps(wire)


def test_reopen_replays_saved_call_and_returns_fresh_part(stack: Any) -> None:
    app_id = _open(stack)
    parts_before = list(stack.app.state.live_assistant_parts.get(stack.sid, []))
    set_saved_display_mode(stack.app, stack.sid, app_id, "fullscreen")

    body = _reopen(stack, app_id)

    assert stack.executor.calls[-1] == ("boards_open_board", {"title": "Q3", "count": 4})
    part = body["part"]
    assert part["type"] == "mcp_app"
    assert part["app_instance_id"] != app_id
    assert part["resource_uri"] == _VIEW
    assert part["source_server"] == "boards"
    assert part["display_mode"] == "fullscreen"
    record = stack.app.state.mcp_app_registry.get(
        stack.sid, part["app_instance_id"], part["data_ref"]
    )
    assert record.tool_input == {"title": "Q3", "count": 4}
    assert record.pinned  # reopened into its tab, so pinned at once (MA6)
    # The card that asked mounts the part; no second card enters the transcript.
    assert list(stack.app.state.live_assistant_parts.get(stack.sid, [])) == parts_before
    # The reopened view has its own saved call, so a later reopen (tab restore) works.
    assert body["saved_call"]["reopened_from"] == app_id
    assert body["saved_call"]["display_mode"] == "fullscreen"
    assert saved_call(stack.app, stack.sid, app_id) is not None


def test_reopen_after_record_is_gone(stack: Any) -> None:
    """A CLIO restart empties the in-memory registry; the saved call still reopens."""

    app_id = _open(stack)
    stack.app.state.mcp_app_registry.drop_session(stack.sid)

    body = _reopen(stack, app_id)

    assert "part" in body
    assert len(stack.app.state.mcp_app_registry.records_for_session(stack.sid)) == 1


def test_failed_replay_returns_reason_and_saved_call(stack: Any) -> None:
    app_id = _open(stack)
    stack.executor.fail_with = {
        "content": [{"type": "text", "text": "the board service is down"}],
        "isError": True,
    }

    body = _reopen(stack, app_id)

    assert body["error"] == {"reason": "tool_error", "detail": "the board service is down"}
    assert body["saved_call"]["arguments"] == {"title": "Q3", "count": 4}

    stack.executor.fail_with = None
    assert "part" in _reopen(stack, app_id)  # Retry


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (PermissionError("tool call 'boards_open_board' denied"), "permission_denied"),
        (RuntimeError("transport closed"), "call_failed"),
        (ValueError("bad"), "call_failed"),
    ],
)
def test_call_exceptions_are_typed(stack: Any, exc: BaseException, reason: str) -> None:
    app_id = _open(stack)
    stack.executor.raise_exc = exc

    body = _reopen(stack, app_id)

    assert body["error"]["reason"] == reason
    assert body["error"]["detail"]
    assert body["saved_call"]["tool"] == "open_board"


def test_tool_gone_or_without_view(stack: Any) -> None:
    app_id = _open(stack)
    definition = stack.executor.definitions.pop("boards_open_board")
    assert _reopen(stack, app_id)["error"]["reason"] == "tool_unavailable"

    stack.executor.definitions["boards_open_board"] = SimpleNamespace(meta={})
    assert _reopen(stack, app_id)["error"]["reason"] == "tool_has_no_view"
    stack.executor.definitions["boards_open_board"] = definition


def test_no_saved_call_and_unknown_session(stack: Any) -> None:
    body = _reopen(stack, "never-opened")
    assert body == {
        "error": {
            "reason": "no_saved_call",
            "detail": mcp_app_calls.REOPEN_REASONS["no_saved_call"],
        },
        "saved_call": None,
    }
    missing = stack.client.post(
        "/v1/sessions/sess_missing/mcp-apps/reopen", json={"app_instance_id": "x"}
    )
    assert missing.status_code == 404
    empty = stack.client.post(f"/v1/sessions/{stack.sid}/mcp-apps/reopen", json={})
    assert empty.status_code == 422


def test_large_arguments_are_not_saved_with_a_reason(stack: Any, monkeypatch) -> None:
    monkeypatch.setattr(mcp_app_calls, "MAX_SAVED_ARGUMENT_BYTES", 64)
    app_id = _open(stack, {"title": "x" * 200})

    entry = saved_call(stack.app, stack.sid, app_id)
    assert entry["arguments"] is None
    assert entry["unsaved_reason"] == "arguments_not_saved"
    calls_before = len(stack.executor.calls)
    assert _reopen(stack, app_id)["error"]["reason"] == "arguments_not_saved"
    assert len(stack.executor.calls) == calls_before


def test_display_mode_is_recorded(stack: Any) -> None:
    app_id = _open(stack)
    assert set_saved_display_mode(stack.app, stack.sid, app_id, "fullscreen") is True
    assert saved_call(stack.app, stack.sid, app_id)["display_mode"] == "fullscreen"
    assert set_saved_display_mode(stack.app, stack.sid, "unknown", "inline") is False
    with pytest.raises(ValueError):
        set_saved_display_mode(stack.app, stack.sid, app_id, "pip")


def test_saved_calls_of_unknown_session_are_empty(stack: Any) -> None:
    assert saved_calls(stack.app, "sess_missing") == {}


def test_live_check_does_not_fail_for_a_closed_view(stack: Any) -> None:
    app_id = _open(stack)
    record = stack.app.state.mcp_app_registry.records_for_session(stack.sid)[0]
    base = f"/v1/sessions/{stack.sid}/mcp-apps/{app_id}/live"

    assert stack.client.get(base, params={"data_ref": record.data_ref}).json() == {"live": True}
    assert stack.client.get(base, params={"data_ref": "wrong"}).json() == {"live": False}
    stack.app.state.mcp_app_registry.drop_session(stack.sid)
    assert stack.client.get(base, params={"data_ref": record.data_ref}).json() == {"live": False}
    missing = stack.client.get(
        "/v1/sessions/sess_missing/mcp-apps/x/live", params={"data_ref": "r"}
    )
    assert missing.status_code == 404


def test_display_mode_route_updates_the_saved_call(stack: Any) -> None:
    app_id = _open(stack)
    record = stack.app.state.mcp_app_registry.records_for_session(stack.sid)[0]
    response = stack.client.put(
        f"/v1/sessions/{stack.sid}/mcp-apps/{app_id}/display-mode",
        params={"data_ref": record.data_ref},
        json={"mode": "fullscreen"},
    )
    assert response.status_code == 200
    assert saved_call(stack.app, stack.sid, app_id)["display_mode"] == "fullscreen"


def test_a_reopened_views_mode_reaches_the_card_it_came_from(stack: Any) -> None:
    first = _open(stack)
    reopened = _reopen(stack, first)["part"]["app_instance_id"]

    assert set_saved_display_mode(stack.app, stack.sid, reopened, "fullscreen")

    assert saved_call(stack.app, stack.sid, first)["display_mode"] == "fullscreen"
    assert saved_call(stack.app, stack.sid, reopened)["display_mode"] == "fullscreen"


def test_a_given_call_opens_a_view_and_is_saved(stack: Any) -> None:
    response = stack.client.post(
        f"/v1/sessions/{stack.sid}/mcp-apps/reopen",
        json={
            "call": {
                "server": "boards",
                "tool": "open_board",
                "arguments": {"title": "From a surface"},
                "display_mode": "fullscreen",
            }
        },
    )
    body = response.json()

    assert stack.executor.calls[-1] == ("boards_open_board", {"title": "From a surface"})
    assert body["part"]["display_mode"] == "fullscreen"
    assert "reopened_from" not in body["saved_call"]
    assert body["saved_call"]["arguments"] == {"title": "From a surface"}


@pytest.mark.parametrize(
    "call",
    [
        {"tool": "open_board"},
        {"server": "boards"},
        {"server": "boards", "tool": "open_board", "arguments": [1]},
        {"server": "boards", "tool": "open_board", "display_mode": "pip"},
    ],
)
def test_a_malformed_call_is_refused(stack: Any, call: dict[str, Any]) -> None:
    response = stack.client.post(f"/v1/sessions/{stack.sid}/mcp-apps/reopen", json={"call": call})
    assert response.status_code == 422


def test_a_given_call_to_a_tool_without_a_view_is_refused(stack: Any) -> None:
    body = stack.client.post(
        f"/v1/sessions/{stack.sid}/mcp-apps/reopen",
        json={"call": {"server": "boards", "tool": "plain", "arguments": {}}},
    ).json()
    assert body["error"]["reason"] == "tool_has_no_view"
    assert stack.executor.calls == []
