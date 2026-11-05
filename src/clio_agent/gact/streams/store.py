"""Streams: artifacts whose latest content lives in the server's memory (CT22, CT25).

A stream is a named artifact of a workspace (``<name>.parquet``) that a script writes to
many times a second. Its rows live here: the latest row, or the last ``window`` rows when
the stream declares a window. Views bound to the artifact's name at ``@latest`` read the
rows from memory while the stream is known to this server and are told when they change
(:mod:`.push`); the rows are written to the artifact store as the artifact's next version
on open, then every ``flush_s`` while they change, and when the stream stops. After a
restart, a view reads the last version written, and the stream reads as stopped.

One flush of a stream runs at a time (its ``flush_lock``), whoever asks: the timer, a
stop, a dashboard's publish. A flush that fails leaves the rows unsaved for the next one,
and is recorded on the stream as ``flush_error`` (``STREAM_FLUSH_FAILED``, traced). Only
``open`` refuses on it; a stop, a script's end and shutdown stop the stream regardless
and announce it, so a failed write never keeps a stream open or a script from settling.

The memory copy is the stream's content; the flushed versions are its persistence. Where a
stream's content lives was decided in CT25 (5): GACT holds it until clio-core can place a
blob in memory (``docs/design/clio-core-requests.md``, issue 7).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from clio_agent.runtime import trace

#: A stream's name: what its artifact is called, without the ``.parquet`` it gets.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
#: Typed failure: a flush could not write the stream's next version.
STREAM_FLUSH_FAILED = "stream_flush_failed"
#: What a failed version write raises (storage, Arrow, a typed StreamError, minting).
FLUSH_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, RuntimeError, TypeError)


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
    #: The write count the last saved version holds (set only once the write succeeded).
    flushed_seq: int = -1
    #: When the last flush was attempted (monotonic), which paces the next one.
    flushed_at: float = 0.0
    artifact_id: str = ""
    #: The last flush's failure (``reason``, ``code``, ``message``, ``at``); None once saved.
    flush_error: dict[str, str] | None = None
    #: The background script that opened it, which stops it when it ends ("" for none).
    task: str = ""
    timer: threading.Timer | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: Held across one version write, so two flushes never write the same file at once.
    flush_lock: threading.Lock = field(default_factory=threading.Lock)

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
                "flush_error": dict(self.flush_error) if self.flush_error else None,
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
        """Open ``name`` for writing, or reopen it (same session, or a stopped stream).

        Reopening a stream that is open keeps its writer: its key, and the background
        script that owns it unless ``task`` names one. Reopening a stopped stream starts a
        new writer: a fresh key (the old writer's no longer writes) and ``task`` as its
        owner. A version is written before the open is answered, so a view can bind to the
        name; when none can be and none was saved before, the open is refused
        (``stream_flush_failed``) and nothing stays registered or changed.
        """
        import re  # noqa: PLC0415

        if not re.match(NAME_PATTERN, name or ""):
            raise StreamError(
                "stream_name_invalid",
                "A stream name is letters, digits, '-', '_' or '.', up to 80 characters.",
            )
        if window < 1:
            raise StreamError("stream_window_invalid", "window is the number of rows kept, ≥ 1.")
        ceiling = window_max()
        if window > ceiling:
            raise StreamError(
                "stream_window_too_large",
                f"window keeps at most {ceiling:,} rows (a view reads at most that many).",
            )
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
            with stream.lock:
                before = (stream.session_id, stream.state, stream.stopped_at, stream.token)
                previous_task = stream.task
                if existing is not None and existing.state != "open":
                    stream.token = secrets.token_urlsafe(16)  # a new writer: the old key ends
                    stream.task = task
                elif task:
                    stream.task = task  # a script reopening the stream owns it from now
                stream.session_id, stream.state, stream.stopped_at = session_id, "open", ""
                if stream.rows.maxlen != window:
                    stream.window, stream.rows = window, deque(stream.rows, maxlen=window)
                for column in columns or ():
                    if column not in stream.columns:
                        stream.columns.append(column)
            self._streams[stream.id] = stream
            self._names[(workspace_id, name)] = stream.id
        try:
            self.flush(stream)  # a view can be bound to the name as soon as it is open
        except FLUSH_ERRORS as exc:
            if stream.artifact_id:  # a saved version is there to bind to; flush_error says
                return stream
            self._undo_open(stream, existing is None, before, previous_task)
            raise StreamError(
                STREAM_FLUSH_FAILED, f"Stream {name!r} could not be saved, so it is not open: {exc}"
            ) from exc
        return stream

    def _undo_open(
        self, stream: Stream, created: bool, before: tuple[str, str, str, str], task: str
    ) -> None:
        """A refused open leaves no trace: a new stream is dropped, a reopened one restored."""
        if created:
            self._forget(stream)
            return
        with stream.lock:
            stream.session_id, stream.state, stream.stopped_at, stream.token = before
            stream.task = task

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
        """Stop the stream: its last rows stay readable and are written as a version.

        A version write that fails does not keep the stream open: it is stopped and
        announced, and ``flush_error`` says the last rows were not saved."""
        stream = self.get(stream_id)
        if stream is None:
            raise StreamError("stream_not_found", f"No stream {stream_id!r} on this server.")
        if token != stream.token and session_id != stream.session_id:
            raise StreamError("stream_not_owned", "Only the stream's session stops it.")
        self._stop(stream)
        return stream

    def _stop(self, stream: Stream) -> bool:
        """Stop ``stream`` (the caller checked who asks); False when it was not open."""
        from clio_agent.gact.streams.push import announce, forget  # noqa: PLC0415

        with stream.lock:
            if stream.state == "stopped":
                return False
            stream.state, stream.stopped_at = "stopped", _now()
            if stream.timer is not None:
                stream.timer.cancel()
                stream.timer = None
        self._flush_recorded(stream)
        announce(self.app, stream, now=True)
        forget(stream.id)  # a stopped stream changes no more: its watch is done
        return True

    def stop_session(self, session_id: str) -> int:
        """The session was deleted: stop the streams it writes and forget them.

        Their last rows were written as their artifact's version, which views read from
        now on; how many were open."""
        with self._lock:
            owned = [s for s in self._streams.values() if s.session_id == session_id]
        stopped = sum(1 for stream in owned if self._stop(stream))
        for stream in owned:
            self._forget(stream)
        return stopped

    def stop_task(self, handle: str) -> int:
        """Stop the open streams a background script opened: it ended, so they did.

        Never raises for a failed version write (``flush_error`` records it), so a
        script's end is never turned into a failure by its streams."""
        with self._lock:
            owned = [s for s in self._streams.values() if handle and s.task == handle]
        for stream in owned:
            self._stop(stream)
        return len(owned)

    def close(self) -> None:
        """A clean shutdown writes every open stream's rows as its last version.

        Every stream is stopped even when one fails; the failures are raised together
        after the last, for the shutdown's cleanup report."""
        with self._lock:
            streams = list(self._streams.values())
        failures: list[Exception] = []
        for stream in streams:
            try:
                self._stop(stream)
            except Exception as exc:  # noqa: BLE001 - forwards: raised together below
                failures.append(exc)
        if failures:
            raise ExceptionGroup("streams failed to stop at shutdown", failures)

    def _forget(self, stream: Stream) -> None:
        from clio_agent.gact.streams.push import forget  # noqa: PLC0415

        with self._lock:
            if self._streams.get(stream.id) is stream:
                del self._streams[stream.id]
            key = (stream.workspace_id, stream.name)
            if self._names.get(key) == stream.id:
                del self._names[key]
        forget(stream.id)

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
            if stream.timer is not None or stream.flush_s <= 0 or stream.state != "open":
                return
            wait = max(0.0, stream.flushed_at + stream.flush_s - time.monotonic())
            stream.timer = threading.Timer(wait, self._flush_due, args=(stream,))
            stream.timer.daemon = True
            stream.timer.start()

    def _flush_due(self, stream: Stream) -> None:
        with stream.lock:
            stream.timer = None
        if not self._flush_recorded(stream):
            self._schedule_flush(stream)  # the rows are still unsaved: try again next interval

    def _flush_recorded(self, stream: Stream) -> bool:
        """Flush; a failure is recorded on the stream and traced, not raised."""
        try:
            self.flush(stream)
        except FLUSH_ERRORS:
            return False  # flush() recorded it
        return True

    def flush(self, stream: Stream) -> str:
        """Write the rows as the artifact's next version (unchanged rows: nothing).

        One flush of a stream at a time. The rows count as saved only once the write
        succeeded; a failure is recorded as ``flush_error`` (and traced), then raised."""
        from clio_agent.gact.streams.persist import write_version  # noqa: PLC0415

        with stream.flush_lock:
            with stream.lock:
                if stream.flushed_seq == stream.seq and stream.artifact_id:
                    return stream.artifact_id
                rows, columns, seq = list(stream.rows), list(stream.columns), stream.seq
                stream.flushed_at = time.monotonic()
            try:
                artifact_id = write_version(self.app, stream, rows, columns)
            except FLUSH_ERRORS as exc:
                _record_flush_error(stream, exc)
                raise
            with stream.lock:
                stream.flushed_seq, stream.flush_error = seq, None
                stream.artifact_id = artifact_id or stream.artifact_id
                return stream.artifact_id


def _record_flush_error(stream: Stream, exc: BaseException) -> None:
    """Keep a failed flush on the stream (``describe`` shows it) and trace it."""
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    with stream.lock:
        stream.flush_error = {
            "reason": STREAM_FLUSH_FAILED,
            "code": code,
            "message": str(exc)[:500],
            "at": _now(),
        }
    trace.event(
        "STREAM",
        "stream flush failed reason=%s stream=%s name=%s code=%s error=%s",
        STREAM_FLUSH_FAILED,
        stream.id,
        stream.name,
        code,
        exc,
    )


def window_max() -> int:
    """The most rows a stream keeps: as many as one view's table query reads
    (``artifacts.table_query_max_rows``), since rows past that no view could show."""
    from clio_agent.gact.routes.artifact_table_query import table_query_max_rows  # noqa: PLC0415

    return max(1, int(table_query_max_rows()))


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
    "window_max",
]
