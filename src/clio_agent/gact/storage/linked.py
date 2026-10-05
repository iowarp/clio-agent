"""Read-on-demand source folders backed by existing fsspec implementations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Iterator, cast
from urllib.parse import unquote, urlsplit

import requests
from clio_schemas.connected_resources import SourceCapabilities
from filelock import FileLock
from fsspec.implementations.local import LocalFileSystem
from gdrive_fsspec import GoogleDriveFileSystem
from gdrive_fsspec.core import GoogleDriveFile
from google.auth.credentials import AnonymousCredentials
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from clio_agent.gact.storage.adapters import safe_child, staged_file
from clio_agent.gact.storage.drive import _EXPORTS
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.globus_download import download_source
from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord
from clio_agent.gact.storage.sftp import SftpSource
from clio_agent.gact.storage.transfers import snapshot_revision
from clio_agent.platform_paths import atomic_replace, win_extended_path

if TYPE_CHECKING:
    from clio_agent.gact.storage.service import StorageService


def github_location(url: str, ref: str = "") -> tuple[str, str, str, str]:
    """Accept repository/folder links, with an explicit revision override."""
    parsed = urlsplit(url.strip())
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) < 2
    ):
        raise ValueError("Paste a github.com repository or folder link")
    org, repo = parts[:2]
    repo = repo.removesuffix(".git")
    if not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in {".", ".."} for name in (org, repo)
    ):
        raise ValueError("Invalid GitHub repository link")
    folder = ""
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] != "tree":
            raise ValueError("Paste a repository or folder link, not an individual file link")
        ref = ref or parts[3]
        folder = "/".join(parts[4:])
    if folder:
        FileEntry(path=folder, kind="directory")
    if ref and (len(ref) > 256 or any(ord(char) < 32 for char in ref)):
        raise ValueError("Invalid GitHub revision")
    return org, repo, ref, folder


class _DriveFile(GoogleDriveFile):
    """Repair upload initialization and cancellation while reusing fsspec buffering."""

    def _initiate_upload(self) -> None:
        parent = self.fs._parent(self.path)
        try:
            existing = self.fs.info(self.path)
        except FileNotFoundError:
            existing = None
        if existing and existing.get("mimeType", "").startswith("application/vnd.google-apps."):
            raise PermissionError(
                "Google documents must be edited in Google Drive, not as exported files"
            )
        base = "https://www.googleapis.com/upload/drive/v3/files"
        body = (
            {}
            if existing
            else {"name": self.path.rsplit("/", 1)[-1], "parents": [self.fs.info(parent)["id"]]}
        )
        url = base + ("/" + existing["id"] if existing else "")
        headers, _ = self.fs.files._http.request(
            url + "?uploadType=resumable&supportsAllDrives=true",
            method="PATCH" if existing else "POST",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            body=json.dumps(body).encode(),
        )
        if int(headers["status"]) >= 400:
            raise OSError("Google Drive refused the upload; check file edit permission")
        self.location = headers["location"]
        # Upstream's upload finalizer appends to this cache even after invalidation.
        self.fs.dircache.setdefault(parent, [])

    def _upload_chunk(self, final: bool = False) -> bool:
        parent = self.fs._parent(self.path)
        self.fs.dircache.setdefault(parent, [])
        result = super()._upload_chunk(final=final)
        rows = self.fs.dircache.get(parent, [])
        self.fs.dircache[parent] = list({row["id"]: row for row in rows}.values())
        return bool(result)

    def discard(self) -> None:
        """Cancel the existing resumable session, never the original file."""
        if self.location is None:
            return
        headers, _ = self.fs.files._http.request(self.location, method="DELETE")
        if int(headers["status"]) not in {200, 204, 404, 410, 499}:
            raise OSError("Google Drive could not cancel the pending upload")
        self.location = None


class _ApprovedDrive(GoogleDriveFileSystem):
    """Inject the source's approved OAuth token at the library's authentication seam."""

    # fsspec checks this before __init__; passing skip_instance_cache only to
    # super().__init__ does not disable this subclass's outer instance cache.
    cachable = False

    def __init__(self, root: str, token: str | None, *, writable: bool = False) -> None:
        self._approved_token = token
        self.writable = writable and bool(token)
        super().__init__(
            root_file_id=root,
            token="cache" if token else "anon",
            access="full_control" if self.writable else "read_only",
            skip_instance_cache=True,
        )

    def _connect_cache(self) -> Credentials:
        return Credentials(token=self._approved_token)

    def connect(self, method: str | None = None) -> None:
        """Supply CLIO's application identity for public reads through the same library."""
        if method != "anon":
            super().connect(method)
            return
        api_key = os.environ.get("CLIO_STORAGE_GOOGLE_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "Public Google Drive access is not set up on this CLIO. "
                "You can sign in to Google Drive to open this folder."
            )
        self.srv = build("drive", "v3", credentials=AnonymousCredentials(), developerKey=api_key)
        self.files = self.srv.files()

    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> BinaryIO:
        if mode != "rb":
            if not self.writable:
                raise PermissionError("This Drive link is read only")
            return cast(BinaryIO, _DriveFile(self, path, mode=mode, **kwargs))
        metadata = self.info(path)
        export = _EXPORTS.get(metadata.get("mimeType", ""))
        if export:
            # The upstream export helper passes unsupported supportsAllDrives.
            # Reuse its authenticated Google client and CLIO's existing formats.
            return io.BytesIO(
                self.files.export(fileId=metadata["id"], mimeType=export[0]).execute()
            )
        return cast(BinaryIO, super()._open(path, mode=mode, **kwargs))


