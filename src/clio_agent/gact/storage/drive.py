"""Google Drive file adapter with explicit export and optimistic revision checks."""

from __future__ import annotations

import io
import posixpath
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal, cast
from urllib.parse import urlparse

import httpx
from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.storage.models import FileEntry

_API = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
_FOLDER = "application/vnd.google-apps.folder"
_EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def drive_folder_id(value: str) -> str:
    """Extract a folder ID from a Drive folder link without following arbitrary URLs."""
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc == "drive.google.com":
        match = re.fullmatch(r"/(?:drive/(?:u/\d+/)?)?folders/([A-Za-z0-9_-]+)/?", parsed.path)
        if match:
            return match.group(1)
    raise ValueError("Paste a Google Drive folder link or folder ID")


class _ResponseStream(io.RawIOBase):
    """Bounded binary reader that owns its HTTP response until closed."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.chunks = response.iter_bytes(64 * 1024)
        self.pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self.pending:
            self.pending = next(self.chunks, b"")
        amount = min(len(buffer), len(self.pending))
        buffer[:amount] = self.pending[:amount]
        self.pending = self.pending[amount:]
        return amount

    def close(self) -> None:
        self.response.close()
        super().close()


class DriveSource:
    """Browse and copy approved Drive folders without exposing token-bearing URLs."""

    def __init__(
        self, root: str, token: str, *, writable: bool = False, client: httpx.Client | None = None
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", root):
            raise ValueError("Select a Google Drive folder ID")
        self.root = root
        self.writable = writable
        self._headers = {"Authorization": "Bearer " + token}
        self._client = client or httpx.Client(timeout=60, follow_redirects=False)
        self._owns_client = client is None
        self._files: dict[str, dict[str, Any]] = {}
        self._folders = {"": root}
        metadata = self._json(
            "GET",
            f"{_API}/files/{root}",
            params={"fields": "id,mimeType", "supportsAllDrives": "true"},
        )
        if metadata.get("mimeType") != _FOLDER:
            raise ValueError("Select a Google Drive folder")
        self.capabilities = SourceCapabilities(
            search=True,
            revision_check=True,
            conditional_write=False,
            supported_modes=["read_only", "working_copy"],
            unavailable_reasons={
                "write_enabled": "Google Drive file access does not provide a writable operating-system folder on this node."
            },
        )

    def close(self) -> None:
        """Close the privately authenticated HTTP connection."""
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not response.is_success:
            code = response.status_code
            response.close()
            raise ValueError(
                f"Google Drive refused the operation (HTTP {code}); check sign-in and folder access"
            )

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, url, headers=self._headers, **kwargs)
            self._check(response)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Google Drive returned invalid metadata")
            return data
        except httpx.HTTPError as exc:
            raise ValueError("Google Drive could not be reached") from exc

    def entries(
        self,
        *,
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[FileEntry]:
        """List all selected descendants and identify native document exports explicitly."""
        rows: list[FileEntry] = []
        self._files = {}
        self._folders = {"": self.root}
        pending = [(self.root, "")]
        while pending:
            folder, parent = pending.pop()
            page = ""
            while True:
                if cancelled is not None and cancelled():
                    raise InterruptedError("Drive folder indexing cancelled")
                parameters = {
                    "q": f"'{folder}' in parents and trashed = false",
                    "pageSize": "1000",
                    "fields": "nextPageToken,files(id,name,mimeType,size,version,modifiedTime,sha256Checksum)",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if page:
                    parameters["pageToken"] = page
                data = self._json("GET", _API + "/files", params=parameters)
                for metadata in data.get("files", []):
                    if cancelled is not None and cancelled():
                        raise InterruptedError("Drive folder indexing cancelled")
                    if len(rows) >= 100_000:
                        raise ValueError("Select a smaller Drive folder (limit 100,000 entries)")
                    name, mime = str(metadata["name"]), str(metadata["mimeType"])
                    if "/" in name or "\\" in name:
                        raise ValueError(
                            "A Drive filename contains a path separator; rename it before copying"
                        )
                    if mime in _EXPORTS:
                        name += _EXPORTS[mime][1]
                    elif mime.startswith("application/vnd.google-apps.") and mime != _FOLDER:
                        raise ValueError(
                            "The selected folder contains an unsupported Drive shortcut or document type"
                        )
                    path = posixpath.join(parent, name)
                    if path in self._files or path in self._folders:
                        raise ValueError(
                            "The Drive folder contains duplicate filenames; select or rename distinct files"
                        )
                    kind: Literal["directory", "file"] = "directory" if mime == _FOLDER else "file"
                    rows.append(
                        FileEntry(
                            path=path,
                            kind=kind,
                            size=int(metadata.get("size", 0)),
                            revision=str(metadata.get("version", metadata.get("modifiedTime", ""))),
                            locator=str(metadata["id"]),
                            sha256=metadata.get("sha256Checksum"),
                        )
                    )
                    if kind == "directory":
                        self._folders[path] = metadata["id"]
                        pending.append((metadata["id"], path))
                    else:
                        self._files[path] = metadata
                    if progress is not None:
                        progress(len(rows))
                page = data.get("nextPageToken", "")
                if not page:
                    break
        return sorted(rows, key=lambda row: row.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Stream a Drive binary or an explicitly selected native-document export."""
        metadata = self._files.get(entry.path)
        if not metadata or metadata["id"] != entry.locator:
            raise ValueError("This Drive file no longer belongs to the selected snapshot")
        mime = metadata["mimeType"]
        url = f"{_API}/files/{metadata['id']}"
        params = {"alt": "media", "supportsAllDrives": "true"}
        if mime in _EXPORTS:
            url += "/export"
            params = {"mimeType": _EXPORTS[mime][0]}
        try:
            response = self._client.send(
                self._client.build_request("GET", url, params=params, headers=self._headers),
                stream=True,
            )
            self._check(response)
            return cast(BinaryIO, io.BufferedReader(_ResponseStream(response)))
        except httpx.HTTPError as exc:
            raise ValueError("Google Drive download could not be started") from exc

    def _parent(self, path: str) -> str:
        parent = str(PurePosixPath(path).parent)
        if parent == ".":
            return self.root
        accumulated = ""
        for part in PurePosixPath(parent).parts:
            next_path = posixpath.join(accumulated, part)
            if next_path not in self._folders:
                row = self._json(
                    "POST",
                    _API + "/files",
                    params={"supportsAllDrives": "true"},
                    json={
                        "name": part,
                        "mimeType": _FOLDER,
                        "parents": [self._folders[accumulated]],
                    },
                )
                self._folders[next_path] = row["id"]
            accumulated = next_path
        return self._folders[parent]

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Update one binary after a fresh version check; native exports cannot be written back."""
        FileEntry(path=path, kind="file")
        if not self.writable:
            raise PermissionError("This Drive source has not been approved for writes")
        current = {entry.path: entry for entry in self.entries() if entry.kind == "file"}.get(path)
        if (current.revision if current else None) != expected:
            raise ValueError(f"Upstream changed after review: {path}")
        metadata = self._files.get(path)
        if metadata and metadata["mimeType"] in _EXPORTS:
            raise ValueError(
                "Native Google document exports are read-only; edit the original in Google Workspace"
            )
        if content is None:
            if metadata:
                # A reviewed deletion moves the source to Trash, retaining the
                # provider's recovery mechanism instead of permanently deleting it.
                self._json(
                    "PATCH",
                    f"{_API}/files/{metadata['id']}",
                    params={"supportsAllDrives": "true"},
                    json={"trashed": True},
                )
            return None
        method = "PATCH" if metadata else "POST"
        endpoint = f"{_UPLOAD}/files" + (f"/{metadata['id']}" if metadata else "")
        body = (
            {} if metadata else {"name": PurePosixPath(path).name, "parents": [self._parent(path)]}
        )
        try:
            response = self._client.request(
                method,
                endpoint,
                headers={**self._headers, "X-Upload-Content-Length": str(content.stat().st_size)},
                params={
                    "uploadType": "resumable",
                    "supportsAllDrives": "true",
                    "fields": "id,version",
                },
                json=body,
            )
            self._check(response)
            location = response.headers.get("Location", "")
            parsed = urlparse(location)
            if parsed.scheme != "https" or parsed.hostname not in {
                "www.googleapis.com",
                "content.googleapis.com",
            }:
                raise ValueError("Google Drive returned an invalid upload destination")
            with content.open("rb") as reader:
                result = self._client.put(
                    location,
                    headers={**self._headers, "Content-Length": str(content.stat().st_size)},
                    content=iter(lambda: reader.read(1024 * 1024), b""),
                )
            self._check(result)
            return str(result.json()["version"])
        except httpx.HTTPError as exc:
            raise ValueError(
                "Google Drive upload failed; review the source before retrying"
            ) from exc
