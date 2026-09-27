"""How viewers load media, stated where the model reads it (0.9.4.19 ruling).

Viewers never auto-load an external URL; a workspace path is exported as an
artifact and renders everywhere. The model must be told this in the two
places it looks: the generated catalog skill (before it builds a surface)
and the producer tool result (after it used an external URL), so it can
recover in one step by downloading the file into the workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs, workspace_catalog_id
from clio_agent.gact.a2ui_catalogs.media_sources import (
    external_url_notice,
    media_source_lines,
    url_properties,
)
from clio_agent.gact.a2ui_catalogs.skills import generate_catalog_skill_body
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_update_a2ui_components_tool,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.media_download_tool import MEDIA_DOWNLOAD_TOOL

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _producer_session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str, Path]:
    # ``build_app`` must be THIS module's name: the test_gact conftest injects a
    # per-test ARC by wrapping the requesting module's ``build_app``.
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    root = tmp_path / "ws"
    root.mkdir()
    client = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    caps = A2UIClientCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": [workspace_catalog_id()]}}
    )
    remember_client_capabilities(app, sid, caps)
    return app, sid, root


def _image_surface(url: str) -> list[dict[str, Any]]:
    return [
        {"id": "root", "component": "Column", "children": ["img"]},
        {"id": "img", "component": "Image", "url": url},
    ]


def test_url_properties_are_read_from_the_catalog_schema() -> None:
    basic, workspace = load_builtin_catalogs()
    assert url_properties(basic.file) == [
        ("AudioPlayer", "url"),
        ("Image", "url"),
        ("Video", "url"),
    ]
    assert ("Image", "url") in url_properties(workspace.file)
    assert ("clio.artifact.v1", "uri") in url_properties(workspace.file)


def test_a_catalog_without_url_properties_gets_no_media_section() -> None:
    file = {"components": {"Text": {"properties": {"text": {"type": "string"}}}}}
    assert url_properties(file) == []
    assert media_source_lines(file) == []


@pytest.mark.parametrize("index", [0, 1])
def test_the_catalog_skill_states_how_viewers_load_media(index: int) -> None:
    entry = load_builtin_catalogs()[index]
    body = generate_catalog_skill_body(entry)
    assert "## Media and file sources" in body
    section = body.split("## Media and file sources", 1)[1]
    assert "never loaded" in section
    assert "link" in section
    assert "exported as an artifact" in section
    assert f"`{MEDIA_DOWNLOAD_TOOL}`" in section
    assert "`Image` (`url`)" in section


def test_the_notice_names_hosts_components_and_the_recovery() -> None:
    notice = external_url_notice(
        [
            {"component_id": "img", "property": "url", "host": "upload.wikimedia.org"},
            {"component_id": "vid", "property": "url", "host": "cdn.example.org"},
        ]
    )
    assert "upload.wikimedia.org" in notice and "cdn.example.org" in notice
    assert "img" in notice and "vid" in notice
    assert MEDIA_DOWNLOAD_TOOL in notice
    assert "workspace path" in notice
    assert "artifact" in notice


def test_create_surface_reports_external_urls_with_the_recovery(
    tmp_path: Any, monkeypatch: Any
) -> None:
    _producer_session(tmp_path, monkeypatch)
    url = "https://upload.wikimedia.org/wikipedia/commons/a/a3/Cat.png"

    result = build_create_a2ui_surface_tool()(surface_id="pic", components=_image_surface(url))

    # Admitted (a link card is a legitimate outcome), never silently.
    assert result.get("ok") is not False, result
    assert result["external_urls"] == [
        {"component_id": "img", "property": "url", "url": url, "host": "upload.wikimedia.org"}
    ]
    assert MEDIA_DOWNLOAD_TOOL in result["external_url_notice"]
    assert "upload.wikimedia.org" in result["external_url_notice"]


def test_update_components_reports_external_urls_too(tmp_path: Any, monkeypatch: Any) -> None:
    _app, _sid, root = _producer_session(tmp_path, monkeypatch)
    (root / "local.png").write_bytes(PNG)
    build_create_a2ui_surface_tool()(surface_id="pic", components=_image_surface("local.png"))

    result = build_update_a2ui_components_tool()(
        surface_id="pic", components=_image_surface("https://example.org/b.png")
    )
    assert result["external_urls"][0]["host"] == "example.org"


def test_workspace_paths_and_references_carry_no_notice(tmp_path: Any, monkeypatch: Any) -> None:
    _app, _sid, root = _producer_session(tmp_path, monkeypatch)
    (root / "local.png").write_bytes(PNG)
    result = build_create_a2ui_surface_tool()(
        surface_id="pic", components=_image_surface("local.png")
    )
    assert "external_urls" not in result
    assert "external_url_notice" not in result


def test_an_insecure_http_url_is_refused_with_the_same_recovery(
    tmp_path: Any, monkeypatch: Any
) -> None:
    _producer_session(tmp_path, monkeypatch)
    result = build_create_a2ui_surface_tool()(
        surface_id="pic", components=_image_surface("http://example.org/cat.png")
    )
    assert result["ok"] is False
    assert result["reason"] == "a2ui_url_unresolved"
    assert MEDIA_DOWNLOAD_TOOL in result["detail"]
    assert "workspace" in result["detail"]


def test_the_unresolved_path_hint_names_the_download_recovery() -> None:
    from clio_agent.gact.a2ui_producer._refusal import _DEFAULT_HINTS

    hint = _DEFAULT_HINTS["a2ui_url_unresolved"]
    assert MEDIA_DOWNLOAD_TOOL in hint
    assert "link" in hint
