"""Streams: artifacts whose content lives in clio-core as typed entries (CT44).

A stream is a named ``stream`` artifact of a workspace, written many times a second by a
script and read for its latest entry or its last ``window`` entries. Its content lives in
clio-core, on the tag ``<namespace>/stream``, as plain blobs until clio-core's
``clio_cte_stream`` has a Python binding (iowarp/clio-core#1342):

- ``<workspace>/<stream>/meta``: the stream's description (name, state open or closed,
  entry type, window, last sequence number, times, the writing task, the write key);
- ``<workspace>/<stream>/e/<seq % window>``: one entry, a header line (``seq``, ``at``,
  ``media_type``) then its bytes;
- ``<workspace>/name/<name>``: the stream's id, so a name finds its stream.

An entry is bytes with a media type: ``application/json`` (a record or a row),
``text/plain`` (a line) or any other type (an image, a frame; stored now, shown by later
viewers). A write puts its entries, then the meta that counts them; a read gets the meta,
then the entries it names, by sequence number. Nothing of a stream lives in this process
but the handle of a writer it opened (a write-through copy of the meta, so a write does not
read the meta first) and the views' leases (:mod:`.push`). After a restart every stream
that was open is closed ("writer_ended"): its writer did not survive the restart.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from clio_agent import conf
from clio_agent.runtime import trace

#: A stream's name: what its artifact is called.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
#: The clio-core kind (tag ``<namespace>/stream``) a stream's blobs live under.
STREAM_KIND = "stream"
JSON_TYPE = "application/json"
TEXT_TYPE = "text/plain"
#: The most entries a stream keeps and a read returns.
WINDOW_MAX = 1000
#: Typed reason: a stream was open when the server stopped, so its writer is gone.
WRITER_ENDED = "writer_ended"


class StreamError(ValueError):
    """A typed refusal (``code``) the tool or route reports as is."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stream_namespace() -> str:
    """``streams``, nested under ARC's own namespace when one is configured (tests)."""
    base = conf.resolve("arc.namespace", env="CLIO_ARC_NAMESPACE", default="", cast=conf.as_str)
    base = base.strip().strip("/")
    return f"{base}/streams" if base else "streams"


def _meta_name(workspace_id: str, stream_id: str) -> str:
    return f"{workspace_id}/{stream_id}/meta"


def _entry_name(workspace_id: str, stream_id: str, slot: int) -> str:
    return f"{workspace_id}/{stream_id}/e/{slot}"


def _index_name(workspace_id: str, name: str) -> str:
    return f"{workspace_id}/name/{name}"


def encode_entry(value: Any, media_type: str = "") -> tuple[str, bytes]:
    """``(media_type, bytes)`` for one written value: a dict or list is JSON, a str is
    text, bytes keep the media type they are given (which they must name)."""
    if isinstance(value, bytes | bytearray | memoryview):
        if not media_type or media_type in (JSON_TYPE, TEXT_TYPE):
            raise StreamError(
                "stream_entry_type_missing",
                "Binary entries name their media type, e.g. media_type='image/png'.",
            )
        return media_type, bytes(value)
    if isinstance(value, str) and media_type in ("", TEXT_TYPE):
        return TEXT_TYPE, value.encode("utf-8")
    if media_type not in ("", JSON_TYPE):
        raise StreamError(
            "stream_entry_type_mismatch",
            f"A {type(value).__name__} is written as JSON or text, not {media_type}.",
        )
    try:
        return JSON_TYPE, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StreamError("stream_entry_invalid", f"Not JSON-serializable: {exc}") from exc


def decode_entry(blob: bytes) -> dict[str, Any]:
    """One stored entry as a reader gets it: JSON and text inline, other types by size."""
    head, _, body = blob.partition(b"\n")
    header = json.loads(head)
    entry: dict[str, Any] = {
        "seq": int(header["seq"]),
        "at": header.get("at", ""),
        "media_type": header.get("media_type", ""),
        "size_bytes": len(body),
    }
    if entry["media_type"] == JSON_TYPE:
        entry["value"] = json.loads(body)
    elif entry["media_type"] == TEXT_TYPE:
        entry["text"] = body.decode("utf-8", errors="replace")
    return entry


def entry_bytes(blob: bytes) -> tuple[str, bytes]:
    """``(media_type, body)`` of one stored entry."""
    head, _, body = blob.partition(b"\n")
    return str(json.loads(head).get("media_type") or ""), body


