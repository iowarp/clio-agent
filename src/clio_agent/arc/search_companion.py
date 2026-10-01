"""The working set's search companion: append-only text chunks per logical scope.

Working-set content lives on the search-excluded ``_events/w`` lane, so scope search
reads a companion written at ingest (``unified-arc-highway.md`` §2.7). The companion
covers EVERY atom ever written to the scope -- live, deleted or compacted -- so an
agent can find what compaction folded away; whether a hit is still live is decided at
query time from the context view, never by rewriting the companion.

Layout: a scope's companion is a family of chunk records ``_search/<scope>/<first lt>``
(named by the ``logical_time`` of their first atom, so a restarted process simply opens
a new chunk). Each record's body lists its atoms (id, ``logical_time``); its plain-text
search companion is their text, one atom per line. An append re-puts only the active
chunk (bounded by ``arc.search_chunk_atoms``). A write that clio-core does not accept is
a typed :class:`SearchCompanionError` -- the write fails, it is not skipped.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import msgspec

from clio_agent import conf
from clio_agent.arc.context_view import NON_CONTENT_KINDS
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import Segment, segment_text
from clio_agent.arc.storage import ARCStore
from clio_agent.errors import ClioError

__all__ = [
    "SEARCH_FAMILY",
    "ContextSearchHit",
    "SearchAtom",
    "SearchChunkBody",
    "SearchCompanion",
    "SearchCompanionError",
    "is_search_scope",
    "parse_search_scope",
    "search_chunk_scope",
]

SEARCH_FAMILY = "_search"


class SearchCompanionError(ClioError):
    """clio-core did not accept a search-companion write (a typed write failure)."""

    reason = "context_search_write_failed"

    def __init__(self, session_id: str, scope: str, cause: BaseException) -> None:
        super().__init__(
            f"clio-core did not store the search companion of {session_id}/{scope}: {cause}",
            error_type=self.reason,
            details={"session_id": session_id, "scope": scope, "cause": type(cause).__name__},
        )


class SearchChunkBody(msgspec.Struct):
    """A companion chunk's record body: the atoms its text covers, in text order."""

    scope: str
    atoms: list[tuple[str, int]] = msgspec.field(default_factory=list)


@dataclass(frozen=True)
class SearchAtom:
    """One atom a search hit covers."""

    atom_id: str
    logical_time: int
    compacted: bool  # retired (deleted, replaced or summarized) at query time


@dataclass(frozen=True)
class ContextSearchHit:
    """A ranked companion chunk and the atoms it covers."""

    scope: str
    score: float
    atoms: tuple[SearchAtom, ...]


def search_chunk_scope(scope: str, first_lt: int) -> str:
    """The record scope of ``scope``'s companion chunk starting at ``first_lt``."""
    return f"{SEARCH_FAMILY}/{scope}/{first_lt}"


def is_search_scope(scope: str) -> bool:
    """Whether ``scope`` is a companion chunk record (never a scope of its own)."""
    return scope.startswith(f"{SEARCH_FAMILY}/")


def parse_search_scope(record_scope: str) -> tuple[str, int] | None:
    """``(logical scope, first lt)`` of a companion chunk record scope, else ``None``."""
    if not is_search_scope(record_scope):
        return None
    head, _, tail = record_scope[len(SEARCH_FAMILY) + 1 :].rpartition("/")
    if not head or not tail.isdigit():
        return None
    return head, int(tail)


def _capacity() -> int:
    """Atoms per companion chunk before a new one opens (file -> env -> default)."""
    return conf.resolve(
        "arc.search_chunk_atoms",
        env="CLIO_ARC_SEARCH_CHUNK_ATOMS",
        default=16,
        cast=conf.as_int,
    )


@dataclass
class _Chunk:
    record_scope: str
    atoms: list[tuple[str, int]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)


class SearchCompanion:
    """Writes and resolves the per-scope companion chunks of one store."""

    def __init__(self, store: ARCStore, record_name: Callable[[str, str], str]) -> None:
        """Bind the companion to a store and its record naming.

        Args:
            store: The persistence backend (clio-core).
            record_name: ``(session_id, scope) -> record name`` of the segment store.
        """
        self._store = store
        self._record_name = record_name
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], _Chunk] = {}

    def add(self, session_id: str, atoms: Sequence[Segment]) -> None:
        """Index content atoms (each once, in order); the active chunk of each scope is re-put.

        Raises:
            SearchCompanionError: clio-core did not accept a chunk.
        """
        touched: dict[str, _Chunk] = {}
        capacity = _capacity()
        with self._lock:
            for atom in atoms:
                if atom.kind in NON_CONTENT_KINDS:
                    continue
                key = (session_id, atom.scope)
                chunk = self._active.get(key)
                if chunk is None or len(chunk.atoms) >= capacity:
                    if chunk is not None:
                        touched.setdefault(atom.scope, chunk)  # finish the full one
                    chunk = _Chunk(search_chunk_scope(atom.scope, atom.logical_time))
                    self._active[key] = chunk
                chunk.atoms.append((atom.id, atom.logical_time))
                chunk.texts.append(segment_text(atom).replace("\n", " "))
                touched[atom.scope] = chunk
            pending = [
                (scope, chunk.record_scope, list(chunk.atoms), "\n".join(chunk.texts))
                for scope, chunk in touched.items()
            ]
        for scope, record_scope, entries, text in pending:
            self._put(session_id, scope, record_scope, entries, text)

    def _put(
        self,
        session_id: str,
        scope: str,
        record_scope: str,
        entries: list[tuple[str, int]],
        text: str,
    ) -> None:
        assert_store_write_off_loop("segments.put", scope=record_scope)
        body = msgspec.msgpack.encode(SearchChunkBody(scope=scope, atoms=entries))
        try:
            self._store.put(
                "segments",
                self._record_name(session_id, record_scope),
                body,
                search_text=text or " ",
            )
        except LoopThreadStoreWrite:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised typed: the write failed
            raise SearchCompanionError(session_id, scope, exc) from exc

    def chunk_atoms(self, session_id: str, record_scope: str) -> list[tuple[str, int]]:
        """The atoms a companion chunk covers, read from clio-core.

        Raises:
            ValueError: The chunk record is absent or does not decode.
        """
        raw = self._store.get("segments", self._record_name(session_id, record_scope))
        if raw is None:
            raise ValueError(f"companion chunk {record_scope!r} is not stored")
        return list(msgspec.msgpack.decode(raw, type=SearchChunkBody).atoms)

    def forget(self, session_id: str) -> None:
        """Drop the session's active chunks (the next atom opens a new chunk)."""
        with self._lock:
            for key in [k for k in self._active if k[0] == session_id]:
                del self._active[key]

    def clear(self) -> None:
        """Drop every active chunk."""
        with self._lock:
            self._active.clear()
