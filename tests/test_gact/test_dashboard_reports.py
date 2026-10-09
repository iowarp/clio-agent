"""Dashboard publishing exercises the real catalog, artifact store, and HTTP export routes."""

from __future__ import annotations

import json
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import build_publish_dashboard_report_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import drain_turn_artifacts
from clio_agent.gact.dashboard_reports import (
    export_dashboard_report,
    list_dashboard_reports,
    publish_dashboard_report,
    read_dashboard_report,
)


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
        assert "connect-src 'none'" in unescape(download.text)
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
