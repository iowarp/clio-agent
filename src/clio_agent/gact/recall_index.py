"""The recall index: each conversation's search digest, kept in clio-core (owner module).

Recall (``session_search``, ``GET /v1/session-recall/search``) and the Evidence board's
reference search look across every conversation of a store. Reading every transcript per
request made them cost seconds to minutes and hundreds of MB on a long-lived store. This
module keeps, in the clio-core store every other record lives in (ARC kind :data:`KIND`;
no other store), each session's :class:`~clio_agent.gact.recall_digest.TranscriptDigest`
and answers a search from them. The digests are grouped into :data:`BUCKETS` bucket
records (``b/<nn>``, a session's bucket fixed by a hash of its id), so a search over
every conversation is :data:`BUCKETS` point reads, not one per conversation.

**Staleness, without a put per transcript write.** Every change of a session's
message-part lane (``_events/m``: an appended chunk, a generation pointer, an erase)
passes the ARC segment store's write observer (:meth:`RecallIndex.writing`), entered
before the record is written and left once the cached copy shows it. In the steady state
that costs no store work: the session is marked *dirty* in memory and a rebuild is
scheduled. Three facts decide whether a stored digest is current:

* **In this process:** the dirty set. A rebuild (one thread, :func:`rebuild_delay_s`
  after the session's last write) assembles the transcript from clio-core, writes the
  digest into its bucket and leaves the session clean only when no write began or was in
  flight meanwhile (a per-session write epoch).
* **Across a crash:** a durable marker ``w/<id>``, put before the FIRST transcript write
  of a session that holds none, and kept while the session is active: it is retired
  :data:`MARKER_RETIRE_S` after the session's digest settled clean with no write since.
  So the one extra put is paid once per conversation per active spell, not per write.
  A process that finds markers (:meth:`RecallIndex._load_markers`) treats those sessions
  as dirty and rebuilds them.
* **Across an older CLIO:** each digest carries the *stamp* it was built against: the
  session's last interaction time (``last_interaction_at``, else ``created_at``), which
  every CLIO moves whenever it records a message (``SessionStore.update`` with a
  ``message_count``). An older CLIO writes transcripts without this index; when this one
  opens the store again, a digest whose stamp differs from the session's reads as
  :data:`MOVED_REASON` and is rebuilt. What the stamp cannot see: an older CLIO's
  transcript change that recorded no message (parts sealed by a turn it never
  finished), until that conversation's next message.

**Read exactly, never partially.** :func:`session_digests` gives each requested session's
digest: a resident (in-memory) ledger is digested on the spot -- it is what the
transcript read returned, live parts included; a clean stored digest whose stamp matches
is read from its bucket; a dirty, moved, missing, old-format or unreadable one is
replaced by the transcript read (:func:`~clio_agent.gact.transcript_reads.read_transcript`)
and digested the same way, with a typed reason counted in the :class:`DigestReadReport`
that rides the response, and a rebuild is scheduled. Every path ends in
:func:`~clio_agent.gact.recall_digest.digest_messages`, so the hits do not depend on
where a digest came from.

The one-time build for a store written before this index is the ``recall_index_build``
store migration (:mod:`clio_agent.gact.store_migrations`); a store that skipped it fills
itself in: a missing digest is read from the transcript and scheduled.

An older CLIO opening a store that holds these records never reads them: the
``recall_index`` kind is not among its record kinds (it neither reads nor clears them)
and the ``recall_index_build`` done marker is a name it does not know.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, Optional

import msgspec

from clio_agent import conf
from clio_agent.arc.batch_put import PutRecord
from clio_agent.errors import ClioError
from clio_agent.gact.recall_digest import (
    TranscriptDigest,
    decode_record,
    digest_messages,
    encode_digest,
)
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

__all__ = [
    "BUCKETS",
    "KIND",
    "MARKER_RETIRE_S",
    "DigestReadReport",
    "RecallIndex",
    "attach_recall_index",
    "bucket_of",
    "build_recall_index",
    "close_recall_index",
    "is_transcript_scope",
    "rebuild_delay_s",
    "recall_index_of",
    "session_digests",
    "session_stamp",
]

#: The ARC record kind (``clio_agent.arc.storage.ARC_KINDS``).
KIND = "recall_index"
#: How many bucket records the digests are split into.
BUCKETS = 64
_BUCKET = "b/"
_DIRTY = "w/"
#: The message-part lane (``part_atoms.MESSAGE_PART_SCOPE``) and its record family:
#: chunks (``/N``), the generation pointer (``/gen``) and later generations
#: (``/g/N`` and their chunks). Not its siblings (``/edge``).
_LANE_FAMILY = re.compile(r"_events/m(?:/gen|(?:/g/[1-9][0-9]*)?(?:/[1-9][0-9]*)?)")

#: Typed reasons (no silent fallback): why a session was read from its transcript.
DIRTY_REASON = "recall_index_session_written"
MOVED_REASON = "recall_index_session_moved"
MISSING_REASON = "recall_index_missing"
FORMAT_REASON = "recall_index_format_changed"
UNREADABLE_REASON = "recall_index_unreadable"
UNAVAILABLE_REASON = "recall_index_unavailable"
#: Typed reasons of index maintenance (stream audit + log).
MARK_FAILED_REASON = "recall_index_mark_failed"
BUILD_FAILED_REASON = "recall_index_build_failed"
#: Seconds a session's durable marker outlives its last clean rebuild (no write since).
MARKER_RETIRE_S = 300.0
#: Locks guarding the durable dirty markers, by session hash.
_MARK_STRIPES = 64
#: Seconds the maintenance thread stays alive with nothing to do.
_IDLE_SECONDS = 5.0
_NEVER = float("inf")
_STORE_ERRORS = (ClioError, OSError, RuntimeError, TimeoutError, ValueError, msgspec.MsgspecError)
_BUCKET_ENCODER = msgspec.msgpack.Encoder()
_BUCKET_DECODER = msgspec.msgpack.Decoder(dict[str, bytes])


def rebuild_delay_s() -> float:
    """Seconds without a transcript write before a session's digest is rebuilt.

    Config: ``gact.recall_index.rebuild_delay_s`` / ``CLIO_RECALL_INDEX_REBUILD_DELAY_S``.
    """

    return conf.resolve(
        "gact.recall_index.rebuild_delay_s",
        env="CLIO_RECALL_INDEX_REBUILD_DELAY_S",
        default=2.0,
        cast=conf.as_float,
    )


def is_transcript_scope(scope: str) -> bool:
    """Whether ``scope`` is a record of a session's message-part lane (any generation)."""

    return scope.startswith("_events/m") and _LANE_FAMILY.fullmatch(scope) is not None


