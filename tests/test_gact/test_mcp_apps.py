"""Focused production-boundary tests for the MCP Apps host."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import mcp_apps as mcp_apps_module
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_apps import (
    MCP_APP_MIME_TYPE,
    MCPAppAdmissionError,
    MCPAppRecord,
    MCPAppRegistry,
    _resolve_app_tool,
    call_tool_result_to_wire,
    cleanup_session_mcp_apps,
)
from clio_agent.gact.runtime.globals import (
    _gact_app_context,
    _tool_session_context,
)


class _Executor:
    """Capability-aware sync executor fixture used by the host routes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[tuple[str | None, str]] = []
        self.definitions = {
            "vigil_open": SimpleNamespace(
                meta={
                    "ui": {
                        "resourceUri": "ui://vigil/view",
                        "visibility": ["model", "app"],
                    }
                }
            ),
            "vigil_update": SimpleNamespace(meta={"ui": {"visibility": ["app"]}}),
            "vigil_close_viewer_session": SimpleNamespace(meta={"ui": {"visibility": ["app"]}}),
            "other_escape": SimpleNamespace(meta={"ui": {"visibility": ["app"]}}),
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        """Return both model-visible and app-only fixture tools."""

        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Record one normally gated tool call and return its private result."""

        self.calls.append((name, args))
        return {
            "content": [{"type": "text", "text": "updated"}],
            "structuredContent": {"revision": 2},
            "_meta": {"fixture.private": {"token": "bridge-only"}},
        }

    def read_resource(self, namespace: str | None, uri: str) -> list[dict[str, Any]]:
        """Record one exact-server resource read."""

        self.reads.append((namespace, uri))
        if uri == "ui://vigil/view":
            return [
                {
                    "uri": uri,
                    "mimeType": MCP_APP_MIME_TYPE,
                    "text": "<!doctype html><title>VIGIL</title>",
                    "_meta": {
                        "ui": {
                            "csp": {
                                "connectDomains": ["http://127.0.0.1:*"],
                                "resourceDomains": ["blob:"],
                            }
                        }
                    },
                }
            ]
        return [{"uri": uri, "mimeType": "application/json", "text": "{}"}]


def _open_result() -> dict[str, Any]:
    """Return a full result with public data and private admission metadata."""

    return {
        "content": [{"type": "text", "text": "opened"}],
        "structuredContent": {"session": {"state": "ready"}},
        "_meta": {
            "io.iowarp.vigil": {
                "admission": {"capability": "never-in-transcript"},
                "cleanup": {
                    "tool": "close_viewer_session",
                    "arguments": {"session_id": "viewer-1"},
                },
            }
        },
    }


def test_call_tool_result_wire_preserves_private_bridge_fields() -> None:
    """The private bridge projection retains every stable result field."""

    wire = call_tool_result_to_wire(_open_result())

    assert wire["content"][0]["text"] == "opened"
    assert wire["structuredContent"]["session"]["state"] == "ready"
    assert wire["_meta"]["io.iowarp.vigil"]["admission"] == {"capability": "never-in-transcript"}


def test_observer_emits_only_opaque_typed_part(tmp_path: Path) -> None:
    """Admission stores private data but emits only a capability reference."""

    executor = _Executor()
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)

    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "VIGIL"}).json()["id"]
        tool = executor.definitions["vigil_open"]
        with _gact_app_context(app), _tool_session_context(sid):
            app.state.pending_mcp_app_observer(
                "vigil_open",
                {"cluster": "configured-target"},
                tool,
                _open_result(),
                "vigil",
            )

        registry = app.state.mcp_app_registry
        assert isinstance(registry, MCPAppRegistry)
        record = registry.records_for_session(sid)[0]
        part = app.state.live_assistant_parts[sid][-1].to_wire()

        rendered = json.dumps(part, sort_keys=True)
        assert part["type"] == "mcp_app"
        assert part["app_instance_id"] == record.app_instance_id
        assert part["data_ref"] == record.data_ref
        assert part["source_server"] == "vigil"
        assert "never-in-transcript" not in rendered
        assert record.tool_result["_meta"]["io.iowarp.vigil"]["admission"] == {
            "capability": "never-in-transcript"
        }

    assert executor.calls[-1] == (
        "vigil_close_viewer_session",
        {"session_id": "viewer-1"},
    )
    assert registry.records_for_session(sid) == []


def test_registry_rejects_overflow_without_evicting_cleanup_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count/TTL bounds never discard an owned remote cleanup identity."""

    monkeypatch.setattr(mcp_apps_module, "_REGISTRY_LIMIT", 1)
    monkeypatch.setattr(mcp_apps_module, "_REGISTRY_TTL_S", 1)
    registry = MCPAppRegistry()
    first = registry.register(
        session_id="session-owned",
        source_namespace="vigil",
        tool_name="vigil_open",
        resource_uri="ui://vigil/view",
        tool_input={},
        tool_result=_open_result(),
    )
    first.last_access = 0

    with pytest.raises(MCPAppAdmissionError, match="active-instance limit") as caught:
        registry.register(
            session_id="session-owned",
            source_namespace="vigil",
            tool_name="vigil_open",
            resource_uri="ui://vigil/view",
            tool_input={},
            tool_result=_open_result(),
        )

    retained = registry.records_for_session("session-owned")
    assert {record.app_instance_id for record in retained} == {
        first.app_instance_id,
        caught.value.record.app_instance_id,
    }
    assert caught.value.record.tool_result == {}

    executor = _Executor()
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    app.state.mcp_app_registry = registry
    asyncio.run(cleanup_session_mcp_apps(app, "session-owned"))

    assert executor.calls == [
        ("vigil_close_viewer_session", {"session_id": "viewer-1"}),
        ("vigil_close_viewer_session", {"session_id": "viewer-1"}),
    ]
    assert registry.records_for_session("session-owned") == []


def test_app_tool_resolution_requires_exact_origin_namespace() -> None:
    """An originless record cannot use globally app-visible tools."""

    record = MCPAppRecord(
        app_instance_id="app-originless",
        data_ref="secret",
        session_id="session",
        source_namespace=None,
        tool_name="vigil_open",
        resource_uri="ui://vigil/view",
        tool_input={},
        tool_result={},
        cleanup=None,
    )

    with pytest.raises(PermissionError, match="no exact originating server namespace"):
        _resolve_app_tool(_Executor(), record, "vigil_update")


def test_capability_routes_stay_bound_and_session_delete_cleans_up(tmp_path: Path) -> None:
    """All App operations stay on one source server and owned cleanup is explicit."""

    executor = _Executor()
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)

    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid = client.post("/v1/sessions", json={"title": "VIGIL"}).json()["id"]
        registry = app.state.mcp_app_registry
        record = registry.register(
            session_id=sid,
            source_namespace="vigil",
            tool_name="vigil_open",
            resource_uri="ui://vigil/view",
            tool_input={"cluster": "configured-target"},
            tool_result=_open_result(),
        )
        prefix = f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}"
        capability = {"data_ref": record.data_ref}

        missing = client.get(prefix, params={"data_ref": "wrong"})
        assert missing.status_code == 404

        payload = client.get(
            prefix,
            params=capability,
            headers={"referer": "http://127.0.0.1:8100/session"},
        )
        assert payload.status_code == 200
        payload_body = payload.json()
        assert payload_body["tool_result"]["_meta"]["io.iowarp.vigil"]
        assert urlparse(payload_body["sandbox_url"]).netloc == "localhost:8100"
        assert urlparse(payload_body["sandbox_url"]).netloc != "127.0.0.1:8100"
        desktop_payload = client.get(
            prefix,
            params=capability,
            headers={"origin": "tauri://localhost"},
        ).json()
        assert urlparse(desktop_payload["sandbox_url"]).netloc == "127.0.0.1:8100"
        assert executor.reads == [("vigil", "ui://vigil/view")]

        sandbox = client.get(
            f"{prefix}/sandbox",
            params=capability,
            headers={"referer": "http://127.0.0.1:5173/session"},
        )
        assert sandbox.status_code == 200
        assert "allow-scripts allow-forms" in sandbox.text
        assert "allow-scripts allow-same-origin" not in sandbox.text
        assert "inner.srcdoc = html" in sandbox.text
        assert "event.origin === 'null'" in sandbox.text
        assert "frame-ancestors http://127.0.0.1:5173" in sandbox.headers["content-security-policy"]
        assert "frame-src 'self' data: blob:" in sandbox.headers["content-security-policy"]

        update = client.post(
            f"{prefix}/tools/call",
            params=capability,
            json={"name": "update", "arguments": {"revision": 1}},
        )
        assert update.status_code == 200
        assert update.json()["_meta"]["fixture.private"]["token"] == "bridge-only"
        assert executor.calls[-1] == ("vigil_update", {"revision": 1})

        escaped = client.post(
            f"{prefix}/tools/call",
            params=capability,
            json={"name": "other_escape", "arguments": {}},
        )
        assert escaped.status_code == 403

        resource = client.post(
            f"{prefix}/resources/read",
            params=capability,
            json={"uri": "artifact://viewer/export-1"},
        )
        assert resource.status_code == 200
        assert executor.reads[-1] == ("vigil", "artifact://viewer/export-1")

        deleted = client.delete(f"/v1/sessions/{sid}")
        assert deleted.status_code == 204
        assert executor.calls[-1] == (
            "vigil_close_viewer_session",
            {"session_id": "viewer-1"},
        )
        assert registry.records_for_session(sid) == []
