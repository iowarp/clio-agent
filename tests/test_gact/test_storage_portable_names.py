"""Provider names that are not portable are listed under safe aliases, never failing a source."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from fsspec.implementations.local import LocalFileSystem
from pydantic import ValidationError

from clio_agent.gact.storage.adapters import LocalSource, safe_child
from clio_agent.gact.storage.desktop_upload import UploadedSourceFile
from clio_agent.gact.storage.drive import DriveSource
from clio_agent.gact.storage.linked import FsspecFolder
from clio_agent.gact.storage.models import FileEntry
from clio_agent.gact.storage.names import (
    check_remote_relative,
    plain_error,
    portable_name,
    resolve_remote,
)
from clio_agent.gact.storage.ssh_target_probe import selected_path
from tests.test_gact.storage_operation_helpers import storage_world

REVIEW = "<<REVIEW QUESTIONS>>.pdf"
posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows cannot create these names")


DRIVE_FILES = [
    {"id": "f1", "name": REVIEW, "mimeType": "application/pdf", "version": "1", "size": "3"},
    {"id": "f2", "name": "dup.txt", "mimeType": "text/plain", "version": "1", "size": "1"},
    {"id": "f3", "name": "dup.txt", "mimeType": "text/plain", "version": "1", "size": "1"},
    {"id": "f4", "name": "1/2 plan", "mimeType": "application/vnd.google-apps.document"},
    {"id": "f5", "name": "Form", "mimeType": "application/vnd.google-apps.form"},
    {"id": "f6", "name": "Link", "mimeType": "application/vnd.google-apps.shortcut"},
]


def test_portable_names_pass_through_and_unsafe_names_get_stable_aliases() -> None:
    assert portable_name("report.pdf") == "report.pdf"
    alias = portable_name(REVIEW)
    assert alias is not None and alias == portable_name(REVIEW)
    assert alias.startswith("__REVIEW QUESTIONS__~") and alias.endswith(".pdf")
    FileEntry(path=alias, kind="file")
    for name in ["a:b", "CON.txt", "trailing. ", 'q"?*|.tar.gz', "tab\there", "a\\b", "a/b"]:
        mapped = portable_name(name)
        assert mapped is not None and mapped != name
        FileEntry(path=mapped, kind="file")
    assert portable_name("a?b") != portable_name("a*b")
    assert portable_name("a.pdf", "id-1") != portable_name("a.pdf", "id-2")
    assert portable_name("x" * 400 + "?.pdf") is not None
    for name in ["", ".", ".."]:
        assert portable_name(name) is None


@pytest.mark.parametrize("value", ["../x", "/abs", "a/../b", "a//b", "a/./b", "a\0b"])
def test_traversal_and_absolute_remote_paths_stay_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        check_remote_relative(value)
    with pytest.raises(ValidationError):
        FileEntry(path="safe.txt", remote_path=value, kind="file")
    if ".." in value or value.startswith("/"):
        with pytest.raises(ValidationError):
            FileEntry(path=value, kind="file")


def test_file_entry_keeps_the_provider_name_only_when_it_differs() -> None:
    plain = FileEntry(path="a.txt", remote_path="a.txt", kind="file")
    assert plain.remote_path == "" and "remote_path" not in plain.model_dump()
    aliased = FileEntry(path=str(portable_name(REVIEW)), remote_path=REVIEW, kind="file")
    assert aliased.source_path == REVIEW
    assert FileEntry.model_validate(aliased.model_dump()).remote_path == REVIEW


def test_resolve_remote_looks_up_only_aliased_components() -> None:
    listed: list[str] = []

    def children(folder: str) -> list[str]:
        listed.append(folder)
        return {"": ["a:b", "plain"], "a:b": [REVIEW, "x.txt"]}[folder]

    portable = f"{portable_name('a:b')}/{portable_name(REVIEW)}"
    assert resolve_remote(portable, children) == f"a:b/{REVIEW}"
    assert resolve_remote("plain/new.txt", children) == "plain/new.txt"
    assert listed == ["", "a:b"]


def test_plain_error_never_returns_pydantic_reports() -> None:
    with pytest.raises(ValidationError) as caught:
        FileEntry(path="../x", kind="file")
    assert plain_error(caught.value) == "Source entry has an unsafe relative path"
    assert plain_error(ValueError("plain")) == "plain"


@posix_only
def test_local_source_lists_reads_and_writes_back_unportable_names(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "a:b").mkdir(parents=True)
    (root / REVIEW).write_bytes(b"%PDF review")
    (root / "a:b" / "CON.txt").write_text("inner")
    (root / "ok.txt").write_text("ok")
    adapter = LocalSource(root, writable=True)
    rows = {row.path: row for row in adapter.entries()}
    review, folder = str(portable_name(REVIEW)), str(portable_name("a:b"))
    inner = f"{folder}/{portable_name('CON.txt')}"
    assert set(rows) == {review, folder, inner, "ok.txt"}
    assert rows[review].remote_path == REVIEW and rows[inner].remote_path == "a:b/CON.txt"
    assert rows["ok.txt"].remote_path == "" and adapter.skipped == 0
    with adapter.open_read(rows[inner]) as stream:
        assert stream.read() == b"inner"
    edited = tmp_path / "edited"
    edited.write_text("changed")
    adapter.apply(inner, edited, rows[inner].revision)
    assert (root / "a:b" / "CON.txt").read_text() == "changed"
    adapter.apply(f"{folder}/new.txt", edited, None)
    assert (root / "a:b" / "new.txt").read_text() == "changed"
    with pytest.raises(ValueError, match="unsafe"):
        adapter.apply("../escape.txt", edited, None)


@posix_only
def test_fsspec_folder_browses_into_aliased_folders_and_writes_original_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "linked"
    (root / "Q?A").mkdir(parents=True)
    (root / "Q?A" / REVIEW).write_text("before")
    folder = FsspecFolder(LocalFileSystem(), str(root), writable=True)
    alias = str(portable_name("Q?A"))
    rows = folder.entries(alias, recursive=False)
    assert [(row.path, row.remote_path) for row in rows] == [
        (f"{alias}/{portable_name(REVIEW)}", f"Q?A/{REVIEW}")
    ]
    with folder.open_read(rows[0]) as stream:
        assert stream.read() == b"before"
    edited = tmp_path / "edited"
    edited.write_text("after")
    folder.apply(rows[0].path, edited, rows[0].revision)
    assert (root / "Q?A" / REVIEW).read_text() == "after"


def test_drive_lists_unportable_duplicate_and_unsupported_items_per_entry() -> None:
    files = DRIVE_FILES

    def request(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/files/folder"):
            return httpx.Response(
                200, json={"id": "folder", "mimeType": "application/vnd.google-apps.folder"}
            )
        if req.url.path.endswith("/files"):
            return httpx.Response(200, json={"files": files})
        assert req.url.path.endswith("/files/f1")
        return httpx.Response(200, content=b"pdf")

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        adapter = DriveSource("folder", "token", client=client)
        rows = adapter.entries()
        assert adapter.skipped == 2
        assert sorted(row.source_path for row in rows) == [
            "1/2 plan.pdf",
            REVIEW,
            "dup.txt",
            "dup.txt",
        ]
        duplicates = sorted(row.path for row in rows if row.locator in {"f2", "f3"})
        assert len(set(duplicates)) == 2 and all(path.endswith(".txt") for path in duplicates)
        by_remote = {row.source_path: row for row in rows}
        for row in rows:
            FileEntry(path=row.path, kind="file")
        with adapter.open_read(by_remote[REVIEW]) as stream:
            assert stream.read() == b"pdf"


class _GDriveLike:
    """The fields gdrive-fsspec's ls reports, for the same folder DriveSource lists."""

    protocol = "gdrive"

    def ls(self, path: str, detail: bool = True) -> list[dict[str, object]]:
        assert path == "root/folder"
        return [
            {
                "name": f"{path}/{item['name']}",
                "id": item["id"],
                "mimeType": item["mimeType"],
                "type": "file",
                "size": item.get("size"),
            }
            for item in DRIVE_FILES
        ]


