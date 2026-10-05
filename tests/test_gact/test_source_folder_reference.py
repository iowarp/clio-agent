"""Folder attachments carry real source identity through resource custody to the agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.resource_enrichment import describe_resource_parts
from clio_agent.gact.storage import setup_tool
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.references import source_folder_resource
from clio_agent.gact.storage.service import StorageService


@pytest.mark.parametrize("linked", [True, False])
@pytest.mark.parametrize("folder", ["", "selected"])
def test_folder_reference_survives_message_grounding_and_opens_only_approved_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool, folder: str
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "a.txt").write_bytes(b"real input")
    (upstream / "selected").mkdir()
    (upstream / "selected" / "b.txt").write_bytes(b"nested input")
    (upstream / "selected-other").mkdir()
    (upstream / "selected-other" / "c.txt").write_bytes(b"outside selection")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "auth.json")
    record = service.create(
        "w", CreateSource(provider="local", root=str(upstream), label="Research inputs"), workspace
    )
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024)
    with pytest.raises(ValueError, match="before attaching"):
        source_folder_resource(service.store, resources, record, linked=linked)
    if linked:
        record = link_folder(service, record)
    else:
        operation = service.store.begin_operation(record.source.id, "materialize")
        asyncio.run(service._transfer(record, workspace, operation))
        record = service.get("w", record.source.id)
    resource = source_folder_resource(
        service.store, resources, record, linked=linked, folder=folder
    )
    assert (
        source_folder_resource(service.store, resources, record, linked=linked, folder=folder).id
        == resource.id
    )
    index = json.loads(resources.content_path(resource).read_text(encoding="utf-8"))
    selected_path = "selected/b.txt" if folder else "a.txt"
    assert index["entries"][0]["path"] == selected_path
    assert index["folder"] == folder
    if folder:
        assert len(index["entries"]) == 1
        assert (
            source_folder_resource(service.store, resources, record, linked=linked).id
            != resource.id
        )
        for invalid in [
            "a.txt",
            "missing",
            "../selected",
            "/selected",
            "selected/../selected-other",
        ]:
            with pytest.raises(ValueError):
                source_folder_resource(
                    service.store, resources, record, linked=linked, folder=invalid
                )
    assert index["linked"] is linked
    assert "locator" not in json.dumps(index)
    assert str(upstream) not in json.dumps(index)
    assert resource.connected_source["kind"] == "folder"
    app = SimpleNamespace(
        state=SimpleNamespace(
            resource_store=resources,
            connected_storage=service,
            sessions={"s": SimpleNamespace(workspace_id="w")},
        )
    )
    part = SimpleNamespace(
        type="resource_ref",
        resource_id=resource.id,
        resource_revision=str(resource.revision),
        name="Research inputs",
    )
    grounding = describe_resource_parts(app, "s", [part])
    label = f"Research inputs / {folder}" if folder else "Research inputs"
    assert f"Attached folder {label!r}" in grounding[0]
    assert f"folder={folder!r}" in grounding[0]
    assert record.source.id in grounding[0] and index["revision"] in grounding[0]
    assert "connected_data_open" in grounding[0]
    monkeypatch.setattr(setup_tool.context, "active_app", lambda: app)
    monkeypatch.setattr(setup_tool.context, "active_session_id", lambda: "s")
    listing = setup_tool.connected_data_open(
        record.source.id, revision=index["revision"], linked=linked, folder=folder
    )
    assert listing["entries"] == index["entries"]
    opened = setup_tool.connected_data_open(
        record.source.id, selected_path, revision=index["revision"], linked=linked, folder=folder
    )
    assert resources.content_path(resources.get("w", opened["resource_id"])).read_bytes() == (
        b"nested input" if folder else b"real input"
    )
    if folder:
        with pytest.raises(ValueError, match="within the attached folder"):
            setup_tool.connected_data_open(
                record.source.id, "selected-other/c.txt", folder=folder, linked=linked
            )
    with pytest.raises(ValueError, match="changed since"):
        setup_tool.connected_data_open(record.source.id, revision="stale", linked=linked)
    if linked:
        service.disconnect(record)
    else:
        service.remove_copy(record, workspace)
    with pytest.raises(ValueError):
        setup_tool.connected_data_open(record.source.id, linked=linked)
    assert resources.content_path(resource).exists()
    app.state.sessions["s"].workspace_id = "other"
    with pytest.raises(KeyError):
        setup_tool.connected_data_open(record.source.id, linked=linked)
