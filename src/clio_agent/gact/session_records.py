"""Session metadata records in clio-core: one record per session, a recent-first index.

:class:`~clio_agent.gact.sessions.SessionStore` used to keep every session's
metadata resident and rewrite the whole ``sessions.json`` on every change, so a
store after months of flow-heavy use paid O(sessions) per write and held every
row in memory from boot. This module owns the replacement, in the clio-core store
every other record lives in (ARC kind ``session_records``; no other store):

* **one record per session** (``row/<id>``: the session's metadata) -- a change to
  one session writes that one record;
* **an index of small columns** (workspace, parent, kind, archive bucket, status,
  title, created and interaction times) kept in :data:`INDEX_BUCKETS` bucket
  records (``index/<nn>``, a session's bucket fixed by a hash of its id). A change
  that moves an indexed column also rewrites its one bucket, about 1/64th of the
  index. The process holds the index in memory (a few hundred bytes per session,
  loaded with :data:`INDEX_BUCKETS` reads when the store is bound), ordered by
  recency, so every view is a query on it: a rail page after a keyset cursor
  (:meth:`SessionRecords.page`), a conversation's tree (:meth:`SessionRecords.family`),
  the sessions waiting on a person and their ancestors, named rows, a title search.
  Session records are read only for the rows a view returns.
* **writes in order, off the server loop.** Every record write goes through one
  writer thread (the newest body per record wins). A caller off the server loop
  waits for its write to land; a caller on the server loop does not (a clio-core
  RPC must never block the loop, ``arc.loop_guard``): its write lands with the
  writer's next batch, readers in this process see it at once, and a failed batch
  is retried with a typed audit (:data:`WRITE_FAILED_REASON`).
* **before clio-core is bound** (unit tests, History mode, the seconds before a
  server's clio-core attach) the records live in memory. A server announces that
  a store is coming (:meth:`SessionRecords.expect_store`): until it is bound, a
  read off the loop waits for it and a read on the loop is a typed retryable
  :class:`SessionRecordsNotReadyError`.

The legacy ``sessions.json`` is imported once by the ``session_records_import``
store migration (:mod:`clio_agent.gact.store_migrations`), never by opening.

The rail order is the recency the wire shows (``last_interaction_at``, else
``created_at`` -- :func:`clio_agent.gact.protocol.v3.session.session_to_v3`),
newest first, ties broken by id. Page cursors are opaque and keyset-based.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Any, Optional

from sortedcontainers import SortedList

from clio_agent import conf
from clio_agent.arc.batch_put import PutRecord
from clio_agent.errors import ClioError
from clio_agent.gact.providers.config import _model_ref_is_empty
from clio_agent.gact.session_kind import session_kind
from clio_agent.gact.transcript_file import TranscriptNotReadyError
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

#: The ARC record kind (``clio_agent.arc.storage.ARC_KINDS``).
KIND = "session_records"
#: How many index bucket records the index is split into.
INDEX_BUCKETS = 64
_ROW = "row/"
_BUCKET = "index/"

#: Typed reasons this module reports (no silent fallback).
LEGACY_IMPORTED_REASON = "session_records_legacy_imported"
LEGACY_UNREADABLE_REASON = "session_records_legacy_unreadable"
ROW_UNREADABLE_REASON = "session_record_unreadable"
WRITE_FAILED_REASON = "session_record_write_failed"
NOT_BOUND_REASON = "session_records_not_bound"
#: Seconds an off-loop read waits for the announced clio-core store to be bound.
_BIND_WAIT_SECONDS = 300.0
#: Point reads of a view's records issued together.
_READ_FANOUT = 16
#: Seconds the writer thread stays alive with nothing to write.
_IDLE_SECONDS = 5.0
#: Seconds an off-loop write waits for its batch to land before it is a typed error.
_WRITE_WAIT_SECONDS = 120.0

#: Stored statuses of a session that waits on a person (a permission or an answer).
WAITING_ON_PERSON: frozenset[str] = frozenset({"waiting_permission", "waiting_user"})


class InvalidCursorError(ValueError):
    """A page cursor that this server did not issue (or that was altered)."""

    reason = "invalid_cursor"


class SessionRecordsNotReadyError(TranscriptNotReadyError):
    """The session records' clio-core store is still attaching (a retryable 503)."""

    def __init__(self, detail: str) -> None:
        super().__init__("", NOT_BOUND_REASON, detail)


