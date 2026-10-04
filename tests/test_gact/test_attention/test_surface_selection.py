"""Selections use stored surface identities, not renderer order or invented patch maps."""

from pathlib import Path

import pytest
from clio_schemas.connected_resources import ContentSelection
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.surface_selection import validate_surface_reference


def test_surface_selection_uses_exact_revision_and_refuses_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real store and HTTP selection boundary for image and data selections."""
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Selected media"}).json()["id"]
        records = app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "createSurface": {"surfaceId": "surface", "catalogId": workspace_catalog_id()},
                },
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "surface",
                        "components": [
                            {"id": "root", "component": "Column", "children": ["image", "table"]},
                            {
                                "id": "image",
                                "component": "Image",
                                "url": "artifact://image-1",
                                "description": "Selected image",
                            },
                            {
                                "id": "table",
                                "component": "clio.data-table.v1",
                                "rows": [{"id": "station-a", "value": 1}],
                                "columns": [
                                    {"key": "id", "label": "Station"},
                                    {"key": "value", "label": "Value"},
                                ],
                            },
                        ],
                    },
                },
            ],
            message_id="media-message",
            part_id="media-part",
        )
        surface = records[-1]
        path = f"/v1/sessions/{sid}/attention/surfaces/surface/selection"
        body = {
            "revision": surface.revision,
            "component_id": "image",
            "source_ref": "artifact://image-1",
            "selection": {"kind": "image_region", "x": 0.2, "y": 0.3, "width": 0.5, "height": 0.4},
        }
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        ref = response.json()
        assert (ref["session_id"], ref["message_id"], ref["part_id"]) == (
            sid,
            "msg_a2ui_media-part",
            "media-part",
        )
        assert ref["artifact_ref"] == "artifact://image-1"
        assert ref["selection"] == body["selection"]
        assert len(ref["content_revision"]) == 64
        assert ref["surface"]["revision"] == surface.revision
        validate_surface_reference(app, sid, ContentSelection.model_validate(ref))
        assert client.post(path, json={**body, "revision": 0}).status_code == 409
        assert (
            client.post(path, json={**body, "source_ref": "artifact://another"}).status_code == 409
        )
        assert client.post(path.replace(sid, "other"), json=body).status_code == 404
        structured = {
            "kind": "structured",
            "surface_id": "surface",
            "component_id": "table",
            "source_ref": "a2ui://surface/table",
            "keys": ['["id","station-a"]'],
        }
        response = client.post(
            path,
            json={
                **body,
                "component_id": "table",
                "source_ref": structured["source_ref"],
                "selection": structured,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["selection"] == structured
        too_large = {**structured, "keys": ["x"] * 1001}
        assert client.post(path, json={**body, "selection": too_large}).status_code == 422
        app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "surface",
                        "components": [
                            {"id": "image", "component": "Image", "url": "artifact://image-2"}
                        ],
                    },
                }
            ],
        )
        with pytest.raises(AttentionUnavailable, match="selected A2UI view changed"):
            validate_surface_reference(app, sid, ContentSelection.model_validate(ref))
        assert client.post(path, json=body).status_code == 409
