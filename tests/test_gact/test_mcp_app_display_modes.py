"""Display modes, registry pinning and downloads for the MCP Apps host (MA1-MA7, MA16)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_downloads import safe_file_name
from clio_agent.gact.mcp_apps import (
    MCP_APP_MIME_TYPE,
    MCPAppAdmissionError,
    MCPAppRegistry,
    registry_limit,
    registry_ttl_s,
)
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from tests._config_layer import set_config


class _Executor:
    """One app-bearing tool and its view resource."""

    def __init__(self) -> None:
        self.definitions = {
            "fixture_open": SimpleNamespace(
                meta={"ui": {"resourceUri": "ui://fixture/view"}}, title="Fixture viewer"
            )
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": name}]}

    def read_resource(self, namespace: str | None, uri: str) -> list[dict[str, Any]]:
        return [{"uri": uri, "mimeType": MCP_APP_MIME_TYPE, "text": "<!doctype html><p>view"}]


def _register(registry: MCPAppRegistry, sid: str = "s1", mode: str = "inline"):
    return registry.register(
        session_id=sid,
        source_namespace="fixture",
        tool_name="fixture_open",
        resource_uri="ui://fixture/view",
        tool_input={},
        tool_result={"content": [{"type": "text", "text": "opened"}]},
        display_mode=mode,
    )


def test_registry_knobs_resolve_through_config() -> None:
    """The registry bounds are configuration, not literals (knob audit)."""

    assert registry_limit() == 64
    assert registry_ttl_s() == 3600.0
    set_config("gact.mcp_apps.registry_limit", 3)
    set_config("gact.mcp_apps.registry_ttl_s", 5)
    registry = MCPAppRegistry()
    assert (registry.limit, registry.ttl_s) == (3, 5.0)


def test_pinned_records_never_expire_and_are_not_counted() -> None:
    """A tab or surface record survives the idle expiry and the count bound (MA6)."""

    registry = MCPAppRegistry(limit=1, ttl_s=1)
    tab = _register(registry, mode="fullscreen")
    surface = _register(registry)
    registry.set_surface_pinned("s1", surface.app_instance_id, surface.data_ref, True)
    inline = _register(registry)  # admitted: pinned records are not counted
    with pytest.raises(MCPAppAdmissionError, match="limit \\(1\\)"):
        _register(registry)
    for record in (tab, surface, inline):
        record.last_access = 0

    live = {record.app_instance_id for record in registry.records_for_session("s1")}
    assert live == {tab.app_instance_id, surface.app_instance_id}
    with pytest.raises(KeyError):
        registry.get("s1", inline.app_instance_id, inline.data_ref)

    # Returning the tab to the conversation makes it an ordinary inline record again.
    registry.set_display_mode("s1", tab.app_instance_id, tab.data_ref, "inline")
    tab.last_access = 0
    assert tab.app_instance_id not in {
        record.app_instance_id for record in registry.records_for_session("s1")
    }


def test_set_display_mode_rejects_unknown_modes_and_foreign_capabilities() -> None:
    registry = MCPAppRegistry(limit=4, ttl_s=60)
    record = _register(registry)
    with pytest.raises(ValueError, match="inline, fullscreen"):
        registry.set_display_mode("s1", record.app_instance_id, record.data_ref, "pip")
    with pytest.raises(KeyError):
        registry.set_display_mode("s1", record.app_instance_id, "wrong", "fullscreen")
    with pytest.raises(KeyError):
        registry.set_display_mode("s2", record.app_instance_id, record.data_ref, "fullscreen")


def test_part_projection_carries_the_display_mode() -> None:
    block = part_to_v3_block(
        {"id": "p", "type": "mcp_app", "app_instance_id": "a", "display_mode": "fullscreen"}
    )
    assert block["display_mode"] == "fullscreen"
    assert part_to_v3_block({"id": "p", "type": "mcp_app"})["display_mode"] == "inline"


def _open_app(app: Any, client: TestClient, sid: str) -> dict[str, Any]:
    executor = app.state.agent._active_tool_executor()
    with _gact_app_context(app), _tool_session_context(sid):
        app.state.pending_mcp_app_observer(
            "fixture_open",
            {"topic": "x"},
            executor.definitions["fixture_open"],
            {"content": [{"type": "text", "text": "opened"}]},
            "fixture",
        )
    return app.state.live_assistant_parts[sid][-1].to_wire()


def _client(tmp_path: Path) -> tuple[Any, TestClient]:
    executor = _Executor()
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    return app, TestClient(app, base_url="http://127.0.0.1:8100")


def test_display_mode_route_sets_mode_and_pin(tmp_path: Path) -> None:
    """``PUT …/display-mode`` moves the frame; ``PUT …/pin`` pins a surface (MA4)."""

    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "apps"}).json()["id"]
        part = _open_app(app, client, sid)
        assert part["display_mode"] == "inline"
        # FP-D2: no saved connection names this server, so the tool title leads,
        # followed by the opening call's subject.
        assert part["title"] == "Fixture viewer: x"
        base = f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}"
        query = {"data_ref": part["data_ref"]}
        headers = {"Origin": "http://127.0.0.1:5173", "Referer": "http://127.0.0.1:5173/"}

        put = client.put(f"{base}/display-mode", params=query, json={"mode": "fullscreen"})
        assert put.json() == {"display_mode": "fullscreen", "pinned": True}
        descriptor = client.get(base, params=query, headers=headers).json()
        assert (descriptor["display_mode"], descriptor["pinned"]) == ("fullscreen", True)

        assert client.put(f"{base}/display-mode", params=query, json={"mode": "pip"}).status_code
        assert (
            client.put(
                f"{base}/display-mode", params={"data_ref": "nope"}, json={"mode": "inline"}
            ).status_code
            == 404
        )
        inline = client.put(f"{base}/display-mode", params=query, json={"mode": "inline"})
        assert inline.json() == {"display_mode": "inline", "pinned": False}
        pin = client.put(f"{base}/pin", params=query, json={"pinned": True})
        assert pin.json() == {"display_mode": "inline", "pinned": True}
        assert client.put(f"{base}/pin", params=query, json={"pinned": "yes"}).status_code == 422


def test_download_saves_into_the_workspace_and_registers_an_artifact(tmp_path: Path) -> None:
    """``ui/download-file`` contents become workspace files and artifacts (MA16)."""

    root = tmp_path / "ws"
    root.mkdir()
    (root / "report.csv").write_text("taken\n", encoding="utf-8")
    app, client = _client(tmp_path)
    with client:
        wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
        sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
        part = _open_app(app, client, sid)
        url = f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}/downloads"
        query = {"data_ref": part["data_ref"]}
        body = {
            "contents": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///exports/report.csv",
                        "mimeType": "text/csv",
                        "text": "a,b\n1,2\n",
                    },
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///../../etc/passwd",
                        "mimeType": "application/octet-stream",
                        "blob": base64.b64encode(b"\x00\x01").decode(),
                    },
                },
            ]
        }
        saved = client.post(url, params=query, json=body)
        assert saved.status_code == 200, saved.text
        files = saved.json()["files"]
        assert [row["path"] for row in files] == ["report (2).csv", "passwd"]
        assert (root / "report (2).csv").read_text(encoding="utf-8") == "a,b\n1,2\n"
        assert (root / "passwd").read_bytes() == b"\x00\x01"
        assert (root / "report.csv").read_text(encoding="utf-8") == "taken\n"
        assert all(row["artifact_id"] for row in files), files
        listed = client.get(f"/v1/workspaces/{wid}/artifacts").json()
        assert "report (2).csv" in json.dumps(listed)

        link = client.post(
            url,
            params=query,
            json={"contents": [{"type": "resource_link", "uri": "https://example.org/a.pdf"}]},
        )
        assert link.status_code == 422
        assert link.json()["error"]["error"] == "download_link_unsupported"
        empty = client.post(url, params=query, json={"contents": []})
        assert empty.json()["error"]["error"] == "invalid_content"


def test_download_without_a_workspace_folder_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "gone"
    root.mkdir()
    app, client = _client(tmp_path)
    with client:
        wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
        sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
        root.rmdir()
        part = _open_app(app, client, sid)
        response = client.post(
            f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}/downloads",
            params={"data_ref": part["data_ref"]},
            json={"contents": [{"type": "resource", "resource": {"uri": "a.txt", "text": "x"}}]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["error"] == "no_workspace"


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("file:///export.json", "export.json"),
        ("ui://view/a%2Fb.png", "a_b.png"),
        ("..", "download"),
        ("file:///dir/", "dir"),
        ("weird<name>.txt", "weird_name_.txt"),
    ],
)
def test_safe_file_name(uri: str, expected: str) -> None:
    assert safe_file_name(uri) == expected


def test_app_calls_run_as_the_session_outside_a_turn(tmp_path: Path) -> None:
    """A view's call binds its session as the turn session (external calls need its owner)."""

    import asyncio

    from clio_agent.gact import context as gact_context
    from clio_agent.gact.mcp_apps import _run_bound

    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "apps"}).json()["id"]
        seen = asyncio.run(_run_bound(app, sid, lambda _executor: gact_context.active_session_id()))
    assert seen == sid
    assert gact_context.active_session_id() == ""


