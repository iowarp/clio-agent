"""Characterize installed upstream backends without writing to users' accounts."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fsspec.implementations.cached import SimpleCacheFileSystem
from fsspec.implementations.github import GithubFileSystem
from fsspec.implementations.local import LocalFileSystem
from fsspec.spec import AbstractFileSystem
from gdrive_fsspec import GoogleDriveFileSystem
from gdrive_fsspec.core import GoogleDriveFile
from globusfs import GlobusFileSystem
from globusfs.credentials import StaticToken


@pytest.fixture
def globus_backend() -> Iterator[tuple[GlobusFileSystem, dict[str, bytes], list[str]]]:
    """Run the real globusfs HTTP methods against a disposable loopback endpoint."""
    files: dict[str, bytes] = {}
    methods: list[str] = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_PUT(self) -> None:
            methods.append("PUT")
            assert self.headers.get("Authorization") == "Bearer probe-token"
            files[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201)
            self.end_headers()

        def do_GET(self) -> None:
            methods.append("GET")
            self.send_response(200 if self.path in files else 404)
            self.end_headers()
            self.wfile.write(files.get(self.path, b""))

        def do_DELETE(self) -> None:
            methods.append("DELETE")
            files.pop(self.path, None)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def info(collection: str, path: str) -> dict[str, Any]:
        key = "/" + path.lstrip("/")
        if key not in files:
            raise FileNotFoundError(path)
        return {"name": collection + key, "size": len(files[key]), "type": "file"}

    fs = GlobusFileSystem(
        collection_id="collection",
        https_url=f"http://127.0.0.1:{server.server_port}",
        credentials=StaticToken("probe-token"),
        metadata=SimpleNamespace(info=info),
        skip_instance_cache=True,
    )
    try:
        yield fs, files, methods
    finally:
        if fs._session is not None:
            fs.close_session(fs.loop, fs._session)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_globus_fsspec_put_replace_read_delete(
    globus_backend: tuple[GlobusFileSystem, dict[str, bytes], list[str]],
) -> None:
    fs, files, methods = globus_backend
    fs.pipe_file("collection/data.txt", b"first")
    assert fs.cat_file("collection/data.txt") == b"first"
    fs.pipe_file("collection/data.txt", b"second")
    assert fs.cat_file("collection/data.txt") == b"second"
    assert len(files) == 1
    fs.rm_file("collection/data.txt")
    assert files == {}
    assert methods == ["PUT", "GET", "PUT", "GET", "DELETE"]


def test_globus_direct_pipe_is_not_deferred_by_fsspec_transaction(
    globus_backend: tuple[GlobusFileSystem, dict[str, bytes], list[str]],
) -> None:
    fs, files, _ = globus_backend
    with pytest.raises(RuntimeError, match="cancel"):
        with fs.transaction:
            fs.pipe_file("collection/data.txt", b"published immediately")
            assert files["/data.txt"] == b"published immediately"
            raise RuntimeError("cancel")
    assert files["/data.txt"] == b"published immediately"
    with pytest.raises(NotImplementedError):
        fs.open("collection/data.txt", "wb")


def test_simplecache_globus_requires_correct_upload_method(
    tmp_path: Path, globus_backend: tuple[GlobusFileSystem, dict[str, bytes], list[str]]
) -> None:
    """The generic cache calls put_file, not Globus's authenticated pipe_file."""
    from aiohttp import InvalidUrlClientError

    fs, files, _ = globus_backend
    cached = SimpleCacheFileSystem(fs=fs, cache_storage=str(tmp_path / "cache"))
    with pytest.raises(InvalidUrlClientError):
        with cached.open("collection/data.txt", "wb") as stream:
            stream.write(b"cached edit")
    assert files == {}


@pytest.mark.parametrize("commit", [True, False])
def test_local_fsspec_defers_until_transaction_completion(tmp_path: Path, commit: bool) -> None:
    fs = LocalFileSystem(skip_instance_cache=True)
    destination = tmp_path / "data.txt"
    destination.write_bytes(b"before")
    transaction = fs.start_transaction()
    with fs.open(str(destination), "wb") as stream:
        stream.write(b"after")
    assert destination.read_bytes() == b"before"
    transaction.complete(commit=commit)
    assert destination.read_bytes() == (b"after" if commit else b"before")