@dataclass
class Stream:
    """One stream's description, as its meta blob holds it (the token is never shown)."""

    id: str
    workspace_id: str
    name: str
    session_id: str
    token: str
    window: int
    media_type: str = ""
    seq: int = 0
    state: str = "open"
    opened_at: str = field(default_factory=_now)
    updated_at: str = ""
    closed_at: str = ""
    closed_reason: str = ""
    task: str = ""
    artifact_id: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @property
    def view_uri(self) -> str:
        return f"artifact://{self.workspace_id}/{quote(self.name)}@latest"

    def meta(self) -> bytes:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "lock"}
        return json.dumps(data, sort_keys=True).encode("utf-8")

    @classmethod
    def from_meta(cls, blob: bytes) -> Stream:
        data = json.loads(blob)
        known = {f for f in cls.__dataclass_fields__ if f != "lock"}
        return cls(**{key: value for key, value in data.items() if key in known})

    def describe(self) -> dict[str, Any]:
        """What a tool, a route and a view say about the stream (no token)."""
        with self.lock:
            return {
                "stream_id": self.id,
                "name": self.name,
                "workspace_id": self.workspace_id,
                "session_id": self.session_id,
                "state": self.state,
                "media_type": self.media_type,
                "window": self.window,
                "held": min(self.seq, self.window),
                "writes": self.seq,
                "opened_at": self.opened_at,
                "updated_at": self.updated_at,
                "closed_at": self.closed_at,
                "closed_reason": self.closed_reason,
                "artifact_id": self.artifact_id,
                "view_uri": self.view_uri,
            }