def _remote_app(
    tmp_path: Path, headers: dict[str, str] | None = None
) -> tuple[Any, TestClient, dict[str, Any], str]:
    """An app opened while CLIO is reached as ``clio.lan.test`` (a LAN name)."""

    set_config("gact.allowed_hosts", "clio.lan.test")
    app, _local = _client(tmp_path)
    client = TestClient(app, base_url="http://clio.lan.test:8100", headers=headers)
    client.__enter__()
    sid = client.post("/v1/sessions", json={"title": "remote"}).json()["id"]
    return app, client, _open_app(app, client, sid), sid


def test_a_remote_browser_needs_a_configured_sandbox_host(tmp_path: Path) -> None:
    """Reached by a LAN name, CLIO refuses to serve a view on an origin it cannot isolate."""

    _app, client, part, sid = _remote_app(tmp_path)
    try:
        base = f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}"
        refused = client.get(base, params={"data_ref": part["data_ref"]})
        assert refused.status_code == 409
        assert refused.json()["error"]["error"] == "sandbox_host_unconfigured"
        set_config("gact.mcp_apps.sandbox_host", "clio.lan.test")
        same = client.get(base, params={"data_ref": part["data_ref"]})
        assert same.json()["error"]["error"] == "sandbox_host_unconfigured"
    finally:
        client.__exit__(None, None, None)


