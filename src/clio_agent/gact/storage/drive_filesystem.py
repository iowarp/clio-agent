"""Google Drive filesystem implementation, imported only when Drive is used."""

from __future__ import annotations

import io
import json
import os
from typing import Any, BinaryIO, cast

from gdrive_fsspec import GoogleDriveFileSystem
from gdrive_fsspec.core import GoogleDriveFile
from google.auth.credentials import AnonymousCredentials
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from clio_agent.gact.storage.drive import _EXPORTS


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