def bucket_of(session_id: str) -> str:
    """The bucket record holding ``session_id``'s digest."""

    return f"{_BUCKET}{zlib.crc32(session_id.encode()) % BUCKETS:02d}"


def session_stamp(session: Any) -> str:
    """The stamp a digest of ``session`` is built against (``""`` for no session)."""

    if session is None:
        return ""
    return str(
        getattr(session, "last_interaction_at", "") or getattr(session, "created_at", "") or ""
    )


@dataclass
class DigestReadReport:
    """Where each session's digest came from, for the response (typed reasons)."""

    index: int = 0
    resident: int = 0
    transcript: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def fallback(self, reason: str) -> None:
        """Count one session read from its transcript for ``reason``."""
        self.transcript += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        """The ``index`` block of a search response's metadata."""
        row: dict[str, Any] = {
            "from_index": self.index,
            "from_resident": self.resident,
            "from_transcript": self.transcript,
        }
        if self.reasons:
            row["transcript_reasons"] = dict(sorted(self.reasons.items()))
        return row


class _NoWrite:
    """The observer's enclosure of a write that is not a transcript write."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


_NO_WRITE = _NoWrite()


class _TranscriptWrite:
    """One transcript write of a session: dirty from before the put until its install."""

    __slots__ = ("_index", "_session_id")

    def __init__(self, index: "RecallIndex", session_id: str) -> None:
        self._index = index
        self._session_id = session_id

    def __enter__(self) -> None:
        self._index._begin(self._session_id)

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self._index._end(self._session_id)


class RecallIndex:
    """The stored digests of one clio-core store, kept current by transcript writes."""

    def __init__(
        self,
        store: Any,
        assemble: Callable[[str], list["Message"]],
        *,
        stamp: Optional[Callable[[str], str]] = None,
        delay_s: Optional[float] = None,
        retire_s: float = MARKER_RETIRE_S,
        inline: bool = False,
    ) -> None:
        self._store = store
        self._assemble = assemble
        self._stamp = stamp or (lambda _sid: "")
        self._delay_s = rebuild_delay_s() if delay_s is None else delay_s
        self._retire_s = retire_s
        self._inline = inline  # rebuild in the writing thread (unit tests)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._dirty: set[str] = set()  # written since its digest was last built clean
        self._marked: set[str] = set()  # holds a durable marker (put or adopted)
        self._epoch: dict[str, int] = {}  # bumped as each transcript write begins and ends
        self._inflight: dict[str, int] = {}
        self._due: dict[str, float] = {}  # rebuilds, by monotonic deadline
        self._retire: dict[str, float] = {}  # marker retirements, by monotonic deadline
        self._building = 0  # maintenance batches the thread took and has not finished
        self._closed = False
        self._wait_until = _NEVER  # the deadline the maintenance thread sleeps until
        # Striped: a marker put and a marker delete of one session never interleave.
        self._mark_locks = tuple(threading.Lock() for _ in range(_MARK_STRIPES))
        # One read-modify-write of a bucket at a time.
        self._bucket_locks = {f"{_BUCKET}{n:02d}": threading.Lock() for n in range(BUCKETS)}
        self._markers_loaded = False
        self._thread: Optional[threading.Thread] = None
        #: Durable marker puts this process made (the write path's only store work).
        self.marker_puts = 0

    # ---- the write observer (ARC segment store) ------------------------- #

    def writing(self, session_id: str, scope: str) -> _NoWrite | _TranscriptWrite:
        """Enclose one record change of ``scope``; a transcript write marks the session."""

        if not is_transcript_scope(scope):
            return _NO_WRITE
        return _TranscriptWrite(self, session_id)

    def _begin(self, session_id: str) -> None:
        with self._lock:
            self._epoch[session_id] = self._epoch.get(session_id, 0) + 1
            self._inflight[session_id] = self._inflight.get(session_id, 0) + 1
            self._dirty.add(session_id)
            self._retire.pop(session_id, None)
            if session_id in self._marked:
                return  # the steady state: no store work at all
        with self._mark_lock(session_id):
            with self._lock:
                if session_id in self._marked:
                    return  # another write of the session put it meanwhile
            if self._put_marker(session_id):
                with self._lock:
                    self._marked.add(session_id)

    def _end(self, session_id: str) -> None:
        with self._lock:
            left = self._inflight.get(session_id, 1) - 1
            if left:
                self._inflight[session_id] = left
            else:
                self._inflight.pop(session_id, None)
            self._epoch[session_id] = self._epoch.get(session_id, 0) + 1
        self.schedule(session_id)

    def _mark_lock(self, session_id: str) -> threading.Lock:
        return self._mark_locks[zlib.crc32(session_id.encode()) % _MARK_STRIPES]

    def _put_marker(self, session_id: str) -> bool:
        """Put ``session_id``'s durable marker (before its transcript write lands)."""
        try:
            self._store.put_many(KIND, [PutRecord(_DIRTY + session_id, b"1")])
        except _STORE_ERRORS as exc:
            # The session stays dirty in this process and is rebuilt; only a crash
            # before that rebuild could leave its digest stale, so say so. The next
            # write of the session tries again.
            self._audit(MARK_FAILED_REASON, session_id, exc)
            return False
        self.marker_puts += 1
        return True

    # ---- maintenance ---------------------------------------------------- #

    def schedule(self, session_id: str) -> None:
        """Rebuild ``session_id``'s digest once its transcript has been quiet a while."""

        if self._inline:
            self.rebuild_many([session_id])
            return
        with self._cond:
            if self._closed:
                return  # stopping: the session stays dirty (and marked) for the next process
            deadline = time.monotonic() + self._delay_s
            self._due[session_id] = deadline
            self._wake(deadline)

    def _wake(self, deadline: float) -> None:
        """Start the maintenance thread, or wake it if ``deadline`` is sooner than the one
        it waits for (under ``self._cond``): a write that only pushes its session's
        rebuild later costs no thread wake-up."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="clio-recall-index", daemon=True)
            self._wait_until = deadline
            self._thread.start()
        elif deadline < self._wait_until:
            self._wait_until = deadline
            self._cond.notify_all()

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            # An unexpected error ends this thread loudly (``threading.excepthook``);
            # the next schedule starts a fresh one, and the sessions stay dirty.
            with self._cond:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
                if not self._due and not self._retire:
                    self._wait_until = _NEVER
                    if not self._cond.wait(_IDLE_SECONDS) and not (self._due or self._retire):
                        self._thread = None  # idle: the next schedule starts a fresh one
                        return
                    continue
                now = time.monotonic()
                ready = [sid for sid, due in self._due.items() if due <= now]
                retiring = [sid for sid, due in self._retire.items() if due <= now]
                if not ready and not retiring:
                    self._wait_until = min([*self._due.values(), *self._retire.values()])
                    self._cond.wait(self._wait_until - now)
                    continue
                self._wait_until = _NEVER  # busy: a sooner deadline is seen on the next pass
                for sid in ready:
                    del self._due[sid]
                for sid in retiring:
                    del self._retire[sid]
                self._building += 1
            try:
                if ready:
                    self.rebuild_many(ready)
                for sid in retiring:
                    self._retire_marker(sid)
            finally:
                with self._cond:
                    self._building -= 1
                    self._cond.notify_all()

    def rebuild(self, session_id: str) -> bool:
        """Store ``session_id``'s digest from its transcript; whether it is clean now."""

        return self.rebuild_many([session_id])[session_id]

    def rebuild_many(self, session_ids: Sequence[str]) -> dict[str, bool]:
        """Rebuild these sessions' digests, one write per bucket; which ones are clean.

        The stamp is read before the transcript, so a digest is never newer-stamped than
        what it holds. Assembly takes no transcript lock: a write that begins while it
        runs (or was in flight when it started) leaves the session dirty.
        """

        result: dict[str, bool] = {}
        epochs: dict[str, int] = {}
        with self._lock:
            for sid in session_ids:
                if self._inflight.get(sid):
                    result[sid] = False  # a write is landing: build after it
                else:
                    epochs[sid] = self._epoch.get(sid, 0)
        built: dict[str, dict[str, bytes]] = {}
        for sid in epochs:
            try:
                stamp = self._stamp(sid)
                data = encode_digest(digest_messages(self._assemble(sid)), stamp)
            except _STORE_ERRORS as exc:
                self._audit(BUILD_FAILED_REASON, sid, exc)
                result[sid] = False
                continue
            built.setdefault(bucket_of(sid), {})[sid] = data
        for bucket, entries in built.items():
            try:
                with self._bucket_locks[bucket]:
                    held = self._read_bucket(bucket, rewriting=True)
                    held.update(entries)
                    self._store.put_many(KIND, [PutRecord(bucket, _BUCKET_ENCODER.encode(held))])
            except _STORE_ERRORS as exc:
                for sid in entries:
                    self._audit(BUILD_FAILED_REASON, sid, exc)
                    result[sid] = False
                continue
            for sid in entries:
                result[sid] = self._settle(sid, epochs[sid])
        if not self._inline:
            for sid, clean in result.items():
                if not clean:
                    self._ensure_due(sid)  # still dirty: reads keep using the transcript
        return result

    def _settle(self, session_id: str, epoch: int) -> bool:
        """After its digest landed: clean unless a write began since the build started.

        A clean session keeps its durable marker for :data:`MARKER_RETIRE_S` (its next
        write then costs nothing); the maintenance thread retires it after that.
        """

        retire_now = False
        with self._cond:
            clean = self._epoch.get(session_id, 0) == epoch and not self._inflight.get(session_id)
            if clean:
                self._dirty.discard(session_id)
                self._due.pop(session_id, None)  # built: no later rebuild owed
                if session_id in self._marked:
                    if self._retire_s <= 0:
                        retire_now = True
                    elif not self._closed:
                        deadline = time.monotonic() + self._retire_s
                        self._retire[session_id] = deadline
                        self._wake(deadline)
        if retire_now:
            self._retire_marker(session_id)
        return clean

    def _retire_marker(self, session_id: str) -> None:
        """Delete a clean session's durable marker (its next write puts it again)."""

        with self._mark_lock(session_id):
            with self._lock:
                if (
                    session_id in self._dirty
                    or self._inflight.get(session_id)
                    or session_id not in self._marked
                ):
                    return  # written again (or never marked): the marker stays
                self._marked.discard(session_id)  # a write from now on puts it again
            try:
                self._store.delete(KIND, _DIRTY + session_id)
            except _STORE_ERRORS as exc:
                # Harmless: the next process reads the marker and rebuilds once more.
                self._audit(MARK_FAILED_REASON, session_id, exc)

    def flush(self, timeout: float = 30.0) -> bool:
        """Wait until no rebuild is due or running (tests and the migration's own end)."""

        deadline = time.monotonic() + timeout
        with self._cond:
            while self._due or self._inflight or self._building:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
        return True

    def close(self, timeout: float = 5.0) -> None:
        """Stop maintenance (shutdown, bounded): retire the markers of clean sessions.

        What is still dirty keeps its marker, so the next process rebuilds it.
        """

        with self._cond:
            self._closed = True
            self._due.clear()
            retiring = list(self._retire)
            self._retire.clear()
            thread = self._thread
            self._cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        deadline = time.monotonic() + timeout
        for sid in retiring:
            if time.monotonic() > deadline:
                break
            self._retire_marker(sid)

    # ---- reading ------------------------------------------------------- #

    def _read_bucket(self, bucket: str, *, rewriting: bool = False) -> dict[str, bytes]:
        """One bucket's ``{session: digest record}`` (``{}`` when absent).

        Raises:
            msgspec.DecodeError: The bucket is not readable (a read types it; a rewrite
                starts the bucket over, typed, and its other sessions refill on read).
        """

        raw = self._store.get(KIND, bucket)
        if not raw:
            return {}
        try:
            return _BUCKET_DECODER.decode(raw)
        except msgspec.DecodeError as exc:
            if not rewriting:
                raise
            self._audit(UNREADABLE_REASON, bucket, exc)
            return {}

    def _load_markers(self) -> None:
        """Adopt the markers a previous process left (a crash before its rebuilds).

        Once per process, at its first read. A marker this process put itself is not
        adopted: its session is tracked already (dirty until rebuilt from the whole
        transcript, which also covers what a previous process left).
        """

        with self._lock:
            if self._markers_loaded:
                return
            self._markers_loaded = True
        names = [name[len(_DIRTY) :] for name in self._store.scan_names(KIND, _DIRTY)]
        with self._lock:
            sessions = [sid for sid in names if sid not in self._marked]
            self._dirty.update(sessions)
            self._marked.update(sessions)
        for sid in sessions:
            self._ensure_due(sid)

    def _ensure_due(self, session_id: str) -> None:
        """Schedule a dirty session's rebuild unless one is already due or running."""
        with self._lock:
            pending = session_id in self._due or bool(self._inflight.get(session_id))
        if not pending and not self._inline:
            self.schedule(session_id)

    def is_dirty(self, session_id: str) -> bool:
        """Whether ``session_id``'s transcript was written since its digest was built."""
        with self._lock:
            return session_id in self._dirty

    def is_marked(self, session_id: str) -> bool:
        """Whether this process holds ``session_id``'s durable marker as stored."""
        with self._lock:
            return session_id in self._marked

    def read(
        self, session_ids: Sequence[str], stamps: Optional[Mapping[str, str]] = None
    ) -> Iterator[tuple[str, TranscriptDigest | str]]:
        """Each session's stored digest, else the typed reason it cannot be used.

        ``stamps`` (each session's :func:`session_stamp`) are compared with the stamp a
        digest was built against; a session absent from it is not compared. Dirty
        sessions first, then bucket by bucket (one bucket in memory at a time).
        """

        self._load_markers()
        by_bucket: dict[str, list[str]] = {}
        for sid in session_ids:
            if self.is_dirty(sid):
                self._ensure_due(sid)
                yield sid, DIRTY_REASON
            else:
                by_bucket.setdefault(bucket_of(sid), []).append(sid)
        for bucket in sorted(by_bucket):
            try:
                held = self._read_bucket(bucket)
            except msgspec.DecodeError as exc:
                self._audit(UNREADABLE_REASON, bucket, exc)
                for sid in by_bucket[bucket]:
                    self._ensure_due(sid)
                    yield sid, UNREADABLE_REASON
                continue
            for sid in by_bucket[bucket]:
                expected = stamps.get(sid) if stamps is not None else None
                yield sid, self._decode(sid, held.get(sid), expected)

    def _decode(
        self, session_id: str, raw: Optional[bytes], expected: Optional[str]
    ) -> TranscriptDigest | str:
        if raw is None:
            reason = MISSING_REASON
        else:
            try:
                found = decode_record(raw)
            except (zlib.error, msgspec.DecodeError) as exc:
                self._audit(UNREADABLE_REASON, session_id, exc)
                found, reason = None, UNREADABLE_REASON
            else:
                reason = FORMAT_REASON
            if found is not None:
                digest, stamp = found
                if expected is None or stamp == expected:
                    return digest
                reason = MOVED_REASON  # written by a CLIO that does not keep this index
        self._ensure_due(session_id)  # fill the index in for the next search
        return reason

    def stored_stamps(self) -> dict[str, str]:
        """Every stored digest's session and stamp (every bucket read; the migration)."""
        held: dict[str, str] = {}
        for bucket in self._bucket_locks:
            try:
                records = self._read_bucket(bucket)
            except msgspec.DecodeError as exc:
                self._audit(UNREADABLE_REASON, bucket, exc)  # rebuilt by the migration
                continue
            for sid, raw in records.items():
                try:
                    found = decode_record(raw)
                except (zlib.error, msgspec.DecodeError):
                    continue  # unreadable: rebuilt
                if found is not None:
                    held[sid] = found[1]
        return held

    @staticmethod
    def _audit(reason: str, subject: str, exc: BaseException) -> None:
        stream_audit(reason, reason=reason, subject=subject, error=repr(exc)[:300])
        logger.warning("recall index reason=%s subject=%s error=%r", reason, subject, exc)