class FsspecFolder:
    """Common bounded metadata traversal and reads; no provider download implementation."""

    capabilities = SourceCapabilities(supported_modes=["read_only"], link_folder=True)

    def __init__(self, fs: Any, root: str, *, writable: bool = False) -> None:
        self.fs, self.root = fs, root.rstrip("/")
        self.writable = writable

    def entries(self, folder: str = "", *, recursive: bool = True) -> list[FileEntry]:
        """Index names and revisions without reading any file bodies."""
        rows: list[FileEntry] = []
        if folder:
            FileEntry(path=folder, kind="directory")
        pending = [(posixpath.join(self.root, folder) if folder else self.root, folder)]
        while pending:
            folder, relative = pending.pop()
            for item in self.fs.ls(folder, detail=True):
                name = str(item["name"]).replace("\\", "/")
                leaf = name.rstrip("/").rsplit("/", 1)[-1]
                path = posixpath.join(relative, leaf)
                kind = item.get("type")
                export = (
                    _EXPORTS.get(item.get("mimeType", ""))
                    if isinstance(self.fs, _ApprovedDrive)
                    else None
                )
                if export:
                    path += export[1]
                if (
                    kind not in {"file", "directory"}
                    or item.get("islink")
                    or (isinstance(item.get("mode"), int) and stat.S_ISLNK(item["mode"]))
                    or str(item.get("mode", "")) == "120000"
                ):
                    raise ValueError("Linked folders support regular files and directories only")
                if len(rows) >= 100_000:
                    raise ValueError("Select a smaller folder (limit 100,000 entries)")
                revision = str(
                    item.get("sha")
                    or item.get("version")
                    or item.get("mtime")
                    or item.get("modifiedTime")
                    or ""
                )
                rows.append(
                    FileEntry(
                        path=path,
                        kind=kind,
                        size=0 if export else int(item.get("size") or 0),
                        revision=revision,
                        locator=name,
                    )
                )
                if kind == "directory" and recursive:
                    pending.append((name, path))
        return sorted(rows, key=lambda row: row.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Delegate buffering and transport to fsspec."""
        return cast(BinaryIO, self.fs.open(entry.locator, "rb"))

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Use the backend's writes, with revision checks and bounded root ownership."""
        if not self.writable:
            raise PermissionError("This linked folder is read only")
        FileEntry(path=path, kind="file")
        current = next((row for row in self.entries() if row.path == path), None)
        if (current.revision if current else None) != expected:
            raise ValueError("The original changed; review this edit again")
        target = posixpath.join(self.root, path)
        if isinstance(self.fs, _ApprovedDrive) and current and current.locator != target:
            raise PermissionError(
                "Google documents must be edited in Google Drive, not as exported files"
            )
        if isinstance(self.fs, LocalFileSystem):
            target = safe_child(Path(self.root), path).as_posix()
        if content is None:
            self.fs.rm_file(target)
            return None
        self.fs.makedirs(posixpath.dirname(target), exist_ok=True)
        if isinstance(self.fs, _ApprovedDrive):
            # Reuse the fixed fsspec upload, including file-ID-preserving replacement.
            self.fs.put_file(str(content), target)
        else:
            # Local and SFTP implementations publish one staged file with rename.
            stage = posixpath.join(posixpath.dirname(target), ".clio-write-" + uuid.uuid4().hex)
            try:
                self.fs.put_file(str(content), stage)
                if isinstance(self.fs, LocalFileSystem):
                    atomic_replace(Path(stage), Path(target))
                else:
                    self.fs.mv(stage, target)
            finally:
                if self.fs.exists(stage):
                    self.fs.rm_file(stage)
        self.fs.invalidate_cache()
        return next(row.revision for row in self.entries() if row.path == path)


@contextmanager
def drive_folder(service: StorageService, record: SourceRecord) -> Iterator[FsspecFolder]:
    """Use gdrive-fsspec's anonymous mode until the folder actually requires sign-in."""
    authenticated = service.auth.connected(record)
    if not authenticated and (record.sign_in_required or record.source.mode != "read_only"):
        raise PermissionError("Sign in to Google Drive to access this folder")
    token = service.auth.token(record) if authenticated else None
    fs = None
    try:
        fs = _ApprovedDrive(record.source.root, token, writable=record.linked_access != "read_only")
        # A children query can return an empty list even when the parent is private.
        # Check the selected folder before interpreting that result as an empty folder.
        metadata = fs.files.get(
            fileId=record.source.root, fields="id,mimeType", supportsAllDrives=True
        ).execute()
        if metadata.get("mimeType") != "application/vnd.google-apps.folder":
            raise ValueError("Select a Google Drive folder")
        yield FsspecFolder(fs, "", writable=record.linked_access != "read_only")
    except HttpError as exc:
        status = int(exc.resp.status)
        try:
            detail = json.loads(exc.content).get("error", {})
            reasons = {str(item.get("reason", "")) for item in detail.get("errors", [])}
            reasons.update(str(item.get("reason", "")) for item in detail.get("details", []))
            message = str(detail.get("message", "")).casefold()
        except (ValueError, AttributeError, TypeError):
            reasons, message = set(), ""
        if status == 429 or reasons & {
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "dailyLimitExceeded",
            "quotaExceeded",
        }:
            raise ValueError("Google Drive is busy. Wait a little and try again.") from None
        if "unregistered callers" in message or reasons & {
            "keyInvalid",
            "accessNotConfigured",
            "API_KEY_INVALID",
            "SERVICE_DISABLED",
            "API_KEY_SERVICE_BLOCKED",
            "API_KEY_HTTP_REFERRER_BLOCKED",
            "API_KEY_IP_ADDRESS_BLOCKED",
        }:
            raise ValueError(
                "CLIO's Google Drive connection needs to be configured. "
                "This does not mean the folder is private."
            ) from None
        if authenticated and status == 401:
            service.auth.reject_token(record, token or "")
            raise PermissionError("Sign in to Google Drive again to access this folder") from None
        if not authenticated and status in {401, 403, 404}:
            record.sign_in_required = True
            service.store.put("source", record.source.id, record)
            raise PermissionError("Sign in to Google Drive to access this folder") from None
        raise ValueError(f"Google Drive could not access this folder (HTTP {status})") from None
    finally:
        if fs is not None:
            fs.srv.close()


@contextmanager
def linked_adapter(
    service: StorageService, record: SourceRecord, *, refresh: bool = False
) -> Iterator[Any]:
    """Reuse privately authenticated filesystems for one read or metadata operation."""
    provider = record.source.provider
    if provider == "github":
        org, repo, ref, root = github_location(record.source.root, record.configuration.github_ref)
        authenticated = service.auth.connected(record)
        if (record.sign_in_required or record.source.mode != "read_only") and not authenticated:
            raise PermissionError("Sign in to GitHub to access this repository")
        token = service.auth.token(record) if authenticated else None
        if authenticated and not record.sign_in_required:
            record.sign_in_required = True
            service.store.put("source", record.source.id, record)
        try:
            with service.github.filesystem(org, repo, ref, refresh=refresh, token=token) as fs:
                from clio_agent.gact.storage.github_source import GitHubSource

                yield GitHubSource(fs, root, writable=record.linked_access != "read_only")
        except (requests.HTTPError, FileNotFoundError) as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, requests.HTTPError) and exc.response is not None
                else 404
            )
            if status not in {401, 403, 404}:
                raise
            if status == 401 and token:
                service.auth.reject_token(record, token)
                service.github.clear()
                raise PermissionError(
                    "GitHub sign-in expired or was revoked; sign in again"
                ) from None
            if not authenticated:
                record.sign_in_required = True
                service.store.put("source", record.source.id, record)
                raise PermissionError(
                    "Sign in to GitHub to access this repository, or check its address"
                ) from None
            raise PermissionError(
                "GitHub could not access this repository or folder. Check the address and allow this repository in the CLIO GitHub App settings."
            ) from None
    elif provider == "google_drive":
        with drive_folder(service, record) as adapter:
            yield adapter
    elif provider == "local":
        yield FsspecFolder(
            LocalFileSystem(auto_mkdir=False),
            record.source.root,
            writable=record.linked_access != "read_only",
        )
    else:
        with service.adapter(record) as adapter:
            if isinstance(adapter, GlobusSource):
                with download_source(adapter, service.auth) as filesystem:
                    yield filesystem
            elif isinstance(adapter, SftpSource):
                yield FsspecFolder(
                    adapter.fs, adapter.root, writable=record.linked_access != "read_only"
                )
            else:
                raise ValueError(
                    "This connection supports downloads; folder linking needs SFTP on the CLIO host"
                )


