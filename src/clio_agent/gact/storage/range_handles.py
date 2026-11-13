"""Byte-range handles: read a linked-source file where it lives, only the bytes needed.

``connected_data_open`` with ``range`` mints a handle for one file of a linked source:
an unguessable, short-lived id bound to the calling session, the source, the link's
manifest and the file's revision. CLIO serves it on loopback
(:mod:`clio_agent.gact.routes.source_ranges`) as an HTTP resource that honours
``Range``, so h5py, xarray or an MCP server read it through fsspec and move only the
ranges they touch. The handle carries no credential: the read goes through the
source's host-side adapter, so keys never leave CLIO. Every read is counted, which is
how a run reports the bytes it moved.

Providers whose adapter cannot seek answer ``range_unsupported``.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord

if TYPE_CHECKING:
    from clio_agent.gact.storage.service import StorageService

#: Typed reasons for a refused range request.
RANGE_UNSUPPORTED = "range_unsupported"
RANGE_HANDLE_EXPIRED = "range_handle_expired"
#: How long a handle answers (seconds); a script asks for a new one after that.
HANDLE_TTL_S = 3600
#: Providers whose files can be read by byte range.
RANGE_PROVIDERS = frozenset({"s3", "local"})

Reader = Callable[[int, int], bytes]


@dataclass
class RangeHandle:
    """One file of one linked source, readable by range for one session."""

    id: str
    session_id: str
    workspace_id: str
    source_id: str
    manifest_id: str
    path: str
    revision: str
    size: int
    provider: str
    expires_at: float
    read: Reader = field(repr=False)
    bytes_served: int = 0
    requests: int = 0

    def stats(self) -> dict[str, Any]:
        return {
            "handle": self.id,
            "source_id": self.source_id,
            "path": self.path,
            "revision": self.revision,
            "size": self.size,
            "bytes_served": self.bytes_served,
            "requests": self.requests,
            "expires_in_s": max(0, int(self.expires_at - time.time())),
        }


class RangeHandles:
    """The live handles of one server process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, RangeHandle] = {}

    def add(self, handle: RangeHandle) -> RangeHandle:
        with self._lock:
            now = time.time()
            for key in [k for k, h in self._handles.items() if h.expires_at < now]:
                del self._handles[key]
            self._handles[handle.id] = handle
        return handle

    def get(self, session_id: str, handle_id: str) -> RangeHandle:
        with self._lock:
            handle = self._handles.get(handle_id)
        if handle is None or handle.session_id != session_id:
            raise KeyError(handle_id)
        if handle.expires_at < time.time():
            raise TimeoutError(f"{RANGE_HANDLE_EXPIRED}: open the file with range again")
        return handle

    def served(self, handle: RangeHandle, n_bytes: int) -> None:
        with self._lock:
            handle.bytes_served += n_bytes
            handle.requests += 1

    def for_session(self, session_id: str) -> list[RangeHandle]:
        with self._lock:
            return [h for h in self._handles.values() if h.session_id == session_id]


def registry(app: Any) -> RangeHandles:
    """The app's handle registry, created on first use."""
    handles = getattr(app.state, "source_range_handles", None)
    if handles is None:
        handles = RangeHandles()
        app.state.source_range_handles = handles
    return handles


def range_origin(app: Any) -> str:
    """The loopback URL prefix the range route answers under (``…/v1/sessions``)."""
    from clio_agent.server_port import claimed_port, configured_port  # noqa: PLC0415

    port = getattr(app.state, "api_port", None) or claimed_port() or configured_port()
    return f"http://127.0.0.1:{port}/v1/sessions"


def handle_url(app: Any, handle: RangeHandle) -> str:
    return f"{range_origin(app)}/{handle.session_id}/source-ranges/{handle.id}"


def _reader(service: StorageService, record: SourceRecord, entry: FileEntry) -> Reader:
    """A function reading ``length`` bytes at ``start`` of ``entry`` through its adapter."""
    from clio_agent.gact.storage.linked import linked_adapter  # noqa: PLC0415

    if record.source.provider == "s3":
        with linked_adapter(service, record) as adapter:
            reader = adapter.range_reader(entry)  # holds the source's own client
        return reader.read_range

    def read(start: int, length: int) -> bytes:
        with linked_adapter(service, record) as adapter, adapter.open_read(entry) as stream:
            stream.seek(start)
            return stream.read(length)

    return read


def mint(
    service: StorageService,
    record: SourceRecord,
    relative: str,
    *,
    session_id: str,
    handles: RangeHandles,
) -> RangeHandle:
    """A handle for ``relative`` in ``record``'s current link, or a typed refusal."""
    if record.source.provider not in RANGE_PROVIDERS:
        raise ValueError(
            f"{RANGE_UNSUPPORTED}: {record.source.provider} sources cannot be read by byte range; "
            "open the file without range (whole-file read within the size limit) or download it"
        )
    if not record.connected or not record.linked_manifest_id:
        raise ValueError("Link or reconnect this source before reading its files")
    manifest = service.store.get("manifest", record.linked_manifest_id, Manifest)
    if manifest.source_id != record.source.id:
        raise ValueError("Linked folder ownership mismatch")
    FileEntry(path=relative, kind="file")
    entry = next((r for r in manifest.entries if r.path == relative and r.kind == "file"), None)
    if entry is None:
        raise ValueError("Select a file in the linked folder")
    return handles.add(
        RangeHandle(
            id="rng_" + secrets.token_urlsafe(32),
            session_id=session_id,
            workspace_id=record.source.workspace_id or "",
            source_id=record.source.id,
            manifest_id=manifest.id,
            path=entry.path,
            revision=entry.revision,
            size=int(entry.size or 0),
            provider=record.source.provider,
            expires_at=time.time() + HANDLE_TTL_S,
            read=_reader(service, record, entry),
        )
    )
