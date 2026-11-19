"""The agent releases a source's older downloads; the current copy stays readable."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.release_tool import connected_data_release, release_presentation
from clio_agent.gact.storage.service import StorageService


def _download(service: StorageService, record, workspace: Path, paths: list[str]):  # type: ignore[no-untyped-def]
    op = service.store.begin_operation(record.source.id, "materialize")
    op = service.store.update_operation(op.id, selected_paths=paths)
    asyncio.run(service.execute_transfer(record, workspace, op))
    return service.get("w", record.source.id)


@pytest.fixture
def downloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "big.bin").write_bytes(b"b" * 50_000)
    (upstream / "small.bin").write_bytes(b"s" * 1_000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = StorageService(tmp_path / "store", tmp_path / "private" / "auth.json")
    record = service.create(
        "w", CreateSource(provider="local", label="Field data", root=str(upstream)), workspace
    )
    record = _download(service, record, workspace, ["big.bin"])
    first = record.manifest_id
    (upstream / "big.bin").write_bytes(b"c" * 60_000)  # changed upstream: fetched again
    record = _download(service, record, workspace, ["big.bin", "small.bin"])
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions={"sess_a": SimpleNamespace(workspace_id="w")}, connected_storage=service
        )
    )
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr("clio_agent.gact.context.active_session_id", lambda: "sess_a")
    (service.store.root / record.source.id / "stage-op_gone").mkdir()  # an interrupted transfer
    return service, record, first


def test_older_copies_go_and_the_current_one_stays(downloaded) -> None:  # type: ignore[no-untyped-def]
    service, record, first = downloaded
    owner = service.store.root / record.source.id
    result = connected_data_release(record.source.id)
    kinds = {row["copy"]: row["kind"] for row in result["released"]}
    assert kinds == {first: "earlier download", "stage-op_gone": "unfinished transfer"}
    assert result["kept"] == [{"copy": record.manifest_id, "kind": "current download"}]
    assert result["freed_bytes"] == 50_000  # the changed file's old bytes, no more
    current = owner / record.manifest_id
    assert (current / "big.bin").read_bytes() == b"c" * 60_000
    assert oct((current / "big.bin").stat().st_mode & 0o777) == "0o444"  # read-only again
    assert result["source_bytes"] == 61_000 and result["free_bytes"] > 0
    again = connected_data_release(record.source.id)
    assert again["released"] == [] and again["freed_bytes"] == 0


def test_a_running_transfer_is_kept(downloaded) -> None:  # type: ignore[no-untyped-def]
    service, record, _ = downloaded
    op = service.store.begin_operation(record.source.id, "materialize")
    service.store.update_operation(op.id, state="running")
    (service.store.root / record.source.id / f"stage-{op.id}").mkdir()
    result = connected_data_release(record.source.id)
    assert {"copy": f"stage-{op.id}", "kind": "transfer in progress"} in result["kept"]


def test_presentation_says_what_was_freed_and_kept() -> None:
    result = {
        "label": "Konza flux tower " * 4,
        "released": [{"copy": "snapshot_1", "kind": "earlier download"}],
        "kept": [{"copy": "snapshot_2", "kind": "current download"}],
        "freed_bytes": 1_243_366_859,
        "source_bytes": 2_000_000_000,
        "free_bytes": 5_000_000_000,
    }
    shown = release_presentation({"source_id": "source_x"}, result, result)
    assert shown["summary"].startswith("Release older downloads of Konza flux tower")
    assert "1.2 GB freed" in shown["summary"]
    text = " ".join(block["text"] for block in shown["blocks"])
    assert "snapshot_1" in text and "released" in text and "kept" in text
    missing = release_presentation({"source_id": "source_x"}, {"message": "boom"}, None)
    assert missing["blocks"][0]["severity"] == "warning"


def test_files_released_from_the_current_copy_free_their_bytes(downloaded) -> None:  # type: ignore[no-untyped-def]
    from clio_agent.gact.storage.models import Manifest

    service, record, _ = downloaded
    record.download_access = "read_only"  # a read-only download, as NDP sources are
    service.store.put("source", record.source.id, record)
    result = connected_data_release(record.source.id, paths=["big.bin"])
    assert {"copy": "big.bin", "kind": "file of the current download"} in result["released"]
    assert result["freed_bytes"] == 110_000  # the older and the current big.bin
    current = service.store.root / record.source.id / record.manifest_id
    assert not (current / "big.bin").exists() and (current / "small.bin").exists()
    manifest = service.store.get("manifest", record.manifest_id, Manifest)
    assert set(manifest.hashes) == {"small.bin"}
    assert "big.bin" not in {row.path for row in manifest.entries}


def test_unknown_paths_and_working_copies_are_refused(downloaded) -> None:  # type: ignore[no-untyped-def]
    service, record, _ = downloaded
    record.download_access = "editable"
    service.store.put("source", record.source.id, record)
    with pytest.raises(ValueError, match="editable working copy"):
        connected_data_release(record.source.id, paths=["big.bin"])
    record.download_access = "read_only"
    service.store.put("source", record.source.id, record)
    with pytest.raises(ValueError, match="Not in the current download: nope.bin"):
        connected_data_release(record.source.id, paths=["nope.bin"])
