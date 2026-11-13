"""A view's state keys, declared by its server or seen in its reports (FP-D11)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import mcp_app_state_keys as keys_module
from clio_agent.gact.a2ui_producer import _mcp_app_reference
from clio_agent.gact.mcp_app_state_keys import (
    STATE_META_KEY,
    declared_state_keys,
    describe,
    state_keys,
)

VIEW_META = {"ui": {"resourceUri": "ui://viewer/view"}}


def _tool(state: Any = None) -> dict[str, Any]:
    meta = dict(VIEW_META)
    if state is not None:
        meta[STATE_META_KEY] = state
    return {"name": "open", "_meta": meta, "inputSchema": {"type": "object"}}


def _app(definitions: dict[str, Any], contexts: dict[str, Any] | None = None) -> Any:
    executor = SimpleNamespace(get_all_tool_definitions=lambda: definitions)
    session = SimpleNamespace(metadata={"mcp_app_context": contexts or {}})
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=SimpleNamespace(tool_executor=executor),
            sessions=SimpleNamespace(get=lambda sid: session),
        )
    )


def test_a_declaration_names_keys_and_their_tools() -> None:
    declared = declared_state_keys(
        _tool(
            [
                {"key": "time", "tool": "set_time", "description": "the shown time"},
                {"key": "scene"},
                {"key": "bad key"},
                {"key": "time"},
                {"no": "key"},
                {"key": "zoom", "tool": "not a tool"},
            ]
        ),
        "viewer_open",
    )
    assert [k.to_dict() for k in declared] == [
        {"key": "time", "declared": True, "tool": "set_time", "description": "the shown time"},
        {"key": "scene", "declared": True},
        {"key": "zoom", "declared": True},
    ]
    assert describe(declared) == "time (set with set_time), scene, zoom"


def test_a_malformed_declaration_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        assert declared_state_keys(_tool({"time": "x"}), "viewer_open") == []
    assert "state_declaration_invalid" in caplog.text


def test_reported_keys_follow_declared_ones() -> None:
    contexts = {
        "app_1": {
            "server": "viewer",
            "tool": "viewer_open",
            "context": {"structuredContent": {"time": 1.0, "camera": [0, 0, 1]}},
        },
        "app_2": {"server": "other", "tool": "open", "context": {"structuredContent": {"x": 1}}},
    }
    app = _app({}, contexts)
    found = state_keys(app, "sess", "viewer", "open", _tool([{"key": "time", "tool": "set"}]))
    assert [(k.key, k.declared) for k in found] == [("time", True), ("camera", False)]


def _component(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "view",
        "component": "clio.mcp-app.v1",
        "server": "viewer",
        "tool": "open",
        "arguments": {},
        "state": [{"key": "time", "path": "/selection/time"}],
        **overrides,
    }


def test_a_declaring_server_refuses_a_channel_key_it_does_not_report() -> None:
    app = _app({"viewer_open": _tool([{"key": "time_index", "tool": "set_timestep"}])})
    refused = _mcp_app_reference.validate_mcp_app_components(
        app, [_component()], session_id="sess"
    )
    assert refused is not None
    assert refused["reason"] == "a2ui_mcp_app_state_key_unknown"
    assert "time_index (set with set_timestep)" in refused["detail"]
    accepted = _component(state=[{"key": "time_index", "path": "/selection/time"}])
    assert _mcp_app_reference.validate_mcp_app_components(app, [accepted]) is None


def test_a_server_that_declares_nothing_is_never_refused_on_keys() -> None:
    app = _app({"viewer_open": _tool()})
    assert _mcp_app_reference.validate_mcp_app_components(app, [_component()]) is None


def test_the_tool_not_found_refusal_lists_each_view_tool_with_its_keys() -> None:
    app = _app({"viewer_open": _tool([{"key": "time", "tool": "set_timestep"}])})
    refused = _mcp_app_reference.validate_mcp_app_components(app, [_component(tool="missing")])
    assert refused is not None
    assert "open (state keys: time (set with set_timestep))" in refused["detail"]


def test_the_context_block_names_the_state_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import mcp_app_context

    app = _app({"viewer_open": _tool([{"key": "time", "tool": "set_timestep"}])})
    record = SimpleNamespace(source_namespace="viewer", tool_name="viewer_open")
    words = keys_module.record_state_words(app, "sess", record)
    assert words == "time (set with set_timestep)"
    seen: dict[str, Any] = {}

    def render(source: str, variables: dict[str, Any], **_: Any) -> str:
        seen.update(variables)
        return "block"

    monkeypatch.setattr("clio_agent.injections.render", render)
    mcp_app_context.app_context_block(app, "sess", server="viewer", tool="open", state_keys=words)
    assert seen["state_keys"] == "time (set with set_timestep)"