def link_folder(service: StorageService, record: SourceRecord) -> SourceRecord:
    """Publish an approved metadata index, without downloading the folder."""
    service.require_idle(record)
    from clio_agent.gact.storage.linked_changes import pending_edits

    if pending_edits(service, record).changes:
        raise ValueError("Publish or discard local edits before refreshing this link")
    if not record.connected:
        raise ValueError("Reconnect this source before linking its folder")
    with linked_adapter(service, record, refresh=bool(record.linked_manifest_id)) as adapter:
        entries = adapter.entries()
    paths = [entry.path.casefold() for entry in entries]
    if len(paths) != len(set(paths)):
        raise ValueError("This folder contains names that collide on the CLIO host")
    manifest = Manifest(
        id="link_" + uuid.uuid4().hex,
        source_id=record.source.id,
        revision=snapshot_revision(entries),
        entries=entries,
        hashes={},
    )
    service.store.put("manifest", manifest.id, manifest)
    record.linked_manifest_id = manifest.id
    service.store.put("source", record.source.id, record)
    return record


def linked_file(service: StorageService, record: SourceRecord, relative: str) -> Path:
    """Fetch only the requested indexed file into immutable, host-owned read cache."""
    FileEntry(path=relative, kind="file")
    identity = f"{record.source.id}/{record.linked_manifest_id}/{relative}"
    lock_root = service.store.root / ".read-locks"
    lock_root.mkdir(exist_ok=True)
    with FileLock(
        lock_root / (hashlib.sha256(identity.encode()).hexdigest() + ".lock"), timeout=30
    ):
        return _linked_file(service, record, relative)


