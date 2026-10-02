"""The durable semantic trace written as JSONL files, off the calling thread.

Split out of :mod:`clio_agent.gact.semantic_events` (its owner of the event model and
the sink): this module owns only the file backend and its one shared writer thread.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.errors import ClioError

if TYPE_CHECKING:
    from clio_agent.gact.semantic_events import SemanticEvent

__all__ = ["FileSemanticTraceBackend", "TraceWriteError"]


# ONE process-global writer thread drains ALL FileSemanticTraceBackend instances.
# Why global + started at backend CONSTRUCTION (not per-emit): starting a thread
# from the event-loop thread DURING a turn cancels the turn task under the
# anyio/TestClient portal; constructing the backend happens at build_app (off the
# turn loop), so the thread is created safely once. A single shared thread also
# avoids one-thread-per-app (the trace is on by default) blowing up under tests.
_TRACE_WRITE_QUEUE: "queue.Queue[tuple[Path, SemanticEvent] | None]" = queue.Queue()
_TRACE_WRITER_THREAD: threading.Thread | None = None
_TRACE_WRITER_LOCK = threading.Lock()
# A write the shared writer could not make, kept per trace file until reported: the next
# ``emit`` to that file or ``flush`` of its backend raises it (never a silent drop).
_TRACE_WRITE_FAILURES: dict[Path, str] = {}


class TraceWriteError(ClioError):
    """The durable semantic trace could not write an event; it was not recorded."""

    reason = "trace_write_failed"

    def __init__(self, path: Path, cause: str) -> None:
        super().__init__(
            f"the semantic trace could not write to {path}: {cause}",
            error_type=self.reason,
            details={"path": str(path), "cause": cause},
        )


def _trace_writer_loop() -> None:
    while True:
        item = _TRACE_WRITE_QUEUE.get()
        try:
            if item is None:  # wake/no-op; the shared writer is never stopped
                continue
            path, event = item
            try:
                # Serialize HERE (off the turn loop): json.dumps of a full event
                # (reasoning + tool results) is non-trivial CPU; doing it on the
                # caller's event-loop thread destabilizes turns under the portal.
                line = json.dumps(event.to_dict("full"), sort_keys=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.write("\n")
            except Exception as exc:  # noqa: BLE001 - kept and raised by the backend (typed)
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event("TRACE-WRITE", "durable trace write failed: %r", exc)
                _TRACE_WRITE_FAILURES.setdefault(path, f"{type(exc).__name__}: {exc}")
        finally:
            _TRACE_WRITE_QUEUE.task_done()


def _ensure_trace_writer() -> None:
    """Start the shared writer once, OFF the turn event loop (backend init time)."""
    global _TRACE_WRITER_THREAD  # noqa: PLW0603
    if _TRACE_WRITER_THREAD is not None:
        return
    with _TRACE_WRITER_LOCK:
        if _TRACE_WRITER_THREAD is None:
            thread = threading.Thread(
                target=_trace_writer_loop, name="SemanticTraceWriter", daemon=True
            )
            thread.start()
            _TRACE_WRITER_THREAD = thread


class FileSemanticTraceBackend:
    """Append semantic events as JSONL, written OFF the calling thread.

    If ``path`` is a directory, events are split into
    ``<session_id>.semantic.jsonl`` files. If it is a file path, all
    events append to that file.

    ``emit`` serializes the FULL event on the caller (cheap CPU) then enqueues to
    the shared writer thread, so the turn event loop is never blocked by file I/O
    (the trace is ON by default). ``flush`` blocks until the queue drains
    (tests/readers); ``close`` drains too (the shared daemon writer lives for the
    process). The durable trace always captures the FULL event; redaction/capping
    is a per-consumer projection applied elsewhere, never here.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        _ensure_trace_writer()

    # A configured path is a single JSONL file ONLY when it carries a recognised
    # trace-file extension; otherwise it is a directory of per-session files.
    # Plain ``Path.suffix`` truthiness misfires on directory paths that contain
    # dots -- e.g. a model-named grind dir ".../trace_..._qwopus3.5-9b-v3_sandiego"
    # whose ``.suffix`` is ".5-9b-v3_sandiego" -- which made the writer try to open
    # a directory as a file and silently drop every event (empty trace).
    _FILE_SUFFIXES = frozenset({".jsonl", ".json", ".ndjson", ".log"})

    def _path_for(self, event: SemanticEvent) -> Path:
        if self.path.suffix.lower() in self._FILE_SUFFIXES:
            return self.path
        return self.path / f"{event.session_id}.semantic.jsonl"

    def emit(self, event: SemanticEvent) -> None:
        # Near-zero work on the caller (which may be the turn's event-loop thread):
        # just resolve the path + enqueue. Serialization + I/O happen in the writer.
        path = self._path_for(event)
        if path in _TRACE_WRITE_FAILURES:
            raise TraceWriteError(path, _TRACE_WRITE_FAILURES[path])
        _TRACE_WRITE_QUEUE.put((path, event))

    def flush(self) -> None:
        """Block until all enqueued events have been written; raise a write that failed."""
        _TRACE_WRITE_QUEUE.join()
        for path, cause in list(_TRACE_WRITE_FAILURES.items()):
            if path == self.path or self.path in path.parents:
                raise TraceWriteError(path, cause)

    def close(self) -> None:
        """Drain pending writes (the shared daemon writer lives for the process)."""
        _TRACE_WRITE_QUEUE.join()