class StreamStore:
    """The workspace streams in clio-core, and the writers this process holds."""

    def __init__(self, app: Any, backend: Any = None) -> None:
        self.app = app
        self._backend = backend
        self._writers: dict[str, Stream] = {}  # open streams this process writes
        self._lock = threading.Lock()
        self._orphans_closed = False

    # ---- clio-core --------------------------------------------------------------------

    @property
    def backend(self) -> Any:
        if self._backend is None:
            from clio_agent.arc import storage  # noqa: PLC0415 - patched by the test suite
            from clio_agent.gact.server_boot import process_arc  # noqa: PLC0415

            if process_arc(self.app) is None:
                raise StreamError(
                    "streams_unavailable", "Streams need clio-core, which this server lacks."
                )
            self._backend = storage.make_arc_store(
                namespace=stream_namespace(), kinds=(STREAM_KIND,)
            )
        if not self._orphans_closed:
            self._orphans_closed = True
            self._close_orphans()
        return self._backend

    def _put_meta(self, stream: Stream) -> None:
        self.backend.put(STREAM_KIND, _meta_name(stream.workspace_id, stream.id), stream.meta())

    def _load(self, workspace_id: str, stream_id: str) -> Stream | None:
        blob = self.backend.get(STREAM_KIND, _meta_name(workspace_id, stream_id))
        return Stream.from_meta(blob) if blob else None

    def _close_orphans(self) -> None:
        """Close every stream left open by an earlier run of the server (no writer now)."""
        for name in self._backend.query_names(STREAM_KIND, r".+/stream_[0-9a-f]+/meta"):
            blob = self._backend.get(STREAM_KIND, name)
            stream = Stream.from_meta(blob) if blob else None
            if stream is None or stream.state != "open" or stream.id in self._writers:
                continue
            stream.state, stream.closed_at, stream.closed_reason = "closed", _now(), WRITER_ENDED
            self._backend.put(STREAM_KIND, name, stream.meta())
            trace.event(
                "STREAM", "stream closed at start reason=%s stream=%s", WRITER_ENDED, stream.id
            )

    # ---- lookups ----------------------------------------------------------------------

    def get(self, stream_id: str, workspace_id: str = "") -> Stream | None:
        """The stream ``stream_id`` (the writer's handle when this process writes it)."""
        with self._lock:
            held = self._writers.get(stream_id)
        if held is not None:
            return held
        if workspace_id:
            return self._load(workspace_id, stream_id)
        for name in self.backend.query_names(STREAM_KIND, rf".+/{re.escape(stream_id)}/meta"):
            blob = self.backend.get(STREAM_KIND, name)
            if blob:
                return Stream.from_meta(blob)
        return None

    def by_name(self, workspace_id: str, name: str) -> Stream | None:
        blob = self.backend.get(STREAM_KIND, _index_name(workspace_id, name))
        return self.get(blob.decode("utf-8"), workspace_id) if blob else None

    def streams(self, workspace_id: str) -> list[Stream]:
        """The workspace's streams, newest first."""
        names = self.backend.query_names(
            STREAM_KIND, rf"{re.escape(workspace_id)}/stream_[0-9a-f]+/meta"
        )
        streams = [s for s in (self.get(n.split("/")[-2], workspace_id) for n in names) if s]
        return sorted(streams, key=lambda s: s.updated_at or s.opened_at, reverse=True)

    # ---- lifecycle --------------------------------------------------------------------

    def open(
        self,
        session_id: str,
        workspace_id: str,
        name: str,
        *,
        window: int = 1,
        media_type: str = "",
        task: str = "",
        turn_id: str = "",
    ) -> Stream:
        """Open ``name`` for writing, or reopen it.

        Reopening a stream open in this session keeps its writer (key and task, unless
        ``task`` names a new one). Reopening a closed stream starts a new writer (a fresh
        key) and keeps its entries. A stream open in another session is refused."""
        if not re.match(NAME_PATTERN, name or ""):
            raise StreamError(
                "stream_name_invalid",
                "A stream name is letters, digits, '-', '_' or '.', up to 80 characters.",
            )
        if not 1 <= window <= WINDOW_MAX:
            raise StreamError(
                "stream_window_invalid", f"window is the entries kept, 1 to {WINDOW_MAX:,}."
            )
        existing = self.by_name(workspace_id, name)
        if existing is not None and existing.state == "open":
            if existing.session_id != session_id:
                raise StreamError(
                    "stream_in_use",
                    f"Stream {name!r} is open in another session; stop it there first.",
                )
            if task:
                existing.task = task
            if existing.window != window:
                self._rewindow(existing, window)
            self._put_meta(existing)
            with self._lock:
                self._writers[existing.id] = existing
            return existing
        if existing is not None:  # closed: a new writer, the entries stay
            stream = existing
            stream.token, stream.task, stream.session_id = (
                secrets.token_urlsafe(16),
                task,
                session_id,
            )
            stream.state, stream.closed_at, stream.closed_reason = "open", "", ""
            if stream.window != window:
                self._rewindow(stream, window)
        else:
            stream = Stream(
                id="stream_" + secrets.token_hex(6),
                workspace_id=workspace_id,
                name=name,
                session_id=session_id,
                token=secrets.token_urlsafe(16),
                window=window,
                media_type=media_type,
                task=task,
            )
            stream.artifact_id = self._mint(stream, turn_id)
            self.backend.put(
                STREAM_KIND, _index_name(workspace_id, name), stream.id.encode("utf-8")
            )
        self._put_meta(stream)
        with self._lock:
            self._writers[stream.id] = stream
        return stream

    def _rewindow(self, stream: Stream, window: int) -> None:
        """Keep the newest entries under a new window (their slots move)."""
        kept = self._entries(stream, min(stream.window, window))
        for entry_seq, blob in kept:
            self.backend.put(
                STREAM_KIND, _entry_name(stream.workspace_id, stream.id, entry_seq % window), blob
            )
        stream.window = window

    def _mint(self, stream: Stream, turn_id: str = "") -> str:
        """Register the stream as a ``stream`` artifact: custody clio-core, no file.

        ``turn_id`` is the turn that opened it (directly or from its background script):
        the stream then rides that turn's answer as an artifact card, as a file the turn
        wrote does.
        """
        from clio_schemas import ArtifactKind, Custody, Mechanism  # noqa: PLC0415
        from clio_schemas.models import EvidenceClass, IdentityEvidence  # noqa: PLC0415

        from clio_agent.gact.artifacts.minting import mint_artifact_outcome  # noqa: PLC0415

        outcome = mint_artifact_outcome(
            self.app,
            stream.session_id,
            name=stream.name,
            workspace_id=stream.workspace_id,
            evidence=IdentityEvidence(
                evidence_class=EvidenceClass.AUTHORITY_ASSERTED,
                authority=f"clio-core:{stream_namespace()}/{STREAM_KIND}:{stream.id}",
            ),
            kind=ArtifactKind.STREAM,
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": "stream",
                "stream_id": stream.id,
                "session_id": stream.session_id,
            },
            custody=Custody.CLIO_CORE,
            turn_id=turn_id,
        )
        return outcome.version.artifact_id if outcome is not None else ""

    def write(self, stream_id: str, token: str, values: list[Any], media_type: str = "") -> Stream:
        """Add ``values`` (the newest last) as entries; the oldest leave past the window."""
        stream = self._writable(stream_id, token)
        entries = [encode_entry(value, media_type) for value in values]
        if not entries:
            raise StreamError("stream_entry_invalid", "A stream write needs at least one entry.")
        from clio_agent.arc.batch_put import PutRecord  # noqa: PLC0415

        with stream.lock:
            first = stream.seq + 1
            stream.seq += len(entries)
            at = _now()
            # One batch for the write's entries (their slots differ: only the newest
            # ``window`` of a large write are put, the rest would be overwritten anyway),
            # then the meta that makes them readable.
            kept = range(max(first, stream.seq - stream.window + 1), stream.seq + 1)
            records = []
            for seq in kept:
                entry_type, body = entries[seq - first]
                header = json.dumps({"seq": seq, "at": at, "media_type": entry_type})
                records.append(
                    PutRecord(
                        name=_entry_name(stream.workspace_id, stream.id, seq % stream.window),
                        data=header.encode("utf-8") + b"\n" + body,
                    )
                )
            self.backend.put_many(STREAM_KIND, records)
            stream.media_type = stream.media_type or entries[-1][0]
            stream.updated_at = _now()
            self._put_meta(stream)
        from clio_agent.gact.streams.push import announce  # noqa: PLC0415

        announce(self.app, stream)
        return stream

    def _entries(self, stream: Stream, limit: int) -> list[tuple[int, bytes]]:
        """``(seq, stored blob)`` of the newest ``limit`` entries, oldest first."""
        count = min(stream.seq, stream.window, max(1, limit))
        found: list[tuple[int, bytes]] = []
        for entry_seq in range(stream.seq - count + 1, stream.seq + 1):
            blob = self.backend.get(
                STREAM_KIND, _entry_name(stream.workspace_id, stream.id, entry_seq % stream.window)
            )
            if blob:
                found.append((entry_seq, blob))
        return found

    def read(self, stream: Stream, limit: int = WINDOW_MAX) -> list[dict[str, Any]]:
        """The newest ``limit`` entries (oldest first), read from clio-core."""
        fresh = self._load(stream.workspace_id, stream.id) or stream
        return [decode_entry(blob) for _seq, blob in self._entries(fresh, limit)]

    def entry(self, stream: Stream, seq: int) -> tuple[str, bytes] | None:
        """One entry's ``(media_type, body)``, while the window still holds it."""
        fresh = self._load(stream.workspace_id, stream.id) or stream
        if not fresh.seq - min(fresh.seq, fresh.window) < seq <= fresh.seq:
            return None
        blob = self.backend.get(
            STREAM_KIND, _entry_name(fresh.workspace_id, fresh.id, seq % fresh.window)
        )
        return entry_bytes(blob) if blob else None

    def stop(self, stream_id: str, token: str = "", *, session_id: str = "") -> Stream:
        """Close the stream: no more writes; its entries stay, shown as closed."""
        stream = self.get(stream_id)
        if stream is None:
            raise StreamError("stream_not_found", f"No stream {stream_id!r}.")
        if token != stream.token and session_id != stream.session_id:
            raise StreamError("stream_not_owned", "Only the stream's session stops it.")
        self._close(stream, "stopped")
        return stream

    def _close(self, stream: Stream, reason: str) -> bool:
        from clio_agent.gact.streams.push import announce, forget  # noqa: PLC0415

        with stream.lock:
            if stream.state != "open":
                return False
            stream.state, stream.closed_at, stream.closed_reason = "closed", _now(), reason
            self._put_meta(stream)
        with self._lock:
            self._writers.pop(stream.id, None)
        announce(self.app, stream, now=True)
        forget(stream.id)
        return True

    def stop_session(self, session_id: str) -> int:
        """The session was deleted: close the streams it writes."""
        with self._lock:
            owned = [s for s in self._writers.values() if s.session_id == session_id]
        return sum(1 for stream in owned if self._close(stream, "session_deleted"))

    def stop_task(self, handle: str) -> int:
        """A background script ended: close the streams it opened."""
        with self._lock:
            owned = [s for s in self._writers.values() if handle and s.task == handle]
        return sum(1 for stream in owned if self._close(stream, "script_ended"))

    def close(self) -> None:
        """A clean shutdown closes the streams this process writes; every one is tried."""
        with self._lock:
            streams = list(self._writers.values())
        failures: list[Exception] = []
        for stream in streams:
            try:
                self._close(stream, "server_stopped")
            except Exception as exc:  # noqa: BLE001 - forwards: raised together below
                failures.append(exc)
        if failures:
            raise ExceptionGroup("streams failed to close at shutdown", failures)

    def _writable(self, stream_id: str, token: str) -> Stream:
        with self._lock:
            stream = self._writers.get(stream_id)
        if stream is None or not secrets.compare_digest(stream.token, str(token or "")):
            raise StreamError(
                "stream_not_open",
                "This stream is not open for this script; open it with open_stream first.",
            )
        return stream


def stream_store(app: Any) -> StreamStore:
    """The app's stream store (created on first use)."""
    store = getattr(app.state, "streams", None)
    if store is None:
        store = StreamStore(app)
        app.state.streams = store
    return store


__all__ = [
    "JSON_TYPE",
    "NAME_PATTERN",
    "STREAM_KIND",
    "TEXT_TYPE",
    "WINDOW_MAX",
    "WRITER_ENDED",
    "Stream",
    "StreamError",
    "StreamStore",
    "decode_entry",
    "encode_entry",
    "entry_bytes",
    "stream_namespace",
    "stream_store",
]
