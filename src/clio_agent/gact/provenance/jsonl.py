"""Native append-only JSONL provenance provider."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.provenance.protocol import ProviderReceipt
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.semantic_trace_file import FileSemanticTraceBackend


class JsonlProvenanceProvider:
    """Preserve the existing trace JSONL bytes behind the provider contract."""

    name = "jsonl"
    durable = True
    queryable = True
    # Explicit (not just the Protocol default): flush() genuinely proxies
    # through to FileSemanticTraceBackend's real drain below, so a
    # returning flush() IS a synchronous-persistence barrier.
    flush_durable = True
    flush_note = ""

    def __init__(self, path: Path) -> None:
        self.path = path
        # Artifact-registry boot replay consumes the same durable journal.  Keep
        # this provider-owned instead of teaching the registry about dispatcher
        # internals; a dispatcher can aggregate these roots across providers.
        self.replay_paths = (path,)
        self._backend = FileSemanticTraceBackend(path)

    def emit(self, event: SemanticEvent) -> ProviderReceipt:
        """Append the event using the established off-loop writer."""
        self._backend.emit(event)
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        """Block until every enqueued write has actually reached disk.

        :meth:`emit` only hands the event to the shared off-loop
        ``FileSemanticTraceBackend`` writer queue (near-zero work on the
        caller); the write itself happens later, on the process-global
        writer thread. Proxy through so a caller that awaits THIS
        provider's flush (e.g. :meth:`ProvenanceDispatcher.flush`) gets a
        real persistence guarantee, not just "handed off to the next
        queue" (see :class:`FileSemanticTraceBackend`).
        """
        self._backend.flush()

    def close(self) -> None:
        """Drain pending JSONL writes."""
        self._backend.close()

    def session_events(self, session_id: str) -> Iterator[dict[str, Any]]:
        """Read complete session records for context recovery, without projection."""
        return self._backend.session_events(session_id)

    def query_execution(
        self,
        *,
        session_id: str,
        child_session_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        """Read released native history from the append-only journal."""
        self._backend.flush()
        session_ids = {session_id, *child_session_ids}
        events: list[dict[str, Any]] = []
        if self.path.suffix.lower() in FileSemanticTraceBackend._FILE_SUFFIXES:
            paths = [self.path]
        else:
            paths = [self.path / f"{sid}.semantic.jsonl" for sid in sorted(session_ids)]
        for path in paths:
            for event in _JOURNAL_EVENTS.events(path):
                if str(event.get("session_id") or "") in session_ids:
                    events.append(event)
        return normalize_semantic_events(
            events,
            provider="native",
            session_id=session_id,
            limit=limit,
        )


#: Source bytes of parsed journals kept in memory; least recently read files go first.
_JOURNAL_CACHE_MAX_BYTES = 64 * 1024 * 1024


@dataclass
class _ParsedJournal:
    identity: tuple[int, int]
    offset: int = 0
    head: bytes = b""
    events: list[dict[str, Any]] = field(default_factory=list)


#: Leading bytes compared on every read: a journal rewritten in place keeps its
#: identity and may not shrink, but its first event (and event id) changes.
_JOURNAL_HEAD_BYTES = 256


class _JournalEventCache:
    """Parsed journal events per file, extended by the appended bytes only.

    The journal is append-only, so re-reading and re-parsing a whole session on every
    execution query cost time proportional to the session's history even for a
    ``limit=1`` read. A file whose identity changed, that shrank or whose leading
    bytes changed (deleted and recreated, rewritten) is parsed again from the start.
    Only complete lines are consumed; a line still being written is read on the next
    query.
    """

    def __init__(self, max_bytes: int = _JOURNAL_CACHE_MAX_BYTES) -> None:
        self._lock = threading.Lock()
        self._journals: OrderedDict[Path, _ParsedJournal] = OrderedDict()
        self._max_bytes = max_bytes

    def events(self, path: Path) -> list[dict[str, Any]]:
        try:
            stat = path.stat()
        except OSError:
            stat = None
        with self._lock:
            if stat is None or not path.is_file():
                self._journals.pop(path, None)
                return []
            identity = (stat.st_dev, stat.st_ino)
            journal = self._journals.get(path)
            with path.open("rb") as stream:
                head = stream.read(_JOURNAL_HEAD_BYTES)
                if (
                    journal is None
                    or journal.identity != identity
                    or stat.st_size < journal.offset
                    or not head.startswith(journal.head)
                ):
                    journal = _ParsedJournal(identity)
                    self._journals[path] = journal
                self._journals.move_to_end(path)
                if stat.st_size > journal.offset:
                    stream.seek(journal.offset)
                    chunk = stream.read(stat.st_size - journal.offset)
                    complete = chunk.rfind(b"\n") + 1
                    for line in chunk[:complete].splitlines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            journal.events.append(event)
                    journal.offset += complete
                    journal.head = head[: journal.offset]
            events = list(journal.events)
            self._evict()
        return events

    def _evict(self) -> None:
        cached = sum(journal.offset for journal in self._journals.values())
        while cached > self._max_bytes and len(self._journals) > 1:
            _, evicted = self._journals.popitem(last=False)
            cached -= evicted.offset


_JOURNAL_EVENTS = _JournalEventCache()