@dataclass(frozen=True)
class RecordColumns:
    """The indexed facts of one session (everything else lives in its record)."""

    id: str
    workspace_id: str
    parent_session_id: str
    kind: str
    archived: bool
    created_at: str
    recent_at: str
    interaction_known: bool
    status: str = ""
    title: str = ""
    # Counters the activity metrics sum, and the flags the controls and the memory
    # pass select by (the ``session_record_columns`` store migration fills them for
    # rows indexed before they existed).
    message_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    #: An active goal or loop (``metadata.goal`` / ``metadata.loop``).
    autonomous: bool = False
    #: An execution gate is open (``metadata.execution_gate``).
    gated: bool = False
    #: A background memory pass is in flight or has ended (``metadata.memory_pass`` /
    #: ``metadata.memory_last_pass``).
    memory_runs: bool = False
    #: The session selected its own model (``session.model``).
    model_selected: bool = False


@dataclass(frozen=True)
class PageQuery:
    """Which sessions a list or page reads; ``None`` filters are not applied."""

    workspace_id: Optional[str] = None
    archived: Optional[bool] = False
    kinds: Optional[frozenset[str]] = None
    parent_session_id: Optional[str] = None
    #: Only these session ids.
    ids: Optional[frozenset[str]] = None
    #: Only sessions in these stored statuses (``waiting_user``, ...).
    statuses: Optional[frozenset[str]] = None
    #: Case-insensitive substring of the title.
    search: Optional[str] = None
    #: Only sessions with an active goal or loop / an open execution gate.
    autonomous: Optional[bool] = None
    gated: Optional[bool] = None
    model_selected: Optional[bool] = None
    memory_runs: Optional[bool] = None
    limit: int = 50
    cursor: Optional[str] = None

    def matches(self, row: RecordColumns) -> bool:
        """Whether ``row`` passes every filter of this query."""
        return (
            (not self.workspace_id or row.workspace_id == self.workspace_id)
            and (self.archived is None or row.archived == self.archived)
            and (self.kinds is None or row.kind in self.kinds)
            and (self.statuses is None or row.status in self.statuses)
            and (self.ids is None or row.id in self.ids)
            and (not self.parent_session_id or row.parent_session_id == self.parent_session_id)
            and (not self.search or self.search.casefold() in row.title.casefold())
            and (self.autonomous is None or row.autonomous == self.autonomous)
            and (self.gated is None or row.gated == self.gated)
            and (self.model_selected is None or row.model_selected == self.model_selected)
            and (self.memory_runs is None or row.memory_runs == self.memory_runs)
        )