# ---- the app seam ------------------------------------------------------ #


def attach_recall_index(app: "FastAPI", arc: Any) -> Optional[RecallIndex]:
    """Give ``arc``'s store a recall index and route its transcript writes through it."""

    from clio_agent.arc.segment_cache import transient_reads  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415
        assemble_session_messages,
    )

    segments = getattr(arc, "_segments", None)
    store = getattr(arc, "_store", None)
    if segments is None or store is None or not hasattr(segments, "set_write_observer"):
        return None
    current = getattr(app.state, "recall_index", None)
    if not isinstance(current, RecallIndex) or current._store is not store:

        def assemble(session_id: str) -> list["Message"]:
            # Never cached: a rebuild does not make the session resident. A rebuild can
            # run while a turn is still writing; its message without an envelope is in
            # flight, not a dead turn, and the finalize write schedules the next rebuild.
            with transient_reads():
                return assemble_session_messages(arc, session_id, report_incomplete=False)

        def stamp(session_id: str) -> str:
            sessions = getattr(app.state, "sessions", None)
            return session_stamp(sessions.get(session_id) if sessions is not None else None)

        current = RecallIndex(store, assemble, stamp=stamp)
        app.state.recall_index = current
    segments.set_write_observer(current)
    return current


def recall_index_of(app: "FastAPI") -> Optional[RecallIndex]:
    """The app's recall index, or ``None`` (no clio-core store: minimal wiring)."""

    index = getattr(getattr(app, "state", None), "recall_index", None)
    return index if isinstance(index, RecallIndex) else None


