"""Mapping choices are independent, explicit and enforced through the shipped API."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage import setup_tool


def test_mapping_choice_to_agent_write_to_reviewed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    original, workspace = tmp_path / "original", tmp_path / "workspace"
    original.mkdir()
    workspace.mkdir()
    (original / "data.txt").write_text("before")
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    app.state.sessions = {"s": SimpleNamespace(workspace_id="w")}
    app.state.resource_store = ResourceStore(
        root=tmp_path / "resources", max_resource_bytes=1024 * 1024
    )
    register_connected_storage_routes(app)
    monkeypatch.setattr(setup_tool.context, "active_app", lambda: app)
    monkeypatch.setattr(setup_tool.context, "active_session_id", lambda: "s")
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "local", "root": str(original), "label": "Test"},
        )
        assert created.status_code == 201, created.text
        source_id = created.json()["id"]
        prefix = "/v1/workspaces/w/sources/" + source_id
        assert client.get(prefix + "/mapping-options").json()["link_access"] == [
            "read_only",
            "publish_later",
            "write_through",
        ]
        assert client.post(prefix + "/link", json={"access": "write_through"}).status_code == 409
        linked = client.post(prefix + "/link", json={"access": "publish_later"})
        from tests.test_gact.storage_operation_helpers import complete_indexing

        complete_indexing(client, prefix, linked)
        assert linked.json()["mode"] == "read_only"  # connection identity stays unchanged
        assert linked.json()["download_access"] == "read_only"
        listing = setup_tool.connected_data_open(source_id)
        assert listing["access"] == "publish_later"
        result = setup_tool.connected_data_write(
            source_id, "data.txt", "after", listing["revision"]
        )
        assert not result["writes_propagate"]
        assert (original / "data.txt").read_text() == "before"
        assert client.post(prefix + "/unlink").status_code == 409
        assert client.post(prefix + "/link", json={"access": "read_only"}).status_code == 409
        review = client.post(prefix + "/review")
        assert review.status_code == 200, review.text
        published = client.post(
            prefix + "/apply", json={"review_id": review.json()["id"], "paths": ["data.txt"]}
        )
        assert published.status_code == 200, published.text
        assert (original / "data.txt").read_text() == "after"
        readonly = client.post(prefix + "/link", json={"access": "read_only"})
        complete_indexing(client, prefix, readonly)
        readonly_listing = setup_tool.connected_data_open(source_id)
        assert readonly_listing["access"] == "read_only"
        with pytest.raises(PermissionError):
            setup_tool.connected_data_write(
                source_id, "data.txt", "forbidden", readonly_listing["revision"]
            )
        assert (original / "data.txt").read_text() == "after"


def test_public_readonly_resource_cannot_enable_remote_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    original, workspace = tmp_path / "original", tmp_path / "workspace"
    original.mkdir()
    workspace.mkdir()
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    register_connected_storage_routes(app)
    with TestClient(app) as client:
        source = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "local", "root": str(original), "label": "Read only"},
        ).json()
        prefix = "/v1/workspaces/w/sources/" + source["id"]
        monkeypatch.setattr("clio_agent.gact.storage.mapping.os.access", lambda *args: False)
        assert client.get(prefix + "/mapping-options").json()["link_access"] == ["read_only"]
        for mode in ["publish_later", "write_through"]:
            assert (
                client.post(
                    prefix + "/link", json={"access": mode, "confirm_remote": True}
                ).status_code
                == 403
            )
