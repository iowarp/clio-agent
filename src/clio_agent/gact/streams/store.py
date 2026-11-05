"""Streams: artifacts whose latest content lives in the server's memory (CT22, CT25).

A stream is a named artifact of a workspace (``<name>.parquet``) that a script writes to
many times a second. Its rows live here: the latest row, or the last ``window`` rows when
the stream declares a window. Views bound to the artifact's name at ``@latest`` read the
rows from memory while the stream is known to this server and are told when they change
(:mod:`.push`); the rows are written to the artifact store as the artifact's next version
on open, then every ``flush_s`` while they change, and when the stream stops. After a
restart, a view reads the last version written, and the stream reads as stopped.

The memory copy is the stream's content; the flushed versions are its persistence. Where a
stream's content lives was decided in CT25 (5): GACT holds it until clio-core can place a
blob in memory (``docs/design/clio-core-requests.md``, issue 7).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: A stream's name: what its artifact is called, without the ``.parquet`` it gets.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
#: Typed failure: a background flush could not write the stream's next version.
STREAM_FLUSH_FAILED = "stream_flush_failed"


class StreamError(ValueError):
    """A typed refusal (``code``) the tool or route reports as is."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cell(value: Any) -> Any:
    """A row value as a table cell: scalars stay, anything else becomes its JSON text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    import json  # noqa: PLC0415

    return json.dumps(value, default=str, ensure_ascii=False)


@dataclass
class Stream:
    """One stream's state. ``rows`` is the content; ``seq`` counts writes."""

    id: str
    workspace_id: str
    name: str
    session_id: str
    token: str
    columns: list[str]
    window: int
    flush_s: float
    rows: deque[dict[str, Any]] = field(default_factory=deque)
    seq: int = 0
    state: str = "open"
    opened_at: str = field(default_factory=_now)
    updated_at: str = ""
    stopped_at: str = ""
    flushed_seq: int = -1
    flushed_at: float = 0.0
    artifact_id: str = ""
    #: The background script that opened it, which stops it when it ends ("" for none).
    task: str = ""
    timer: threading.Timer | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def artifact_name(self) -> str:
        return f"{self.name}.parquet"

    @property
    def view_uri(self) -> str:
        from urllib.parse import quote  # noqa: PLC0415

        return f"artifact://{self.workspace_id}/{quote(self.artifact_name)}@latest"

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.rows)

    def describe(self) -> dict[str, Any]:
        """What a tool, a route and a view say about the stream (no token)."""
        with self.lock:
            return {
                "stream_id": self.id,
                "name": self.name,
                "workspace_id": self.workspace_id,
                "session_id": self.session_id,
                "state": self.state,
                "window": self.window,
                "rows": len(self.rows),
                "writes": self.seq,
                "columns": list(self.columns),
                "opened_at": self.opened_at,
                "updated_at": self.updated_at,
                "stopped_at": self.stopped_at,
                "flush_s": self.flush_s,
                "artifact_name": self.artifact_name,
                "artifact_id": self.artifact_id,
                "view_uri": self.view_uri,
            }