def test_simplecache_keeps_edits_local_until_explicit_commit(tmp_path: Path) -> None:
    destination = tmp_path / "data.txt"
    destination.write_bytes(b"before")
    fs = SimpleCacheFileSystem(
        fs=LocalFileSystem(skip_instance_cache=True), cache_storage=str(tmp_path / "cache")
    )
    transaction = fs.start_transaction()
    with fs.open(str(destination), "wb") as stream:
        stream.write(b"after")
    assert destination.read_bytes() == b"before"
    transaction.complete(commit=True)
    assert destination.read_bytes() == b"after"


def test_github_upstream_write_mode_is_unimplemented() -> None:
    # _open refuses before making a request, independent of repository permissions.
    fs = object.__new__(GithubFileSystem)
    with pytest.raises(NotImplementedError):
        fs._open("file.txt", "wb")


def test_drive_upload_initialization_creates_instead_of_overwriting() -> None:
    """Execute the upstream method at its HTTP seam, including an existing target ID."""
    calls: list[dict[str, Any]] = []

    def request(url: str, **kwargs: Any) -> tuple[dict[str, str], bytes]:
        calls.append({"url": url, **kwargs})
        return {"status": "200", "location": "https://example.invalid/upload"}, b""

    fs = SimpleNamespace(
        info=lambda path: {"id": "parent" if path == "folder" else "existing-file"},
        _parent=lambda path: path.rsplit("/", 1)[0],
        files=SimpleNamespace(_http=SimpleNamespace(request=request)),
    )
    file = object.__new__(GoogleDriveFile)
    file.fs, file.path = fs, "folder/existing.txt"
    file.closed = True  # Avoid a destructor upload; this test exercises initiation only.
    file._initiate_upload()
    assert calls[0]["method"] == "POST"
    assert "/files?" in calls[0]["url"]
    assert json.loads(calls[0]["body"]) == {"name": "existing.txt", "parents": ["parent"]}


@pytest.mark.parametrize("commit", [True, False])
def test_drive_transaction_commits_but_discard_uses_missing_method(commit: bool) -> None:
    """Use the real Drive file and transaction, replacing only the remote HTTP seam."""
    calls: list[dict[str, Any]] = []

    def request(url: str, **kwargs: Any) -> tuple[dict[str, str], bytes]:
        calls.append({"url": url, **kwargs})
        if kwargs["method"] == "POST":
            return {"status": "200", "location": "https://example.invalid/?upload_id=probe"}, b""
        if kwargs["headers"]["Content-Range"].endswith("/*"):
            return {"status": "308", "range": "bytes=0-5"}, b""
        return {"status": "200"}, json.dumps(
            {"id": "created", "name": "new.txt", "size": "6"}
        ).encode()

    # Bypass login only; filesystem, cache invalidation, buffered upload and
    # transaction code are the installed dependency's real implementation.
    fs = object.__new__(GoogleDriveFileSystem)
    AbstractFileSystem.__init__(fs)
    fs.files = SimpleNamespace(_http=SimpleNamespace(request=request))
    fs.info = lambda path: {"id": "parent", "name": path, "type": "directory"}
    fs.dircache["folder"] = []
    transaction = fs.start_transaction()
    with fs.open("folder/new.txt", "wb") as stream:
        stream.write(b"staged")
    assert calls[-1]["headers"]["Content-Range"] == "bytes 0-5/*"
    if commit:
        transaction.complete(commit=True)
        assert calls[-1]["headers"]["Content-Range"] == "bytes */6"
        assert fs.dircache["folder"][0]["id"] == "created"
        assert fs.dircache["folder"][0]["size"] == 6
    else:
        # The upstream discard method calls a filesystem method that does not exist.
        with pytest.raises(AttributeError, match="_call"):
            transaction.complete(commit=False)
