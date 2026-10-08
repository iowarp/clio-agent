"""Adapt globusfs file reads to CLIO snapshots, retaining native Transfer jobs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, cast
from urllib.parse import urlsplit

import globus_sdk
from globusfs import GlobusFileSystem
from globusfs.credentials import CallableToken
from globusfs.transfer import TransferMetadata

from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.globus_consent import GlobusConsentRequired
from clio_agent.gact.storage.models import FileEntry


class GlobusDownload:
    """Bind the upstream filesystem to one approved source and CLIO's private vault."""

    capabilities = GlobusSource.capabilities

    def __init__(self, source: GlobusSource, auth: StorageAuth) -> None:
        self.source = source
        self.auth = auth
        self.collection = source.record.configuration.collection_id
        try:
            endpoint = source.client.get_endpoint(self.collection)
        except globus_sdk.GlobusAPIError:
            raise ValueError("Globus collection download information is unavailable") from None
        address = endpoint.get("https_server")
        if not address:
            raise ValueError(
                "This collection does not offer direct downloads. Connect receiving storage "
                "for this CLIO host to use a native Globus transfer."
            )
        url = urlsplit(address)
        if (
            endpoint.get("id") != self.collection
            or url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Globus returned an invalid collection download address")
        self.scopes = [f"https://auth.globus.org/scopes/{self.collection}/https"]
        if endpoint.get("entity_type") == "GCSv5_mapped_collection" and not endpoint.get(
            "high_assurance"
        ):
            self.scopes.append(f"https://auth.globus.org/scopes/{self.collection}/data_access")
        self._token(self.collection)
        self.fs = GlobusFileSystem(
            collection_id=self.collection,
            https_url=address.rstrip("/"),
            metadata=TransferMetadata(source.client),
            credentials=CallableToken(self._token),
            skip_instance_cache=True,
            allow_redirects=False,
        )

    def _token(self, collection: str) -> str:
        if collection != self.collection:
            raise PermissionError("The credential belongs to another collection")
        try:
            return self.auth.token(self.source.record, resource_server=collection)
        except PermissionError:
            raise GlobusConsentRequired(self.scopes) from None

    def entries(
        self,
        *,
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[FileEntry]:
        """Retain bounded native metadata traversal and special-file validation."""
        return self.source.entries(progress=progress, cancelled=cancelled)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Delegate ranged reads, buffering and backend retries to globusfs."""
        entry = FileEntry.model_validate(entry.model_dump())
        path = self.source.record.source.root.rstrip("/") + "/" + entry.path
        return cast(
            BinaryIO,
            self.fs.open(
                f"globus://{self.collection}{path}",
                "rb",
                size=entry.size,
                block_size=1024 * 1024,
                # HTTPFile's small-file path also needs the collection credential.
                headers={"Authorization": "Bearer " + self._token(self.collection)},
            ),
        )

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Delegate linked writes to globusfs's authenticated PUT and DELETE methods."""
        if self.source.record.linked_access == "read_only":
            raise PermissionError("This Globus link is read only")
        FileEntry(path=path, kind="file")
        current = next((row for row in self.entries() if row.path == path), None)
        if (current.revision if current else None) != expected:
            raise ValueError("The Globus file changed; review this edit again")
        target = f"{self.collection}{self.source.record.source.root.rstrip('/')}/{path}"
        if content is None:
            self.fs.rm_file(target)
            return None
        # HTTPFileSystem.put_file loses Globus's collection URL and credentials;
        # pipe_file is globusfs's supported authenticated replacement operation.
        self.fs.pipe_file(target, content.read_bytes())
        return next(row.revision for row in self.entries() if row.path == path)

    def close(self) -> None:
        """Release this operation's upstream HTTP session."""
        if self.fs._session is not None:
            self.fs.close_session(self.fs.loop, self.fs._session)


@contextmanager
def download_source(source: GlobusSource, auth: StorageAuth) -> Iterator[GlobusDownload]:
    """Own one library filesystem for one materialization attempt."""
    adapter = GlobusDownload(source, auth)
    try:
        yield adapter
    finally:
        adapter.close()
