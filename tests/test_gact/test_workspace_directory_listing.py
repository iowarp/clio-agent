"""Directory browsing never spends the root page on closed descendants."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from clio_agent.gact.app import build_app
from clio_agent.gact.routes import workspace_directory_listing as listing
from clio_agent.gact.storage.models import FileEntry, Manifest


def test_directory_pages_closed_children_without_reading_bodies(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    folder = root / "analysis"
    folder.mkdir()
    for index in range(7):
        (folder / f"plot-{index}.csv").write_text("sample,value\n1,2\n")
    (root / "README.md").write_text("Workspace description")
    monkeypatch.setattr(listing, "DIRECTORY_PAGE_SIZE", 3)
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    workspace = client.post(
        "/v1/workspaces", json={"name": "Directory test", "root_path": str(root)}
    ).json()
    url = f"/v1/workspaces/{workspace['id']}/files"
    first = client.get(url, params={"directory": ""}).json()
    assert {entry["path"] for entry in first["entries"]} >= {"analysis", "README.md"}
    assert not any("plot-" in entry["path"] for entry in first["entries"])
    assert first["next_offset"] is None
    pages = [
        client.get(url, params={"directory": "analysis", "offset": offset}).json()
        for offset in (0, 3, 6)
    ]
    assert [page["next_offset"] for page in pages] == [3, 6, None]
    assert len({row["path"] for page in pages for row in page["entries"]}) == 7
    assert all("media_type" not in row for page in pages for row in page["entries"])
    assert client.get(url, params={"directory": "../outside"}).status_code == 400
    assert client.get(url, params={"directory": "/outside"}).status_code == 400


def test_directory_listing_preserves_hidden_and_redacted_policy(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".hidden").mkdir()
    (root / ".hidden" / "note.md").write_text("hidden")
    (root / ".clio-child-cache").mkdir()
    (root / ".clio-child-cache" / "secret.txt").write_text("private cache")
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    workspace = client.post(
        "/v1/workspaces", json={"name": "Policy test", "root_path": str(root)}
    ).json()
    url = f"/v1/workspaces/{workspace['id']}/files"
    visible = client.get(url, params={"directory": "", "include_hidden": False}).json()
    assert not any(row["path"].startswith(".") for row in visible["entries"])
    assert (
        client.get(url, params={"directory": ".hidden", "include_hidden": False}).json()["entries"]
        == []
    )
    assert client.get(url, params={"directory": ".clio-child-cache"}).json()["entries"] == []


def test_directory_projects_only_owned_source_manifest_levels(tmp_path: Path) -> None:
    """Connected source names remain available without scanning their snapshots."""
    app = FastAPI()
    manifest = Manifest(
        id="revision",
        source_id="source",
        revision="one",
        hashes={},
        entries=[
            FileEntry(path="curves/area.csv", kind="file", size=12),
            FileEntry(path="curves/biomass.csv", kind="file", size=14),
        ],
    )
    source = SimpleNamespace(
        id="source",
        label="Experiment",
        local_path=str(tmp_path / "source/revision"),
        materialization="ready",
    )
    record = SimpleNamespace(
        source=source,
        linked_manifest_id=None,
        manifest_id="revision",
        connected=False,
        download_read_only=True,
    )
    app.state.connected_storage = SimpleNamespace(
        sources=Mock(return_value=[record]),
        store=SimpleNamespace(root=tmp_path, get=Mock(return_value=manifest)),
    )
    root = tmp_path / "workspace"
    root.mkdir()

    def page(directory: str) -> dict[str, object]:
        return asyncio.run(
            listing.collect_workspace_directory(
                app,
                "workspace",
                root,
                directory=directory,
                offset=0,
                include_hidden=False,
                exclude_service_storage=False,
            )
        )

    assert page("")["entries"] == [
        {
            "path": ".clio-agent/sources",
            "display_path": "Connected data",
            "type": "dir",
            "internal": False,
        }
    ]
    prefix = ".clio-agent/sources/source/revision"
    assert page(prefix)["entries"] == [
        {"path": prefix + "/curves", "display_path": "curves", "type": "dir", "internal": False}
    ]
    assert len(page(prefix + "/curves")["entries"]) == 2
    source.local_path = str(tmp_path / "different-owner")
    assert page("")["entries"] == []