def encode_cursor(recent_at: str, session_id: str) -> str:
    """The opaque keyset cursor naming the last row of a page."""

    raw = json.dumps([recent_at, session_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor from :func:`encode_cursor`; anything else is refused."""

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError(f"invalid session page cursor: {cursor!r}") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(part, str) for part in value)
    ):
        raise InvalidCursorError(f"invalid session page cursor: {cursor!r}")
    return value[0], value[1]


def bucket_of(session_id: str) -> str:
    """The index bucket record a session's columns live in."""
    return f"{_BUCKET}{zlib.crc32(session_id.encode()) % INDEX_BUCKETS:02d}"


class _MemoryRecords:
    """The records before a clio-core store is bound (and in unit tests)."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put_many(self, kind: str, records: Sequence[PutRecord]) -> None:
        for record in records:
            self.data[record.name] = record.data

    def get(self, kind: str, name: str) -> Optional[bytes]:
        return self.data.get(name)

    def delete(self, kind: str, name: str) -> None:
        self.data.pop(name, None)


class _Writer:
    """One ordered writer of record changes (the newest body per record wins)."""

    def __init__(self, store: Any, *, inline: bool = False) -> None:
        self.store = store
        self._inline = inline  # an in-process store: apply at once, no thread
        self._cond = threading.Condition()
        self._pending: dict[str, Optional[bytes]] = {}
        self._inflight: dict[str, Optional[bytes]] = {}  # taken, not yet confirmed
        self._submitted = 0
        self._landed = 0
        self._thread: Optional[threading.Thread] = None

    def peek(self, name: str) -> tuple[bool, Optional[bytes]]:
        """A pending (not yet landed) body: ``(True, body)``; a ``None`` body is a delete."""
        with self._cond:
            for held in (self._pending, self._inflight):
                if name in held:
                    return True, held[name]
        return False, None

    def submit(self, changes: dict[str, Optional[bytes]]) -> int:
        with self._cond:
            self._submitted += 1
            ticket = self._submitted
            if self._inline:
                for name, data in changes.items():
                    if data is None:
                        self.store.delete(KIND, name)
                    else:
                        self.store.put_many(KIND, [PutRecord(name, data)])
                self._landed = ticket
                return ticket
            self._pending.update(changes)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="clio-session-records", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()
        return ticket

    def wait(self, ticket: int, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._landed >= ticket, timeout)

    def flush(self, timeout: float) -> bool:
        with self._cond:
            ticket = self._submitted
        return self.wait(ticket, timeout)

    def _run(self) -> None:
        backoff = 0.5
        while True:
            with self._cond:
                if not self._cond.wait_for(lambda: bool(self._pending), _IDLE_SECONDS):
                    self._thread = None  # idle: the next submit starts a fresh writer
                    return
                batch = dict(self._pending)
                self._inflight = batch
                self._pending.clear()
                ticket = self._submitted
            try:
                puts = [PutRecord(name, data) for name, data in batch.items() if data is not None]
                if puts:
                    self.store.put_many(KIND, puts)
                for name, data in batch.items():
                    if data is None:
                        self.store.delete(KIND, name)
            except (ClioError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
                # A store write failure: retried, and reported typed each time.
                stream_audit(
                    WRITE_FAILED_REASON,
                    reason=WRITE_FAILED_REASON,
                    records=len(batch),
                    error=repr(exc)[:300],
                )
                logger.warning(
                    "session records write failed reason=%s records=%d error=%r; retrying",
                    WRITE_FAILED_REASON,
                    len(batch),
                    exc,
                )
                with self._cond:
                    for name, data in batch.items():
                        self._pending.setdefault(name, data)  # a newer body wins
                    self._inflight = {}
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 0.5
            # Evidence of what each change wrote (the stream audit log, when enabled).
            stream_audit("session_records.write", records=sorted(batch))
            with self._cond:
                self._inflight = {}
                self._landed = max(self._landed, ticket)
                self._cond.notify_all()


def _on_server_loop() -> bool:
    from clio_agent.arc.loop_guard import on_server_loop  # noqa: PLC0415

    return on_server_loop()


class SessionRecords:
    """Session records plus their in-memory index. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._memory = _MemoryRecords()
        self._writer = _Writer(self._memory, inline=True)
        self._bound = threading.Event()
        self._expected = False
        self._columns: dict[str, RecordColumns] = {}
        self._recent: SortedList = SortedList()  # (recent_at, id)
        self._created: SortedList = SortedList()  # (created_at, id)
        self._children: dict[str, set[str]] = {}
        self._buckets: dict[str, set[str]] = {}

    # ---- binding ------------------------------------------------------------

    @property
    def bound(self) -> bool:
        """Whether the records live in a clio-core store (not only in memory)."""
        return self._bound.is_set()

    def expect_store(self) -> None:
        """Announce that a clio-core store will be bound: reads wait for it."""
        self._expected = True

    def bind(self, store: Any) -> None:
        """Move the records into ``store`` and load its index (off the server loop)."""

        with self._lock:
            if self._bound.is_set() and self._writer.store is store:
                return
            carried = dict(self._memory.data)
            self._writer = _Writer(store)
            for number in range(INDEX_BUCKETS):
                raw = store.get(KIND, f"{_BUCKET}{number:02d}")
                for values in json.loads(raw).values() if raw else ():
                    self._index(RecordColumns(*values))
            if carried:
                # Records written before the store was bound (normally none).
                for name, raw in carried.items():
                    if name.startswith(_BUCKET):
                        for values in json.loads(raw).values():
                            self._index(RecordColumns(*values))
                buckets = {name for name in carried if name.startswith(_BUCKET)}
                rows = {name: raw for name, raw in carried.items() if name.startswith(_ROW)}
                self._writer.submit(
                    {**rows, **{bucket: self._bucket_body(bucket) for bucket in buckets}}
                )
                self._memory.data.clear()
            self._bound.set()

    def _ready(self) -> None:
        if not self._expected or self._bound.is_set():
            return
        if _on_server_loop():
            raise SessionRecordsNotReadyError(
                "Conversations are stored in clio-core, which is still attaching. Retry shortly."
            )
        if not self._bound.wait(_BIND_WAIT_SECONDS):
            raise SessionRecordsNotReadyError(
                "Conversations are stored in clio-core, which did not attach in time."
            )

    def flush(self, timeout: float = _WRITE_WAIT_SECONDS) -> bool:
        """Wait until every submitted write has landed (shutdown, tests)."""
        return self._writer.flush(timeout)

    # ---- the index ------------------------------------------------------------

    def _index(self, row: RecordColumns) -> None:
        self._unindex(row.id)
        self._columns[row.id] = row
        self._recent.add((row.recent_at, row.id))
        self._created.add((row.created_at, row.id))
        if row.parent_session_id:
            self._children.setdefault(row.parent_session_id, set()).add(row.id)
        self._buckets.setdefault(bucket_of(row.id), set()).add(row.id)

    def _unindex(self, session_id: str) -> None:
        old = self._columns.pop(session_id, None)
        if old is None:
            return
        self._recent.discard((old.recent_at, old.id))
        self._created.discard((old.created_at, old.id))
        self._buckets.get(bucket_of(old.id), set()).discard(old.id)
        siblings = self._children.get(old.parent_session_id)
        if siblings is not None:
            siblings.discard(old.id)
            if not siblings:
                self._children.pop(old.parent_session_id, None)

    def _bucket_body(self, bucket: str) -> bytes:
        members = sorted(self._buckets.get(bucket, ()))
        rows = {sid: list(astuple(self._columns[sid])) for sid in members}
        return json.dumps(rows, separators=(",", ":")).encode()

    # ---- records ------------------------------------------------------------

    def put(self, columns: RecordColumns, body: str) -> None:
        """Write one session's record (and its index bucket when a column moved)."""

        self._ready()
        with self._lock:
            old = self._columns.get(columns.id)
            self._index(columns)
            changes: dict[str, Optional[bytes]] = {_ROW + columns.id: body.encode()}
            if old != columns:
                bucket = bucket_of(columns.id)
                changes[bucket] = self._bucket_body(bucket)
            ticket = self._writer.submit(changes)
        self._settle(ticket)

    def reindex(self, rows: Iterable[RecordColumns]) -> int:
        """Rewrite the index columns of stored sessions (records untouched); returns
        how many changed. Each changed bucket is written once."""

        self._ready()
        changed = 0
        with self._lock:
            buckets: set[str] = set()
            for columns in rows:
                if columns.id in self._columns and self._columns[columns.id] != columns:
                    self._index(columns)
                    buckets.add(bucket_of(columns.id))
                    changed += 1
            if not buckets:
                return 0
            ticket = self._writer.submit({b: self._bucket_body(b) for b in sorted(buckets)})
        self._settle(ticket)
        return changed

    def totals(self) -> dict[str, Any]:
        """Session counts by status and the summed counters, from the index alone."""

        self._ready()
        with self._lock:
            rows = list(self._columns.values())
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        return {
            "total": len(rows),
            "by_status": by_status,
            "messages": sum(row.message_count for row in rows),
            "tokens_input": sum(row.tokens_input for row in rows),
            "tokens_output": sum(row.tokens_output for row in rows),
            "cost_usd": sum(row.cost_usd for row in rows),
        }

    def delete(self, session_id: str) -> None:
        """Remove one session's record and its index entry."""

        self._ready()
        with self._lock:
            self._unindex(session_id)
            bucket = bucket_of(session_id)
            ticket = self._writer.submit(
                {_ROW + session_id: None, bucket: self._bucket_body(bucket)}
            )
        self._settle(ticket)

    def _settle(self, ticket: int) -> None:
        # In memory the write is done at once. Off the server loop the write is
        # committed before the caller goes on; on the loop it lands with the writer's
        # next batch (a clio-core RPC never blocks the loop).
        if self._writer.store is self._memory or _on_server_loop():
            return
        if not self._writer.wait(ticket, _WRITE_WAIT_SECONDS):
            raise TimeoutError(
                f"{WRITE_FAILED_REASON}: a session record did not land in clio-core within "
                f"{_WRITE_WAIT_SECONDS:.0f}s; the write keeps retrying"
            )

    def body(self, session_id: str) -> Optional[str]:
        """One session's stored record, or ``None``."""

        self._ready()
        if session_id not in self._columns:
            return None
        name = _ROW + session_id
        pending, data = self._writer.peek(name)
        if not pending:
            data = self._writer.store.get(KIND, name)
        return None if data is None else data.decode()

    def bodies_for(self, ids: Iterable[str]) -> dict[str, str]:
        """The records of the named sessions (unknown ids are absent).

        Each record is one point read of clio-core; a page's reads are issued
        together (:data:`_READ_FANOUT` at a time) instead of one after another.
        """

        wanted = list(dict.fromkeys(ids))
        if len(wanted) <= 1 or self._writer.store is self._memory:
            pairs = [(sid, self.body(sid)) for sid in wanted]
        else:
            with ThreadPoolExecutor(min(_READ_FANOUT, len(wanted))) as pool:
                pairs = list(zip(wanted, pool.map(self.body, wanted), strict=True))
        return {sid: body for sid, body in pairs if body is not None}

    def ids(self) -> list[str]:
        """Every session id (no record is read)."""
        self._ready()
        with self._lock:
            return list(self._columns)

    def count(self) -> int:
        """How many sessions exist."""
        self._ready()
        return len(self._columns)

    def child_ids_by_parent(self) -> dict[str, list[str]]:
        """``{parent_session_id: [child ids]}`` (newest created first) from the index."""
        self._ready()
        with self._lock:
            return {
                parent: [
                    sid
                    for _created, sid in sorted(
                        ((self._columns[c].created_at, c) for c in children), reverse=True
                    )
                ]
                for parent, children in self._children.items()
            }

    def most_recent_id(self) -> Optional[str]:
        """The newest created session's id."""
        self._ready()
        with self._lock:
            return self._created[-1][1] if self._created else None

    def without_interaction_time(self) -> list[str]:
        """Ids of sessions written before ``last_interaction_at`` existed, still lacking it."""
        self._ready()
        with self._lock:
            return sorted(sid for sid, row in self._columns.items() if not row.interaction_known)

    # ---- queries ------------------------------------------------------------

    def select(self, query: PageQuery) -> list[str]:
        """Every session id matching ``query``, newest created first (no paging)."""

        self._ready()
        with self._lock:
            if query.ids is not None:
                rows = [self._columns[sid] for sid in query.ids if sid in self._columns]
                ordered = sorted(((r.created_at, r.id) for r in rows), reverse=True)
            else:
                ordered = list(reversed(self._created))
            return [sid for _created, sid in ordered if query.matches(self._columns[sid])]

    def page(self, query: PageQuery) -> tuple[list[str], Optional[str], int]:
        """One page of ids newest-interaction first, the next cursor, and the total."""

        self._ready()
        start = decode_cursor(query.cursor) if query.cursor else None
        limit = max(1, int(query.limit))
        with self._lock:
            ordered: Iterator[tuple[str, str]] = (
                self._recent.irange(maximum=start, inclusive=(True, False), reverse=True)
                if start is not None
                else reversed(self._recent)
            )
            found: list[RecordColumns] = []
            for _recent, sid in ordered:
                row = self._columns[sid]
                if query.matches(row):
                    found.append(row)
                    if len(found) > limit:
                        break
            total = sum(1 for row in self._columns.values() if query.matches(row))
        more = len(found) > limit
        found = found[:limit]
        last = found[-1] if found else None
        next_cursor = encode_cursor(last.recent_at, last.id) if more and last else None
        return [row.id for row in found], next_cursor, total

    def ancestors(self, ids: Iterable[str]) -> list[str]:
        """``ids`` and every ancestor of them (unknown ids dropped)."""

        self._ready()
        seen: dict[str, None] = {}
        with self._lock:
            for start in ids:
                sid = start
                while sid and sid in self._columns and sid not in seen:
                    seen[sid] = None
                    sid = self._columns[sid].parent_session_id
        return list(seen)

    def family(self, session_id: str) -> list[str]:
        """The tree ``session_id`` belongs to: its root first, then every descendant
        of the root (level by level, oldest first), then any ancestor not reached."""

        chain = self.ancestors([session_id])
        if not chain:
            return []
        with self._lock:
            tree: dict[str, None] = {chain[-1]: None}
            frontier = [chain[-1]]
            while frontier:
                level = sorted(
                    (self._columns[child].created_at, child)
                    for parent in frontier
                    for child in self._children.get(parent, ())
                    if child not in tree and child in self._columns
                )
                frontier = [child for _created, child in level]
                tree.update(dict.fromkeys(frontier))
        return [*tree, *(sid for sid in chain if sid not in tree)]

    def related_ids(self, family_of: str = "") -> list[str]:
        """``family_of``'s tree (:meth:`family`); without it, the sessions waiting on a
        person (:data:`WAITING_ON_PERSON`) and every ancestor of them."""

        if family_of:
            return self.family(family_of)
        return self.ancestors(self.select(PageQuery(archived=None, statuses=WAITING_ON_PERSON)))

    # ---- legacy import (the session_records_import store migration) -------

    def import_legacy(self, legacy_json: Optional[Path]) -> dict[str, Any]:
        """Import ``sessions.json``: every session not already present, in one batch.

        Idempotent, so an interrupted run can simply run again (present sessions are
        kept). An unreadable file is reported typed and raises, so the migration
        stays owed and a repaired file is imported by the next run. The file itself
        is never changed.
        """

        self._ready()
        if legacy_json is None or not legacy_json.is_file():
            return {"sessions": 0}
        try:
            raw = json.loads(legacy_json.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            stream_audit(
                LEGACY_UNREADABLE_REASON,
                reason=LEGACY_UNREADABLE_REASON,
                path=str(legacy_json),
                error=type(exc).__name__,
            )
            raise ValueError(f"{LEGACY_UNREADABLE_REASON}: {legacy_json}: {exc}") from exc
        from clio_agent.gact.sessions import Session  # noqa: PLC0415 - import cycle

        changes: dict[str, Optional[bytes]] = {}
        buckets: set[str] = set()
        with self._lock:
            for payload in raw.values() if isinstance(raw, dict) else ():
                if not isinstance(payload, dict):
                    continue
                try:
                    session = Session(**payload)
                except TypeError:
                    continue  # schema drift: the same per-row skip the JSON loader made
                if session.id in self._columns:
                    continue
                self._index(record_columns(session))
                changes[_ROW + session.id] = json.dumps(payload, sort_keys=True).encode()
                buckets.add(bucket_of(session.id))
            changes.update({bucket: self._bucket_body(bucket) for bucket in buckets})
            ticket = self._writer.submit(changes) if changes else 0
        if ticket and not self._writer.wait(ticket, _WRITE_WAIT_SECONDS):
            raise TimeoutError(f"{WRITE_FAILED_REASON}: the legacy import did not land")
        imported = sum(1 for name in changes if name.startswith(_ROW))
        stream_audit(
            LEGACY_IMPORTED_REASON,
            reason=LEGACY_IMPORTED_REASON,
            path=str(legacy_json),
            sessions=imported,
        )
        return {"sessions": imported}


def record_columns(session: Any) -> RecordColumns:
    """The indexed columns of ``session`` (the views' filters and recency)."""

    metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
    return RecordColumns(
        id=session.id,
        workspace_id=session.workspace_id,
        parent_session_id=session.parent_session_id,
        kind=session_kind(session),
        archived=bool(session.archived),
        created_at=session.created_at,
        # The recency the wire shows (protocol.v3.session.session_to_v3).
        recent_at=session.last_interaction_at or session.created_at,
        interaction_known=bool(session.last_interaction_at),
        status=str(session.status or ""),
        title=str(session.title or ""),
        message_count=int(session.message_count or 0),
        tokens_input=int(session.tokens_input or 0),
        tokens_output=int(session.tokens_output or 0),
        cost_usd=float(session.cost_usd or 0.0),
        autonomous=_flag(metadata, "goal", "active") or _flag(metadata, "loop", "active"),
        gated=bool(metadata.get("execution_gate")),
        memory_runs=bool(metadata.get("memory_pass") or metadata.get("memory_last_pass")),
        model_selected=not _model_ref_is_empty(session.model),
    )


def _flag(metadata: Mapping[str, Any], key: str, field: str) -> bool:
    value = metadata.get(key)
    return bool(isinstance(value, Mapping) and value.get(field))


def cache_capacity() -> int:
    """How many session rows stay resident (file -> env -> default)."""

    return max(
        1,
        conf.resolve(
            "sessions.cache_records",
            env="CLIO_SESSIONS_CACHE_RECORDS",
            default=1024,
            cast=conf.as_int,
        ),
    )


__all__ = [
    "INDEX_BUCKETS",
    "KIND",
    "InvalidCursorError",
    "PageQuery",
    "RecordColumns",
    "SessionRecords",
    "SessionRecordsNotReadyError",
    "WAITING_ON_PERSON",
    "bucket_of",
    "cache_capacity",
    "decode_cursor",
    "encode_cursor",
    "record_columns",
]
