"""Real filesystem transfers, immutable revisions, conflicts, and restart recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from clio_schemas.connected_resources import ConnectedSource, ResourceOwner

from clio_agent.gact.storage.adapters import LocalSource
from clio_agent.gact.storage.models import FileEntry, SourceRecord, TransferOperation
from clio_agent.gact.storage.review import apply_review, review_changes
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import TransferCancelled, materialize, remove_owned_tree


@pytest.fixture
def source_setup(tmp_path: Path) -> tuple[SourceStore, SourceRecord, LocalSource, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "nested").mkdir()
    (upstream / "nested" / "data.csv").write_text("x,y\n1,2\n")
    (upstream / "readme.txt").write_text("original")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SourceStore(tmp_path / "agent-data" / "connected-sources")
    adapter = LocalSource(str(upstream), writable=True)
    source = ConnectedSource(
        id="source_test",
        owner=ResourceOwner(clio_id=store.clio_id, host_id="local"),
        provider="local",
        label="OPAL input",
        root=str(upstream),
        mode="working_copy",
        workspace_id="workspace1",
        capabilities=adapter.capabilities,
    )
    record = SourceRecord(source=source, principal="owner")
    store.put("source", source.id, record)
    return store, record, adapter, workspace


def transfer(setup: tuple[SourceStore, SourceRecord, LocalSource, Path]) -> SourceRecord:
    """Run the real streaming path on a real local directory."""
    store, record, adapter, workspace = setup
    operation = store.begin_operation(record.source.id, "materialize")
    return materialize(store, record, adapter, workspace, operation)


def test_working_copy_preserves_structure_and_immutable_baseline(source_setup: tuple) -> None:
    store, record, adapter, workspace = source_setup
    record = transfer(source_setup)
    copy = Path(record.source.local_path)
    assert copy.is_relative_to(workspace)
    assert (copy / "nested" / "data.csv").read_text() == "x,y\n1,2\n"
    (copy / "readme.txt").write_text("edited")
    assert (adapter.root / "readme.txt").read_text() == "original"
    assert (
        store.root / record.source.id / record.manifest_id / "readme.txt"
    ).read_text() == "original"
    assert not (workspace / ".clio").exists()
    assert store.list("operation", TransferOperation)[0].state == "completed"


def test_attached_reference_retains_source_bytes_and_identity(source_setup: tuple) -> None:
    from clio_agent.gact.resource_custody import ResourceStore
    from clio_agent.gact.storage.references import source_resource

    store, _, _, workspace = source_setup
    record = transfer(source_setup)
    resources = ResourceStore(root=store.root.parent / "resources", max_resource_bytes=10000)
    (Path(record.source.local_path) / "readme.txt").write_text("working edit")
    resource = source_resource(store, resources, record, "readme.txt")
    assert resources.content_path(resource).read_text() == "original"
    assert resource.connected_source["id"] == record.source.id
    assert resource.connected_source["clio_id"] == store.clio_id
    assert source_resource(store, resources, record, "readme.txt").id == resource.id
    assert not (workspace / ".clio").exists()
    with pytest.raises(ValueError):
        source_resource(store, resources, record, "../readme.txt")


def test_private_and_workspace_ancestor_sources_are_rejected(tmp_path: Path) -> None:
    from clio_agent.gact.storage.models import CreateSource
    from clio_agent.gact.storage.service import StorageService

    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for root in [tmp_path, tmp_path / "private", tmp_path / "data", workspace]:
        with pytest.raises(ValueError, match="Select"):
            service.create(
                "w", CreateSource(provider="local", root=str(root), label="bad"), workspace
            )


def test_apply_only_selected_changes_and_advances_baseline(source_setup: tuple) -> None:
    store, _, adapter, _ = source_setup
    record = transfer(source_setup)
    previous_manifest = record.manifest_id
    copy = Path(record.source.local_path)
    (copy / "readme.txt").write_text("approved")
    (copy / "nested" / "data.csv").write_text("unselected")
    review = review_changes(store, record, adapter)
    identifier = apply_review(store, record, adapter, review.id, ["readme.txt"])
    assert (adapter.root / "readme.txt").read_text() == "approved"
    assert (adapter.root / "nested" / "data.csv").read_text() != "unselected"
    assert store.get("operation", identifier, TransferOperation).applied_paths == ["readme.txt"]
    assert record.manifest_id != previous_manifest
    assert (
        store.root / record.source.id / previous_manifest / "readme.txt"
    ).read_text() == "original"
    assert [row.path for row in review_changes(store, record, adapter).changes] == [
        "nested/data.csv"
    ]


@pytest.mark.parametrize("changed", ["upstream", "working_copy"])
def test_stale_review_cannot_authorize_new_bytes(source_setup: tuple, changed: str) -> None:
    store, _, adapter, _ = source_setup
    record = transfer(source_setup)
    copy = Path(record.source.local_path)
    (copy / "readme.txt").write_text("reviewed")
    review = review_changes(store, record, adapter)
    ((adapter.root if changed == "upstream" else copy) / "readme.txt").write_text("later edit")
    with pytest.raises(ValueError, match="changed after review"):
        apply_review(store, record, adapter, review.id, ["readme.txt"])
    assert (adapter.root / "readme.txt").read_text() != "reviewed"


def test_conflicts_are_reported_and_refresh_preserves_unsaved_edits(source_setup: tuple) -> None:
    store, _, adapter, workspace = source_setup
    record = transfer(source_setup)
    copy = Path(record.source.local_path)
    (copy / "readme.txt").write_text("my edit")
    (adapter.root / "readme.txt").write_text("their edit")
    review = review_changes(store, record, adapter)
    assert review.changes[0].conflict
    with pytest.raises(ValueError, match="Resolve upstream conflicts"):
        apply_review(store, record, adapter, review.id, ["readme.txt"])
    operation = store.begin_operation(record.source.id, "refresh")
    with pytest.raises(ValueError, match="local changes"):
        materialize(store, record, adapter, workspace, operation)
    assert (copy / "readme.txt").read_text() == "my edit"
    assert record.source.materialization == "ready"


def test_read_only_adapter_and_snapshot_never_apply_upstream(source_setup: tuple) -> None:
    store, record, adapter, workspace = source_setup
    record.source = record.source.model_copy(update={"mode": "read_only"})
    adapter = LocalSource(str(adapter.root))
    operation = store.begin_operation(record.source.id, "materialize")
    materialize(store, record, adapter, workspace, operation)
    with pytest.raises(PermissionError):
        adapter.apply("readme.txt", None, "anything")
    with pytest.raises(PermissionError):
        apply_review(store, record, adapter, "unknown", ["readme.txt"])
    assert Path(record.source.local_path).is_relative_to(store.root)
    assert not (workspace / "connected-data").exists()


def test_cancellation_and_restart_recovery_keep_identity_and_previous_data(
    source_setup: tuple,
) -> None:
    store, record, adapter, workspace = source_setup
    operation = store.begin_operation(record.source.id, "materialize")
    store.update_operation(operation.id, cancel_requested=True)
    with pytest.raises(TransferCancelled):
        materialize(store, record, adapter, workspace, operation)
    assert store.get("operation", operation.id, TransferOperation).state == "cancelled"
    operation = store.begin_operation(record.source.id, "materialize")
    restored = SourceStore(store.root)
    restored.recover()
    assert restored.clio_id == store.clio_id
    assert restored.get("operation", operation.id, TransferOperation).state == "interrupted"
    record = transfer((restored, record, adapter, workspace))
    assert record.source.materialization == "ready"


def test_duplicate_operations_and_cross_source_review_are_refused(source_setup: tuple) -> None:
    store, record, _, _ = source_setup
    store.begin_operation(record.source.id, "materialize")
    with pytest.raises(ValueError, match="already active"):
        store.begin_operation(record.source.id, "refresh")


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        "C:/escape",
        "a\\b",
        "CON",
        "file:stream",
        "nested/../outside",
        "tail.",
    ],
)
def test_unsafe_source_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError):
        FileEntry(path=name, kind="file")


def test_cleanup_cannot_remove_an_ownership_root_or_its_parent(source_setup: tuple) -> None:
    store, _, _, _ = source_setup
    for path in (store.root, store.root.parent):
        with pytest.raises(ValueError):
            remove_owned_tree(path, store.root)
    assert store.path.exists()
