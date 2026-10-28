"""The recall index: each conversation's search digest, kept in clio-core (owner module).

Recall and the Evidence board's reference search look across every conversation of a
store. Reading every transcript per request made them cost seconds to minutes and
hundreds of MB on a long-lived store. This module keeps, in the clio-core store every
other record lives in (ARC kind :data:`KIND`; no other store), one record per session
holding its :class:`~clio_agent.gact.recall_digest.TranscriptDigest` (``d/<id>``), and
answers a search from those records: one small point read per conversation.

**Maintained where the transcript is written.** Every write of a session's message-part
lane (``_events/m``: an appended chunk, a whole-lane replace, an erase) passes the ARC
segment store's write observer (:meth:`RecallIndex.writing`), BEFORE the record is put:

* the session becomes *dirty*: in memory at once and, on its first write since its
  digest was last built, durably (a ``w/<id>`` marker put before the transcript write,
  so a crash between the two leaves the marker, never a stale digest that reads clean);
* a rebuild is scheduled: one writer thread, after :func:`rebuild_delay_s` without a
  write to that session, assembles the session's transcript from clio-core (not
  cached), stores its digest and, when no write landed meanwhile, deletes the marker.

**Read exactly.** :func:`session_digests` gives each requested session's digest in the
order asked: a resident (in-memory) ledger is digested on the spot -- it is what the
transcript read returned, live parts included; a clean stored digest is read from
clio-core; a dirty, missing or unreadable one is replaced by the transcript read
(:func:`~clio_agent.gact.transcript_reads.read_transcript`) and digested the same way,
with a typed reason counted in the :class:`DigestReadReport` that rides the response.
Every path ends in :func:`~clio_agent.gact.recall_digest.digest_messages`, so the
hits do not depend on where a digest came from.

The one-time build for a store written before this index is the ``recall_index_build``
store migration (:mod:`clio_agent.gact.store_migrations`).
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import batched
from typing import TYPE_CHECKING, Any, Optional

import msgspec

from clio_agent import conf
from clio_agent.arc.batch_put import PutRecord
from clio_agent.errors import ClioError
from clio_agent.gact.recall_digest import (
    TranscriptDigest,
    decode_digest,
    digest_messages,
    encode_digest,
)
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

__all__ = [
    "KIND",
    "DigestReadReport",
    "RecallIndex",
    "attach_recall_index",
    "build_recall_index",
    "is_transcript_scope",
    "rebuild_delay_s",
    "recall_index_of",
    "session_digests",
]

#: The ARC record kind (``clio_agent.arc.storage.ARC_KINDS``).
KIND = "recall_index"
_DIGEST = "d/"
_DIRTY = "w/"
#: The message-part lane every transcript write goes to (``part_atoms.MESSAGE_PART_SCOPE``).
_LANE = "_events/m"

#: Typed reasons (no silent fallback): why a session was read from its transcript.
DIRTY_REASON = "recall_index_session_written"
MISSING_REASON = "recall_index_missing"
FORMAT_REASON = "recall_index_format_changed"
UNREADABLE_REASON = "recall_index_unreadable"
UNAVAILABLE_REASON = "recall_index_unavailable"
#: Typed reasons of index maintenance (stream audit + log).
MARK_FAILED_REASON = "recall_index_mark_failed"
BUILD_FAILED_REASON = "recall_index_build_failed"
#: Point reads issued together, and sessions read per window (bounded memory).
_READ_FANOUT = 16
_WINDOW = 64
#: Locks guarding the durable dirty markers, by session hash.
_MARK_STRIPES = 64
#: Seconds the rebuild thread stays alive with nothing to do.
_IDLE_SECONDS = 5.0
_STORE_ERRORS = (ClioError, OSError, RuntimeError, TimeoutError, ValueError)


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

    return scope == _LANE or scope.startswith(_LANE + "/")


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


class RecallIndex:
    """The stored digests of one clio-core store, kept current by transcript writes."""

    def __init__(
        self,
        store: Any,
        assemble: Callable[[str], list["Message"]],
        *,
        delay_s: Optional[float] = None,
        inline: bool = False,
    ) -> None:
        self._store = store
        self._assemble = assemble
        self._delay_s = rebuild_delay_s() if delay_s is None else delay_s
        self._inline = inline  # rebuild in the writing thread (unit tests)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._dirty: set[str] = set()
        self._epoch: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._due: dict[str, float] = {}
        # Striped: a marker put and a marker delete of one session never interleave.
        self._mark_locks = tuple(threading.Lock() for _ in range(_MARK_STRIPES))
        self._markers_loaded = False
        self._thread: Optional[threading.Thread] = None

    # ---- the write observer (ARC segment store) ------------------------- #

    @contextmanager
    def writing(self, session_id: str, scope: str) -> Iterator[None]:
        """Wrap one record write of ``scope``; a transcript write marks the session dirty."""

        if not is_transcript_scope(scope):
            yield
            return
        self._begin(session_id)
        try:
            yield
        finally:
            self._end(session_id)

    def _begin(self, session_id: str) -> None:
        with self._lock:
            self._epoch[session_id] = self._epoch.get(session_id, 0) + 1
            self._inflight[session_id] = self._inflight.get(session_id, 0) + 1
            first = session_id not in self._dirty
            self._dirty.add(session_id)
        if first:
            with self._mark_lock(session_id):
                self._put_marker(session_id)

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

    def _put_marker(self, session_id: str) -> None:
        try:
            self._store.put_many(KIND, [PutRecord(_DIRTY + session_id, b"1")])
        except _STORE_ERRORS as exc:
            # The session stays dirty in this process and is rebuilt; only a crash
            # before that rebuild could leave its digest stale, so say so.
            self._audit(MARK_FAILED_REASON, session_id, exc)

    # ---- rebuilding ---------------------------------------------------- #

    def schedule(self, session_id: str) -> None:
        """Rebuild ``session_id``'s digest once its transcript has been quiet a while."""

        if self._inline:
            self.rebuild(session_id)
            return
        with self._cond:
            self._due[session_id] = time.monotonic() + self._delay_s
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="clio-recall-index", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._cond.wait_for(lambda: bool(self._due), _IDLE_SECONDS):
                    self._thread = None  # idle: the next schedule starts a fresh one
                    return
                now = time.monotonic()
                ready = [sid for sid, due in self._due.items() if due <= now]
                if not ready:
                    self._cond.wait(min(self._due.values()) - now)
                    continue
                for sid in ready:
                    del self._due[sid]
            for sid in ready:
                self.rebuild(sid)

    def rebuild(self, session_id: str) -> bool:
        """Store ``session_id``'s digest from its transcript; whether it is clean now."""

        with self._lock:
            writing = bool(self._inflight.get(session_id))
            epoch = self._epoch.get(session_id, 0)
        if writing:
            if not self._inline:
                self.schedule(session_id)  # a write is landing: build after it
            return False
        try:
            data = encode_digest(digest_messages(self._assemble(session_id)))
            self._store.put_many(KIND, [PutRecord(_DIGEST + session_id, data)])
        except _STORE_ERRORS as exc:
            self._audit(BUILD_FAILED_REASON, session_id, exc)
            if not self._inline:
                self.schedule(session_id)  # still dirty: reads keep using the transcript
            return False
        with self._mark_lock(session_id):
            with self._lock:
                clean = self._epoch.get(session_id, 0) == epoch and not self._inflight.get(
                    session_id
                )
                marked = clean and session_id in self._dirty
                if clean:
                    self._dirty.discard(session_id)
                    self._epoch.pop(session_id, None)
            if marked:
                try:
                    self._store.delete(KIND, _DIRTY + session_id)
                except _STORE_ERRORS as exc:
                    # Harmless: the next process reads the marker and rebuilds once more.
                    self._audit(MARK_FAILED_REASON, session_id, exc)
        if not clean and not self._inline:
            self.schedule(session_id)
        return clean

    def flush(self, timeout: float = 30.0) -> bool:
        """Wait until no rebuild is due (tests and the migration's own end)."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._due and not self._inflight:
                    return True
            time.sleep(0.05)
        return False

    # ---- reading ------------------------------------------------------- #

    def _load_markers(self) -> None:
        """Adopt the markers a previous process left (a crash before its rebuilds)."""

        with self._lock:
            if self._markers_loaded:
                return
            self._markers_loaded = True
        names = list(self._store.scan_names(KIND, _DIRTY))
        sessions = [name[len(_DIRTY) :] for name in names]
        with self._lock:
            self._dirty.update(sessions)
        for sid in sessions:
            self.schedule(sid)

    def is_dirty(self, session_id: str) -> bool:
        """Whether ``session_id``'s transcript was written since its digest was built."""
        with self._lock:
            return session_id in self._dirty

    def read(self, session_ids: Sequence[str]) -> dict[str, TranscriptDigest | str]:
        """Each clean session's stored digest, else the typed reason it cannot be used."""

        self._load_markers()
        out: dict[str, TranscriptDigest | str] = {}
        wanted: list[str] = []
        for sid in session_ids:
            if self.is_dirty(sid):
                out[sid] = DIRTY_REASON
            else:
                wanted.append(sid)
        if not wanted:
            return out
        if len(wanted) == 1:
            raws = [self._store.get(KIND, _DIGEST + wanted[0])]
        else:
            with ThreadPoolExecutor(min(_READ_FANOUT, len(wanted))) as pool:
                raws = list(pool.map(lambda sid: self._store.get(KIND, _DIGEST + sid), wanted))
        for sid, raw in zip(wanted, raws, strict=True):
            out[sid] = self._decode(sid, raw)
        return out

    def _decode(self, session_id: str, raw: Optional[bytes]) -> TranscriptDigest | str:
        if raw is None:
            self.schedule(session_id)  # fill the index in for the next search
            return MISSING_REASON
        try:
            digest = decode_digest(raw)
        except (zlib.error, msgspec.DecodeError) as exc:
            self._audit(UNREADABLE_REASON, session_id, exc)
            self.schedule(session_id)
            return UNREADABLE_REASON
        if digest is None:
            self.schedule(session_id)
            return FORMAT_REASON
        return digest

    def has_digest(self) -> set[str]:
        """The sessions holding a stored digest (one listing; the migration's resume)."""
        return {name[len(_DIGEST) :] for name in self._store.scan_names(KIND, _DIGEST)}

    @staticmethod
    def _audit(reason: str, session_id: str, exc: BaseException) -> None:
        stream_audit(reason, reason=reason, session_id=session_id, error=repr(exc)[:300])
        logger.warning("recall index reason=%s session=%s error=%r", reason, session_id, exc)


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
            # Under the lane lock, as a transcript read: never a half-written replace.
            with segments._lock_for(session_id, _LANE), transient_reads():
                return assemble_session_messages(arc, session_id)

        current = RecallIndex(store, assemble)
        app.state.recall_index = current
    segments.set_write_observer(current)
    return current