def test_a_remote_browser_gets_a_per_server_sandbox_host(tmp_path: Path) -> None:
    """``*.apps…`` gives each server its own host, serving only the view and its service."""

    from clio_agent.gact.mcp_app_sandbox_origin import sandbox_host_for

    _app, client, part, sid = _remote_app(tmp_path)
    try:
        set_config("gact.mcp_apps.sandbox_host", "*.apps.lan.test")
        base = f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}"
        query = {"data_ref": part["data_ref"]}
        descriptor = client.get(
            base, params=query, headers={"Referer": "http://clio.lan.test:5173/"}
        ).json()
        host = sandbox_host_for("fixture", "clio.lan.test")
        assert host.endswith(".apps.lan.test") and host.startswith("s")
        assert descriptor["sandbox_url"].startswith(f"http://{host}:8100{base}/sandbox?")
        sandbox = client.get(
            f"http://{host}:8100{base}/sandbox",
            params=query,
            headers={"Referer": "http://clio.lan.test:5173/session"},
        )
        assert sandbox.status_code == 200, sandbox.text
        csp = sandbox.headers["content-security-policy"]
        assert "frame-ancestors http://clio.lan.test:5173" in csp
        foreign = client.get(
            f"http://{host}:8100{base}/sandbox",
            params=query,
            headers={"Referer": "http://elsewhere.test/"},
        )
        assert foreign.status_code == 403
        api = client.get(f"http://{host}:8100/v1/sessions")
        assert api.status_code == 403
        assert api.json()["error"]["error"] == "sandbox_origin_refused"
        other = client.get(
            f"http://{sandbox_host_for('other', 'clio.lan.test')}:8100{base}/sandbox",
            params=query,
            headers={"Referer": "http://clio.lan.test:5173/"},
        )
        assert other.status_code == 403
    finally:
        client.__exit__(None, None, None)


def test_the_sandbox_route_needs_no_bearer_but_the_api_on_its_host_does(tmp_path: Path) -> None:
    """A frame cannot send the bearer: the app's capability admits its sandbox document."""

    from clio_agent.gact.mcp_app_sandbox_origin import sandbox_host_for

    set_config("gact.auth.bearer_token", "secret-token")
    app, authed, part, sid = _remote_app(tmp_path, {"Authorization": "Bearer secret-token"})
    client = TestClient(app, base_url="http://clio.lan.test:8100")
    try:
        app.state.peer_address_getter = lambda _scope: "10.0.0.5"
        set_config("gact.mcp_apps.sandbox_host", "*.apps.lan.test")
        host = sandbox_host_for("fixture", "clio.lan.test")
        base = f"/v1/sessions/{sid}/mcp-apps/{part['app_instance_id']}"
        query = {"data_ref": part["data_ref"]}
        sandbox = client.get(
            f"http://{host}:8100{base}/sandbox",
            params=query,
            headers={"Referer": "http://clio.lan.test:5173/"},
        )
        assert sandbox.status_code == 200, sandbox.text
        assert client.get(f"http://{host}:8100/v1/sessions").status_code in {401, 403}
        assert client.get("/v1/sessions").status_code == 401
    finally:
        authed.__exit__(None, None, None)