def close_recall_index(app: "FastAPI", timeout: float = 5.0) -> None:
    """Stop the app's recall index maintenance before clio-core is released (shutdown)."""

    index = recall_index_of(app)
    if index is not None:
        index.close(timeout)


def _resident(app: "FastAPI", session_id: str) -> Optional[list["Message"]]:
    peek = getattr(app.state.messages, "peek_resident", None)
    return peek(session_id) if callable(peek) else None


def session_digests(
    app: "FastAPI",
    session_ids: Sequence[str],
    report: DigestReadReport,
    *,
    stamps: Optional[Mapping[str, str]] = None,
    may_contain: Optional[Callable[[str], bool]] = None,
) -> Iterator[tuple[str, TranscriptDigest]]:
    """Each session's digest, once, in no particular order (callers order by position).

    ``stamps`` are the sessions' :func:`session_stamp` as the caller read them.
    ``may_contain`` (a transcript prefilter) lets a fallback skip a session whose
    stored records cannot match: it then yields an empty digest.
    """

    from clio_agent.gact.transcript_reads import read_transcript  # noqa: PLC0415

    rest: list[str] = []
    for sid in dict.fromkeys(session_ids):
        rows = _resident(app, sid)
        if rows is None:
            rest.append(sid)
            continue
        report.resident += 1
        yield sid, digest_messages(rows)
    index = recall_index_of(app)
    stored = (
        index.read(rest, stamps)
        if index is not None
        else ((sid, UNAVAILABLE_REASON) for sid in rest)
    )
    for sid, found in stored:
        if isinstance(found, TranscriptDigest):
            report.index += 1
            yield sid, found
            continue
        report.fallback(found)
        if may_contain is not None and not may_contain(sid):
            yield sid, TranscriptDigest()
            continue
        yield sid, digest_messages(read_transcript(app, sid))


def build_recall_index(app: "FastAPI") -> dict[str, Any]:
    """Store a current digest for every session that has none (the one-time migration).

    Idempotent and resumable: a session already holding a clean digest built against
    its current stamp is skipped. One bucket's sessions are built and written together
    (one write per bucket).
    """

    index = recall_index_of(app)
    if index is None:
        arc = getattr(app.state, "arc", None)
        index = attach_recall_index(app, arc) if arc is not None else None
    if index is None:
        raise RuntimeError("the recall index needs the clio-core store (none is attached)")
    index._load_markers()  # a marked session is rebuilt even when it has a digest
    stored = index.stored_stamps()
    by_bucket: dict[str, list[str]] = {}
    done = 0
    for sid in app.state.sessions.ids():
        if sid in stored and not index.is_dirty(sid) and stored[sid] == index._stamp(sid):
            done += 1
        else:
            by_bucket.setdefault(bucket_of(sid), []).append(sid)
    built = failed = 0
    for bucket in sorted(by_bucket):
        for clean in index.rebuild_many(by_bucket[bucket]).values():
            built += clean
            failed += not clean
    index.flush()
    return {"sessions": done + built, "built": built, "not_clean": failed}
