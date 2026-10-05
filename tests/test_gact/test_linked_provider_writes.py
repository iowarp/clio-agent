"""CLIO's provider repairs exercise real fsspec methods at isolated network seams."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fsspec import AbstractFileSystem

from clio_agent.gact.storage.globus_download import GlobusDownload
from clio_agent.gact.storage.linked import _ApprovedDrive, _DriveFile
from clio_agent.gact.storage.models import FileEntry
from tests.test_gact.test_fsspec_backend_contracts import globus_backend  # noqa: F401


def test_globus_adapter_create_replace_delete_and_readonly(
    tmp_path: Path,
    globus_backend: Any,  # noqa: F811
) -> None:
    fs, files, methods = globus_backend
    adapter = object.__new__(GlobusDownload)
    adapter.collection = "collection"
    adapter.fs = fs
    record = SimpleNamespace(linked_access="write_through", source=SimpleNamespace(root="/"))
    adapter.source = SimpleNamespace(
        record=record,
        entries=lambda: [
            FileEntry(path=path.lstrip("/"), kind="file", size=len(data), revision=data.hex())
            for path, data in files.items()
        ],
    )
    content = tmp_path / "bytes"
    content.write_bytes(b"first")
    assert adapter.apply("data.txt", content, None) == b"first".hex()
    content.write_bytes(b"second")
    adapter.apply("data.txt", content, b"first".hex())
    assert files == {"/data.txt": b"second"}
    with pytest.raises(ValueError, match="changed"):
        adapter.apply("data.txt", content, b"first".hex())
    record.linked_access = "read_only"
    with pytest.raises(PermissionError):
        adapter.apply("data.txt", None, b"second".hex())
    record.linked_access = "write_through"
    adapter.apply("data.txt", None, b"second".hex())
    assert files == {} and methods == ["PUT", "PUT", "DELETE"]


@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("existing", [True, False])
def test_drive_replacement_preserves_identity_and_discard_cancels_session(
    commit: bool, existing: bool
) -> None:
    calls: list[dict[str, Any]] = []
    remote = {"existing-id": b"before"} if existing else {}
    upload = bytearray()

    def request(url: str, **kwargs: Any) -> tuple[dict[str, str], bytes]:
        calls.append({"url": url, **kwargs})
        method = kwargs["method"]
        if method in {"POST", "PATCH"}:
            assert method == ("PATCH" if existing else "POST")
            if existing:
                assert "/files/existing-id?" in url
            return {"status": "200", "location": "https://example.invalid/?upload_id=test"}, b""
        if method == "DELETE":
            assert "upload_id=test" in url
            return {"status": "499"}, b""
        upload.extend(kwargs.get("body") or b"")
        if kwargs["headers"]["Content-Range"].endswith("/*"):
            return {"status": "308", "range": "bytes=0-4"}, b""
        identifier = "existing-id" if existing else "created-id"
        remote[identifier] = bytes(upload)
        return {"status": "200"}, json.dumps(
            {"id": identifier, "name": "data.txt", "size": "5"}
        ).encode()

    fs = object.__new__(_ApprovedDrive)
    AbstractFileSystem.__init__(fs)
    fs.writable = True
    fs.files = SimpleNamespace(_http=SimpleNamespace(request=request))

    def info(path: str) -> dict[str, Any]:
        if path == "folder":
            return {"id": "parent", "name": path, "type": "directory"}
        if existing:
            return {"id": "existing-id", "name": path, "type": "file"}
        raise FileNotFoundError(path)

    fs.info = info
    fs.dircache["folder"] = [{"id": "existing-id", "name": "folder/data.txt"}] if existing else []
    transaction = fs.start_transaction()
    with fs.open("folder/data.txt", "wb") as stream:
        stream.write(b"after")
    assert remote == ({"existing-id": b"before"} if existing else {})
    transaction.complete(commit=commit)
    assert remote == (
        {"existing-id" if existing else "created-id": b"after"}
        if commit
        else {"existing-id": b"before"}
        if existing
        else {}
    )
    if commit:
        assert len(fs.dircache["folder"]) == 1
    else:
        assert calls[-1]["method"] == "DELETE"


def test_drive_native_document_refuses_byte_replacement() -> None:
    fs = SimpleNamespace(
        _parent=lambda path: "folder",
        info=lambda path: {"id": "document", "mimeType": "application/vnd.google-apps.document"},
    )
    file = object.__new__(_DriveFile)
    file.closed = True
    file.fs, file.path = fs, "folder/document"
    with pytest.raises(PermissionError, match="Google documents"):
        file._initiate_upload()