def test_drive_browse_and_download_list_the_same_portable_paths() -> None:
    """Browse/link (gdrive-fsspec) and download (DriveSource) must agree on every path."""

    def request(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/files/folder"):
            return httpx.Response(
                200, json={"id": "folder", "mimeType": "application/vnd.google-apps.folder"}
            )
        return httpx.Response(200, json={"files": DRIVE_FILES})

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        download = DriveSource("folder", "token", client=client)
        downloaded = sorted((row.path, row.source_path) for row in download.entries())
    browse = FsspecFolder(_GDriveLike(), "root/folder")
    assert sorted((row.path, row.source_path) for row in browse.entries()) == downloaded
    assert browse.skipped == download.skipped == 2


def test_case_variant_siblings_get_distinct_reversible_aliases(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README").write_text("upper")
    try:
        (root / "readme").write_text("lower")
    except OSError:
        pytest.skip("case-insensitive filesystem")
    if len(list(root.iterdir())) == 1:
        pytest.skip("case-insensitive filesystem")
    folder = FsspecFolder(LocalFileSystem(), str(root))
    rows = folder.entries()
    assert sorted(row.source_path for row in rows) == ["README", "readme"]
    assert len({row.path.casefold() for row in rows}) == 2
    for row in rows:
        assert resolve_remote(row.path, folder._children) == row.source_path


def test_github_commit_validation_accepts_repository_names_but_not_traversal() -> None:
    from clio_agent.gact.storage.github_filesystem import WritableGithubFileSystem

    fs = object.__new__(WritableGithubFileSystem)
    fs.org, fs.repo, fs.token = "o", "r", "t"
    with pytest.raises(PermissionError, match="workflows"):
        fs.commit_files([("docs/a:b.md", b"x", None), (".github/workflows/x.yml", b"x", None)])
    with pytest.raises(ValueError):
        fs.commit_files([("../x", b"x", None)])


def test_desktop_upload_accepts_unportable_names_and_stores_aliases() -> None:
    item = UploadedSourceFile(path=f"docs/{REVIEW}", resource_id="r", revision=1)
    assert item.portable == f"docs/{portable_name(REVIEW)}"
    for value in ["../x", "/x", "a//b", "a\\b"]:
        with pytest.raises(ValidationError):
            UploadedSourceFile(path=value, resource_id="r", revision=1)


@posix_only
def test_ssh_probe_reads_posix_names_with_colons(tmp_path: Path) -> None:
    (tmp_path / "a:b").write_text("x")
    assert selected_path(tmp_path.resolve(), "a:b") == tmp_path.resolve() / "a:b"
    with pytest.raises(ValueError):
        selected_path(tmp_path.resolve(), "../x")


@posix_only
def test_http_browse_search_copy_and_attach_with_unportable_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure: one such name made browse and search fail for the source."""
    from clio_agent.gact.resource_custody import ResourceStore

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    upstream, workspace = tmp_path / "drive-like", tmp_path / "workspace"
    (upstream / "Reviews").mkdir(parents=True)
    workspace.mkdir()
    (upstream / "Reviews" / REVIEW).write_bytes(b"%PDF questions")
    (upstream / "notes.txt").write_text("notes")
    alias = f"Reviews/{portable_name(REVIEW)}"
    with storage_world(tmp_path, workspace) as (app, client, wid, sid):
        app.state.resource_store = ResourceStore(
            root=tmp_path / "resources", max_resource_bytes=1 << 20
        )
        created = client.post(
            f"/v1/workspaces/{wid}/sources",
            json={
                "provider": "local",
                "root": str(upstream),
                "label": "Drive",
                "mode": "read_only",
            },
        )
        assert created.status_code == 201, created.text
        prefix = f"/v1/workspaces/{wid}/sources/{created.json()['id']}"
        listing = client.post(prefix + "/browse", json={"folder": "Reviews"})
        assert listing.status_code == 200, listing.text
        (row,) = listing.json()["entries"]
        assert (row["path"], row["remote_path"], row["size"]) == (alias, f"Reviews/{REVIEW}", 14)
        assert "notice" not in listing.json()
        found = client.post(prefix + "/browse", json={"query": "review questions"})
        assert [row["path"] for row in found.json()["entries"]] == [alias]
        assert client.post(prefix + "/transfer", json={"session_id": sid}).status_code == 202
        for _ in range(200):
            operations = client.get(prefix + "/operations").json()["operations"]
            if operations[-1]["state"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert operations[-1]["state"] == "completed", operations
        copy = Path(client.get(f"/v1/workspaces/{wid}/sources").json()["sources"][0]["local_path"])
        assert (copy / alias).read_bytes() == b"%PDF questions"
        assert not any(char in path.name for path in copy.rglob("*") for char in "<>")
        attached = client.post(prefix + "/reference", json={"path": alias})
        assert attached.status_code == 200, attached.text
        unsafe = client.post(prefix + "/reference", json={"path": "../outside"})
        assert unsafe.status_code == 409
        assert unsafe.json()["error"]["message"] == "Select a file in the approved source snapshot"
        assert "validation error" not in unsafe.text and "Value error" not in unsafe.text
        assert safe_child(copy, alias).is_file()