class StreamStore:
    """The server's streams, by id and by ``(workspace, name)``."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._streams: dict[str, Stream] = {}
        self._names: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def get(self, stream_id: str) -> Stream | None:
        with self._lock:
            return self._streams.get(stream_id)

    def by_name(self, workspace_id: str, name: str) -> Stream | None:
        """The stream whose artifact is ``name`` (with or without ``.parquet``)."""
        bare = name.removesuffix(".parquet")
        with self._lock:
            stream_id = self._names.get((workspace_id, bare))
            return self._streams.get(stream_id) if stream_id else None

    def open(
        self,
        session_id: str,
        workspace_id: str,
        name: str,
        *,
        columns: list[str] | None = None,
        window: int = 1,
        flush_s: float = 30.0,
        task: str = "",
    ) -> Stream:
        """Open ``name`` for writing, or reopen it (same session, or a stopped stream)."""
        import re  # noqa: PLC0415

        if not re.match(NAME_PATTERN, name or ""):
            raise StreamError(
                "stream_name_invalid",
                "A stream name is letters, digits, '-', '_' or '.', up to 80 characters.",
            )
        if window < 1:
            raise StreamError("stream_window_invalid", "window is the number of rows kept, ≥ 1.")
        with self._lock:
            existing = self._streams.get(self._names.get((workspace_id, name), ""))
            if existing is not None and existing.state == "open":
                if existing.session_id != session_id:
                    raise StreamError(
                        "stream_in_use",
                        f"Stream {name!r} is open in another session; stop it there first.",
                    )
            stream = existing or Stream(
                id="stream_" + secrets.token_hex(6),
                workspace_id=workspace_id,
                name=name,
                session_id=session_id,
                token=secrets.token_urlsafe(16),
                columns=list(dict.fromkeys(columns or [])),
                window=window,
                flush_s=max(0.0, flush_s),
            )
            stream.session_id, stream.state, stream.stopped_at = session_id, "open", ""
            stream.task = task
            if stream.rows.maxlen != window:
                stream.window, stream.rows = window, deque(stream.rows, maxlen=window)
            for column in columns or ():
                if column not in stream.columns:
                    stream.columns.append(column)
            self._streams[stream.id] = stream
            self._names[(workspace_id, name)] = stream.id
        self.flush(stream)  # a view can be bound to the name as soon as it is open
        return stream

    def write(self, stream_id: str, token: str, rows: list[dict[str, Any]]) -> Stream:
        """Add ``rows`` (the newest last); the oldest leave past the window."""
        stream = self._writable(stream_id, token)
        clean = [
            {str(k): _cell(v) for k, v in row.items()} for row in rows if isinstance(row, dict)
        ]
        if not clean:
            raise StreamError("stream_row_invalid", "A stream write is a dict, or a list of dicts.")
        with stream.lock:
            stream.rows.extend(clean)
            stream.seq += len(clean)
            stream.updated_at = _now()
            for row in clean:
                for column in row:
                    if column not in stream.columns:
                        stream.columns.append(column)
        from clio_agent.gact.streams.push import announce  # noqa: PLC0415

        announce(self.app, stream)
        self._schedule_flush(stream)
        return stream

    def stop(self, stream_id: str, token: str = "", *, session_id: str = "") -> Stream:
        """Stop the stream: its last rows stay readable and are written as a version."""
        stream = self.get(stream_id)
        if stream is None:
            raise StreamError("stream_not_found", f"No stream {stream_id!r} on this server.")
        if token != stream.token and session_id != stream.session_id:
            raise StreamError("stream_not_owned", "Only the stream's session stops it.")
        with stream.lock:
            if stream.state == "stopped":
                return stream
            stream.state, stream.stopped_at = "stopped", _now()
            if stream.timer is not None:
                stream.timer.cancel()
                stream.timer = None
        self.flush(stream)
        from clio_agent.gact.streams.push import announce  # noqa: PLC0415

        announce(self.app, stream, now=True)
        return stream

    def stop_session(self, session_id: str) -> int:
        """Stop every open stream ``session_id`` writes (its writer is gone)."""
        with self._lock:
            open_ = [s for s in self._streams.values() if s.session_id == session_id]
        stopped = 0
        for stream in open_:
            if stream.state == "open":
                self.stop(stream.id, stream.token)
                stopped += 1
        return stopped

    def stop_task(self, handle: str) -> int:
        """Stop the open streams a background script opened: it ended, so they did."""
        with self._lock:
            owned = [s for s in self._streams.values() if handle and s.task == handle]
        for stream in owned:
            if stream.state == "open":
                self.stop(stream.id, stream.token)
        return len(owned)

    def close(self) -> None:
        """A clean shutdown writes every open stream's rows as its last version."""
        with self._lock:
            streams = list(self._streams.values())
        for stream in streams:
            if stream.state == "open":
                self.stop(stream.id, stream.token)

    def _writable(self, stream_id: str, token: str) -> Stream:
        stream = self.get(stream_id)
        if stream is None or not secrets.compare_digest(stream.token, str(token or "")):
            raise StreamError(
                "stream_not_open",
                "This stream is not open for this script; open it with open_stream first.",
            )
        if stream.state != "open":
            raise StreamError("stream_stopped", f"Stream {stream.name!r} was stopped.")
        return stream

    def _schedule_flush(self, stream: Stream) -> None:
        with stream.lock:
            if stream.timer is not None or stream.flush_s <= 0:
                return
            wait = max(0.0, stream.flushed_at + stream.flush_s - time.monotonic())
            stream.timer = threading.Timer(wait, self._flush_due, args=(stream,))
            stream.timer.daemon = True
            stream.timer.start()

    def _flush_due(self, stream: Stream) -> None:
        with stream.lock:
            stream.timer = None
        try:
            self.flush(stream)
        except (OSError, ValueError, RuntimeError) as exc:  # the version's write failed: typed
            logger.warning(
                "stream flush failed reason=%s stream=%s error=%r",
                STREAM_FLUSH_FAILED,
                stream.id,
                exc,
            )

    def flush(self, stream: Stream) -> str:
        """Write the rows as the artifact's next version (unchanged rows: nothing)."""
        from clio_agent.gact.streams.persist import write_version  # noqa: PLC0415

        with stream.lock:
            if stream.flushed_seq == stream.seq and stream.artifact_id:
                return stream.artifact_id
            rows, columns, seq = list(stream.rows), list(stream.columns), stream.seq
            stream.flushed_seq, stream.flushed_at = seq, time.monotonic()
        artifact_id = write_version(self.app, stream, rows, columns)
        with stream.lock:
            stream.artifact_id = artifact_id or stream.artifact_id
        return stream.artifact_id


def stream_store(app: Any) -> StreamStore:
    """The app's stream store (created on first use)."""
    store = getattr(app.state, "streams", None)
    if store is None:
        store = StreamStore(app)
        app.state.streams = store
    return store


__all__ = [
    "NAME_PATTERN",
    "STREAM_FLUSH_FAILED",
    "Stream",
    "StreamError",
    "StreamStore",
    "stream_store",
]