def _linked_file(service: StorageService, record: SourceRecord, relative: str) -> Path:
    """Read under the per-file lock so concurrent previews share one cached result."""
    latest = service.get(record.source.workspace_id or "", record.source.id)
    if latest.linked_manifest_id != record.linked_manifest_id:
        raise ValueError("The folder link changed; refresh Files")
    if not record.connected or not record.linked_manifest_id:
        raise ValueError("Link or reconnect this source before reading its files")
    manifest = service.store.get("manifest", record.linked_manifest_id, Manifest)
    if manifest.source_id != record.source.id:
        raise ValueError("Linked folder ownership mismatch")
    selected = next(
        (row for row in manifest.entries if row.path == relative and row.kind == "file"), None
    )
    if selected is None:
        raise ValueError("Select a file in the linked folder")
    from clio_agent.gact.storage.linked_changes import staged_file as local_edit

    edited = local_edit(service, record, relative)
    if edited is not None:
        return edited
    destination = safe_child(service.store.root / record.source.id / manifest.id, relative)
    github = record.source.provider == "github"
    # Public GitHub bytes already checked against the approved blob hash need no network read.
    if github and not record.sign_in_required and os.path.isfile(win_extended_path(destination)):
        return destination
    # Check authorization even for a cached read after credentials were removed/restarted.
    with linked_adapter(service, record) as adapter:
        if github:
            metadata = adapter.fs.info(selected.locator)
            current = (
                selected
                if (
                    metadata.get("sha") == selected.revision
                    and metadata.get("size", 0) == selected.size
                    and metadata.get("type") == "file"
                    and str(metadata.get("mode", "")) != "120000"
                )
                else None
            )
        else:
            current = next((row for row in adapter.entries() if row.path == relative), None)
        if current is None or (current.revision, current.size) != (
            selected.revision,
            selected.size,
        ):
            raise ValueError("This linked file changed. Refresh the folder before opening it")
        if os.path.isfile(win_extended_path(destination)):
            return destination
        if selected.size > 512 * 1024 * 1024:
            raise ValueError("This file exceeds the 512 MB linked-read limit")
        git_digest = hashlib.sha1(f"blob {selected.size}\0".encode())
        os.makedirs(win_extended_path(destination.parent), exist_ok=True)
        with staged_file(destination) as stage:
            total = 0
            with (
                adapter.open_read(current) as reader,
                open(win_extended_path(stage), "wb") as writer,
            ):
                while chunk := reader.read(1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ValueError("This file exceeds the linked-read limit")
                    writer.write(chunk)
                    git_digest.update(chunk)
            if selected.size and total != selected.size:
                raise ValueError("The linked file changed while reading; refresh the folder")
            if record.source.provider == "github" and git_digest.hexdigest() != selected.revision:
                raise ValueError("The GitHub file changed; refresh the folder")
            latest = service.get(record.source.workspace_id or "", record.source.id)
            if latest.linked_manifest_id != record.linked_manifest_id:
                raise ValueError("The folder link changed while reading; refresh Files")
            atomic_replace(stage, destination)
            os.chmod(win_extended_path(destination), 0o400)
    return destination