def recall_index_of(app: "FastAPI") -> Optional[RecallIndex]:
    """The app's recall index, or ``None`` (no clio-core store: minimal wiring)."""

    index = getattr(getattr(app, "state", None), "recall_index", None)
    return index if isinstance(index, RecallIndex) else None


def _resident(app: "FastAPI", session_id: str) -> Optional[list["Message"]]:
    peek = getattr(app.state.messages, "peek_resident", None)
    return peek(session_id) if callable(peek) else None


def session_digests(
    app: "FastAPI",
    session_ids: Sequence[str],
    report: DigestReadReport,
    *,
    may_contain: Optional[Callable[[str], bool]] = None,
) -> Iterator[tuple[str, TranscriptDigest]]:
    """Each session's digest, in the order given, a window of sessions at a time.

    ``may_contain`` (a transcript prefilter) lets a fallback skip a session whose
    stored records cannot match: it then yields an empty digest.
    """

    from clio_agent.gact.transcript_reads import read_transcript  # noqa: PLC0415

    index = recall_index_of(app)
    for window in batched(session_ids, _WINDOW):
        resident = {sid: rows for sid in window if (rows := _resident(app, sid)) is not None}
        rest = [sid for sid in window if sid not in resident]
        stored = index.read(rest) if index is not None else dict.fromkeys(rest, UNAVAILABLE_REASON)
        for sid in window:
            if sid in resident:
                report.resident += 1
                yield sid, digest_messages(resident[sid])
                continue
            found = stored[sid]
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
    """Store a digest for every session that has none (the one-time migration).

    Idempotent and resumable: a session already holding a digest is skipped.
    """

    index = recall_index_of(app)
    if index is None:
        arc = getattr(app.state, "arc", None)
        index = attach_recall_index(app, arc) if arc is not None else None
    if index is None:
        raise RuntimeError("the recall index needs the clio-core store (none is attached)")
    index._load_markers()  # a marked session is rebuilt even when it has a digest
    done = {sid for sid in index.has_digest() if not index.is_dirty(sid)}
    built = failed = 0
    for sid in app.state.sessions.ids():
        if sid in done:
            continue
        if index.rebuild(sid):
            built += 1
        else:
            failed += 1
    index.flush()
    return {"sessions": len(done) + built, "built": built, "not_clean": failed}
