"""Dashboard publishing exercises the real catalog, artifact store, and HTTP export routes."""

from __future__ import annotations

import base64
import io
import json
import re
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities
from fastapi.testclient import TestClient
from PIL import Image

from clio_agent.gact import context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import build_publish_dashboard_report_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.designation_tool import build_create_artifact_tool
from clio_agent.gact.artifacts.minting import drain_turn_artifacts
from clio_agent.gact.dashboard_reports import (
    export_dashboard_report,
    list_dashboard_reports,
    publish_dashboard_report,
    read_dashboard_report,
)
from clio_agent.gact.types import AgentDef


@pytest.fixture
def dashboard(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str, Path]:
    """Use a real isolated workspace and active producer context."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    session = app.state.sessions.create(workspace_id="ws_default", title="Dashboard")
    remember_client_capabilities(
        app,
        session.id,
        A2UIClientCapabilities.model_validate(
            {"v0.9": {"supportedCatalogIds": [workspace_catalog_id()]}}
        ),
    )
    monkeypatch.setattr(context, "active_app", lambda: app)
    monkeypatch.setattr(context, "active_session_id", lambda: session.id)
    monkeypatch.setattr(context, "active_turn_id", lambda: "dashboard_turn")
    path = tmp_path / "analysis.dashboard.document.json"
    path.write_text(
        json.dumps(
            {
                "format": "clio.dashboard.document.v1",
                "title": "Design evolution",
                "components": [
                    {"id": "root", "component": "Column", "children": ["overview", "detail"]},
                    {"id": "overview", "component": "Text", "text": "Original and two evolutions."},
                    {"id": "detail", "component": "Text", "text": {"path": "/detail"}},
                ],
                "data_model": {"detail": "Shared dashboard model"},
            }
        ),
        encoding="utf-8",
    )
    return app, session.id, path


def test_publish_is_artifact_only_and_retains_definition(dashboard: tuple[Any, str, Path]) -> None:
    app, sid, path = dashboard
    result = build_publish_dashboard_report_tool()(definition_path=str(path))
    assert "artifact_id" in result, result
    assert app.state.a2ui_store.list_wire(sid) == []
    report = read_dashboard_report(app, sid, result["artifact_id"])
    assert report["definition"]["data_model"] == {"detail": "Shared dashboard model"}
    assert report["surface"]["state"] == "ready"
    assert (
        report["surface"]["messages"][-1]["updateDataModel"]["value"]
        == report["definition"]["data_model"]
    )
    assert list_dashboard_reports(app, sid)[0]["artifact_id"] == result["artifact_id"]
    links = drain_turn_artifacts(app, sid, "dashboard_turn")
    assert len(links) == 1
    assert links[0]["version"].artifact_id == result["artifact_id"]


def test_edit_and_publish_preserves_old_artifact(dashboard: tuple[Any, str, Path]) -> None:
    app, sid, path = dashboard
    first = publish_dashboard_report(app, sid, definition_path=str(path))
    source = json.loads(path.read_text())
    source["title"] = "Final comparison"
    source["data_model"]["detail"] = "Updated evidence"
    path.write_text(json.dumps(source))
    second = publish_dashboard_report(app, sid, definition_path=str(path), report_id=first["id"])
    assert second["id"] == first["id"]
    assert second["artifact_id"] != first["artifact_id"]
    assert read_dashboard_report(app, sid, first["artifact_id"])["title"] == "Design evolution"
    assert read_dashboard_report(app, sid, second["artifact_id"])["title"] == "Final comparison"
    assert len(list_dashboard_reports(app, sid)) == 1
    assert list_dashboard_reports(app, sid)[0]["artifact_id"] == second["artifact_id"]


def test_saved_view_control_uses_declared_binding_and_leaves_artifact_intact(
    dashboard: tuple[Any, str, Path],
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from clio_agent.gact.a2ui_producer import build_update_a2ui_data_model_tool
    from clio_agent.gact.a2ui_visual import ViewerReport
    from tests.test_gact.test_a2ui_visual import claim, reply

    app, sid, path = dashboard
    saved = publish_dashboard_report(app, sid, definition_path=str(path))
    original = read_dashboard_report(app, sid, saved["artifact_id"])
    surface = original["surface"]
    before = ViewerReport(
        viewer_id="saved-view",
        surface_id=surface["id"],
        revision=surface["revision"],
        view_revision=0,
        artifact_id=saved["artifact_id"],
        visible=True,
        ready=True,
        state={"detail": "Shared dashboard model"},
    )
    app.state.a2ui_visual.report(sid, before)
    args = {
        "surface_id": surface["id"],
        "expected_revision": surface["revision"],
        "artifact_id": saved["artifact_id"],
        "viewer_id": before.viewer_id,
        "expected_view_revision": 0,
        "path": "/detail",
        "value": "Temporary local view",
    }
    update = build_update_a2ui_data_model_tool()
    assert update(**{**args, "path": "/undeclared"})["reason"] == "a2ui_view_control_unbound"
    assert update(**{**args, "delete": True})["reason"] == "a2ui_view_control_invalid"
    missing_epoch = {key: value for key, value in args.items() if key != "expected_view_revision"}
    assert update(**missing_epoch)["reason"] == "a2ui_view_control_invalid"
    with ThreadPoolExecutor(1) as pool, TestClient(app) as client:
        future = pool.submit(update, **args)
        job = claim(app.state.a2ui_visual, before, sid)
        after = before.model_copy(update={"view_revision": 1, "state": {"detail": args["value"]}})
        endpoint = f"/v1/sessions/{sid}/a2ui/visual-feedback"
        assert client.post(endpoint, json=after.model_dump()).status_code == 200
        assert (
            client.post(
                endpoint,
                json=reply(job, after, png_base64="", previous_view_revision=0).model_dump(),
            ).status_code
            == 200
        )
        result = future.result(timeout=2)
    assert result["controlled"] is True
    assert read_dashboard_report(app, sid, saved["artifact_id"]) == original
    assert app.state.a2ui_store.list_wire(sid) == []


def test_linked_mesh_groups_publish_and_remain_isolated(dashboard: tuple[Any, str, Path]) -> None:
    """Publishing real referenced views keeps shared cameras valid and report-local."""
    app, sid, path = dashboard
    mesh = path.parent / "empty.gltf"
    mesh.write_text(
        json.dumps({"asset": {"version": "2.0"}, "scenes": [{"nodes": []}], "scene": 0})
    )
    registered = build_create_artifact_tool(AgentDef(id="mesh-source", title="CLIO")).func(
        path=str(mesh), pdf_preview=False
    )
    mesh_uri = f"artifact://{registered['artifacts'][0]['artifact_id']}"
    source = json.loads(path.read_text())
    source["components"] = [
        {"id": "root", "component": "Row", "children": ["a", "b"]},
        *[
            {
                "id": name,
                "component": "clio.mesh-viewport.v1",
                "meshUri": mesh_uri,
                "format": "gltf",
                "syncGroup": "comparison",
            }
            for name in ("a", "b")
        ],
    ]
    path.write_text(json.dumps(source))
    first = publish_dashboard_report(app, sid, definition_path=str(path))
    second = publish_dashboard_report(app, sid, definition_path=str(path))
    revised = publish_dashboard_report(app, sid, definition_path=str(path), report_id=first["id"])
    groups = []
    for result in (first, second, revised):
        report = read_dashboard_report(app, sid, result["artifact_id"])
        components = report["surface"]["messages"][1]["updateComponents"]["components"]
        names = [component["syncGroup"] for component in components if "syncGroup" in component]
        assert names[0] == names[1]
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", names[0])
        assert report["definition"]["components"][1]["syncGroup"] == "comparison"
        groups.append(names[0])
    assert groups[0] != groups[1]
    assert groups[0] == groups[2]
    source["components"][1]["syncGroup"] = "bad:group"
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="syncGroup"):
        publish_dashboard_report(app, sid, definition_path=str(path))


def test_publish_reports_registered_media_and_keeps_editable_source(
    dashboard: tuple[Any, str, Path],
) -> None:
    app, sid, path = dashboard
    image = path.parent / "progression.svg"
    image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    source = json.loads(path.read_text())
    source["components"] = [
        {"id": "root", "component": "Column", "children": ["diagram"]},
        {"id": "diagram", "component": "Image", "url": "progression.svg"},
    ]
    path.write_text(json.dumps(source))
    result = publish_dashboard_report(app, sid, definition_path=str(path))
    registered = result["exported_artifacts"][0]
    assert registered["component_id"] == "diagram"
    assert registered["uri"].startswith("artifact://")
    report = read_dashboard_report(app, sid, result["artifact_id"])
    assert report["definition"]["components"][1]["url"] == "progression.svg"
    assert (
        report["surface"]["messages"][1]["updateComponents"]["components"][1]["url"]
        == registered["uri"]
    )
    assert app.state.a2ui_store.list_wire(sid) == []


def test_foreign_session_cannot_read_or_revise(dashboard: tuple[Any, str, Path]) -> None:
    app, sid, path = dashboard
    saved = publish_dashboard_report(app, sid, definition_path=str(path))
    foreign = app.state.sessions.create(workspace_id="ws_default", title="Other")
    with pytest.raises(ValueError, match="does not belong"):
        read_dashboard_report(app, foreign.id, saved["artifact_id"])
    with pytest.raises(ValueError, match="does not belong"):
        publish_dashboard_report(app, foreign.id, definition_path=str(path), report_id=saved["id"])


def test_invalid_tree_and_outside_source_return_refusals(dashboard: tuple[Any, str, Path]) -> None:
    app, sid, path = dashboard
    result = build_publish_dashboard_report_tool()(
        definition_path=str(path.parent.parent / "outside.json")
    )
    assert result["reason"] == "dashboard_report_failed"
    assert "inside the workspace" in result["detail"]
    source = json.loads(path.read_text())
    source["components"] = [{"id": "wrong", "component": "Text", "text": "No root"}]
    path.write_text(json.dumps(source))
    result = build_publish_dashboard_report_tool()(definition_path=str(path))
    assert result["reason"] == "dashboard_report_failed"
    assert list_dashboard_reports(app, sid) == []


def test_http_html_download_retains_artifact_and_all_model_data(
    dashboard: tuple[Any, str, Path],
) -> None:
    app, sid, path = dashboard
    saved = publish_dashboard_report(app, sid, definition_path=str(path))
    report = read_dashboard_report(app, sid, saved["artifact_id"])
    drain_turn_artifacts(app, sid, "dashboard_turn")
    client_surface = {
        key: value
        for key, value in report["surface"].items()
        if key not in {"created_at", "updated_at", "evicted_messages", "actions"}
    }
    snapshot = {
        "sessions": {sid: [client_surface]},
        "responses": {},
        "tables": {},
        "failures": [],
    }
    with TestClient(app) as client:
        assert client.get(f"/v1/sessions/{sid}/dashboards").json()[0]["id"] == saved["id"]
        assert client.get(f"/v1/sessions/{sid}/dashboards/{saved['artifact_id']}").json() == report
        response = client.post(
            f"/v1/sessions/{sid}/dashboards/{saved['artifact_id']}/export",
            json={
                "javascript": "window.dashboardStarted=true;",
                "stylesheet": "body{color:black}",
                "snapshot": snapshot,
            },
        )
        assert response.status_code == 200, response.text
        value = response.json()
        download = client.get(value["download_path"])
        assert download.status_code == 200
        assert "CLIO_DASHBOARD_DATA" in download.text
        assert "connect-src blob: data:" in unescape(download.text)
        assert client.get(value["download_path"]).status_code == 404
        assert read_dashboard_report(app, sid, saved["artifact_id"]) == report
        assert drain_turn_artifacts(app, sid, "dashboard_turn") == []


def test_export_refuses_missing_dependency_or_another_surface(
    dashboard: tuple[Any, str, Path],
) -> None:
    app, sid, path = dashboard
    saved = publish_dashboard_report(app, sid, definition_path=str(path))
    renderer = {
        "javascript": "test",
        "stylesheet": "",
        "snapshot": {"sessions": {}, "failures": []},
    }
    with pytest.raises(ValueError, match="exactly"):
        export_dashboard_report(app, sid, saved["artifact_id"], renderer)
    renderer["snapshot"]["failures"] = [{"path": "artifact://missing", "message": "Missing data"}]
    with pytest.raises(ValueError, match="could not all be captured"):
        export_dashboard_report(app, sid, saved["artifact_id"], renderer)


def test_http_png_download_preserves_pixels_and_session_ownership(
    dashboard: tuple[Any, str, Path],
) -> None:
    """Displayed PNGs use single-use downloads and cannot export a foreign report."""
    app, sid, path = dashboard
    saved = publish_dashboard_report(app, sid, definition_path=str(path))
    report = read_dashboard_report(app, sid, saved["artifact_id"])
    pixels = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(pixels, format="PNG")
    body = {"png_base64": base64.b64encode(pixels.getvalue()).decode()}
    endpoint = f"/v1/sessions/{sid}/dashboards/{saved['artifact_id']}/export"
    foreign = app.state.sessions.create(workspace_id="ws_default", title="Other")
    with TestClient(app) as client:
        assert client.post(endpoint, json={"png_base64": "not-png"}).status_code == 409
        assert client.post(endpoint.replace(sid, foreign.id), json=body).status_code == 409
        prepared = client.post(endpoint, json=body)
        assert prepared.status_code == 200, prepared.text
        value = prepared.json()
        download = client.get(value["download_path"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "image/png"
        assert download.content == pixels.getvalue()
        assert client.get(value["download_path"]).status_code == 404
        assert read_dashboard_report(app, sid, saved["artifact_id"]) == report
