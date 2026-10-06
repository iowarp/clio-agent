"""Explicit desktop folder manifests preserve paths and remain scoped to workspace custody."""

from pathlib import Path

import pytest

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.desktop_upload import UploadedSource, publish_uploaded_source
from clio_agent.gact.storage.store import SourceStore
from clio_agent.platform_paths import win_extended_path


def test_folder_upload_preserves_nested_paths_and_retries_idempotently(tmp_path: Path) -> None:
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=10000)
    store = SourceStore(tmp_path / "sources")
    first, _ = resources.create_or_resume(workspace_id="w", name="data.csv", declared_size=5)
    first = resources.append(first.id, offset=0, data=b"first")
    second, _ = resources.create_or_resume(workspace_id="w", name="data.csv", declared_size=6)
    second = resources.append(second.id, offset=0, data=b"second")
    request = UploadedSource.model_validate(
        {
            "label": "OPAL",
            "files": [
                {"path": "OPAL/clean/data.csv", "resource_id": first.id, "revision": 1},
                {"path": "OPAL/poisoned/data.csv", "resource_id": second.id, "revision": 1},
            ],
        }
    )
    source = publish_uploaded_source(
        store, resources, "w", tmp_path / "workspace", "owner", request
    )
    root = Path(source.source.local_path)
    with open(win_extended_path(root / "OPAL/clean/data.csv"), "rb") as file:
        assert file.read() == b"first"
    with open(win_extended_path(root / "OPAL/poisoned/data.csv"), "rb") as file:
        assert file.read() == b"second"
    assert source.origin == "desktop_upload"
    assert source.source.capabilities.supported_modes == ["read_only"]
    assert (
        publish_uploaded_source(
            store, resources, "w", tmp_path / "workspace", "owner", request
        ).source.id
        == source.source.id
    )
    with pytest.raises(ValueError, match="this CLIO workspace"):
        publish_uploaded_source(store, resources, "other", tmp_path / "other", "owner", request)


def test_desktop_manifest_rejects_unsafe_and_colliding_paths(tmp_path: Path) -> None:
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=100)
    store = SourceStore(tmp_path / "sources")
    resource, _ = resources.create_or_resume(workspace_id="w", name="empty", declared_size=0)
    with pytest.raises(ValueError, match="unsafe"):
        UploadedSource.model_validate(
            {
                "label": "bad",
                "files": [{"path": "../outside", "resource_id": resource.id, "revision": 1}],
            }
        )
    request = UploadedSource.model_validate(
        {
            "label": "bad",
            "files": [
                {"path": name, "resource_id": resource.id, "revision": 1}
                for name in ["Data.txt", "data.txt"]
            ],
        }
    )
    with pytest.raises(ValueError, match="colliding"):
        publish_uploaded_source(store, resources, "w", tmp_path / "workspace", "owner", request)
