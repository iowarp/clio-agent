"""The session index of the working-set content lane (``_events/w/_index``).

The content lane is partitioned by expert span (``_events/w/<span>``) and each
partition is a chunk family (``lane_chunking`` grammar: chunk 1 is the bare partition,
chunk N is ``<partition>/<N>``). Finding a scope's atoms used to mean scanning every
``segments`` record of the session (downloading each blob to read its name) and
folding all of them. The index replaces that scan on the read path: ONE record per
session listing

* every chunk with its partition, chunk number, first ``logical_time`` and the logical
  scopes it holds atoms of;
* per logical scope, its anchor (:class:`~clio_agent.arc.context_view.AnchorEntry`)
  and the chunks holding its atoms at or after the anchor.

A cold read gets the index (one get), then only the chunks listed for the scope.

Write order makes the index a SUPERSET of what is stored, never a subset: the index is
put before the chunk it newly lists. A write that fails after its index update leaves
a listed chunk without that atom (or absent), which every reader treats as holding
nothing -- the failed write was already raised typed at write time. The index is put
only when it changes: a new chunk, a scope's first atom in a chunk, an anchor move.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

import msgspec

from clio_agent.arc.context_view import NON_CONTENT_KINDS, AnchorEntry, retirements
from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.schema import Segment
from clio_agent.errors import ClioError

__all__ = [
    "INDEX_SCOPE",
    "WS_CONTENT_FAMILY",
    "ChunkEntry",
    "ContextIndexError",
    "ScopeEntry",
    "SessionIndex",
    "build_index",
    "chunks_holding",
    "decode_index",
    "encode_index",
    "find_anchor",
    "is_ws_content_scope",
    "note_atom",
    "parse_chunk",
    "set_anchor",
    "ws_content_partition",
]

# The content lane of the canonical log: a chunk family UNDER ``_events`` (so
# ``is_events_scope`` is True -- search-excluded + lifecycle-erased with the log),
# partitioned by ``expert_span_id`` so parallel experts do not contend on one lock.
WS_CONTENT_FAMILY = f"{EVENTS_SCOPE}/w"
# The session index record (not a chunk: decoded only by :func:`decode_index`).
INDEX_SCOPE = f"{WS_CONTENT_FAMILY}/_index"

_CANONICAL_N = re.compile(r"^[1-9][0-9]*$")


class ContextIndexError(ClioError):
    """The content-lane index (or a legacy lane being migrated) cannot be read."""

    reason = "context_index_unreadable"

    def __init__(self, session_id: str, problem: str, cause: BaseException | None = None) -> None:
        detail = f": {cause}" if cause is not None else ""
        super().__init__(
            f"the context index of session {session_id!r} cannot be read ({problem}){detail}",
            error_type=self.reason,
            details={
                "session_id": session_id,
                "problem": problem,
                "cause": type(cause).__name__ if cause is not None else "",
            },
        )


class ChunkEntry(msgspec.Struct):
    """One stored chunk of the content lane."""

    partition: str
    index: int
    min_lt: int
    scopes: list[str] = msgspec.field(default_factory=list)


class ScopeEntry(msgspec.Struct):
    """One logical scope: its anchor and the chunks holding its atoms from the anchor on."""

    chunks: list[str] = msgspec.field(default_factory=list)
    anchor: AnchorEntry | None = None


class SessionIndex(msgspec.Struct):
    """The whole index record of one session."""

    version: int = 1
    chunks: dict[str, ChunkEntry] = msgspec.field(default_factory=dict)
    scopes: dict[str, ScopeEntry] = msgspec.field(default_factory=dict)


def ws_content_partition(expert_span_id: str) -> str:
    """Physical partition (chunk 1 scope) of the content lane for one ``expert_span_id``.

    Concurrent experts write disjoint partitions (``_events/w/<span>``) so their
    appends take different per-scope locks. An empty span (unstamped writers, tests)
    maps to the shared ``_events/w/_`` partition.
    """
    return f"{WS_CONTENT_FAMILY}/{expert_span_id or '_'}"


def is_ws_content_scope(scope: str) -> bool:
    """Whether ``scope`` is under the content lane (a chunk or the index)."""
    return scope == WS_CONTENT_FAMILY or scope.startswith(f"{WS_CONTENT_FAMILY}/")


def parse_chunk(scope: str) -> tuple[str, int] | None:
    """``(partition, chunk number)`` of a content-lane chunk scope, else ``None``.

    ``_events/w/<span>`` is chunk 1 of its partition and ``_events/w/<span>/<N>`` (N a
    canonical decimal >= 2) chunk N. The index record and anything outside the lane
    are not chunks.
    """
    if scope == INDEX_SCOPE or not scope.startswith(f"{WS_CONTENT_FAMILY}/"):
        return None
    parts = scope[len(WS_CONTENT_FAMILY) + 1 :].split("/")
    if len(parts) == 1 and parts[0]:
        return scope, 1
    if len(parts) == 2 and parts[0] and _CANONICAL_N.match(parts[1]) and int(parts[1]) >= 2:
        return f"{WS_CONTENT_FAMILY}/{parts[0]}", int(parts[1])
    return None


def encode_index(index: SessionIndex) -> bytes:
    """The index record's bytes."""
    return msgspec.msgpack.encode(index)


def decode_index(session_id: str, raw: bytes) -> SessionIndex:
    """Decode an index record.

    Raises:
        ContextIndexError: The record does not decode (never read as an empty session).
    """
    try:
        return msgspec.msgpack.decode(raw, type=SessionIndex)
    except msgspec.DecodeError as exc:
        raise ContextIndexError(session_id, "undecodable_index", exc) from exc


def note_atom(index: SessionIndex, chunk: str, atom: Segment) -> bool:
    """Record that ``atom`` is being written to ``chunk``; ``True`` if the index changed.

    Raises:
        ValueError: ``chunk`` is not a content-lane chunk scope.
    """
    parsed = parse_chunk(chunk)
    if parsed is None:
        raise ValueError(f"not a content-lane chunk: {chunk!r}")
    changed = False
    entry = index.chunks.get(chunk)
    if entry is None:
        partition, number = parsed
        entry = ChunkEntry(partition=partition, index=number, min_lt=atom.logical_time)
        index.chunks[chunk] = entry
        changed = True
    if atom.scope not in entry.scopes:
        entry.scopes.append(atom.scope)
        changed = True
    scope = index.scopes.setdefault(atom.scope, ScopeEntry())
    if chunk not in scope.chunks:
        scope.chunks.append(chunk)
        changed = True
    return changed


def set_anchor(index: SessionIndex, scope: str, anchor: AnchorEntry) -> None:
    """Move ``scope``'s anchor; its only atom at or after the anchor is the summary."""
    index.scopes[scope] = ScopeEntry(chunks=[anchor.chunk], anchor=anchor)


def chunks_holding(index: SessionIndex, scope: str) -> list[str]:
    """Every chunk holding any atom of ``scope`` (the full-log read), in index order."""
    return [name for name, entry in index.chunks.items() if scope in entry.scopes]


def find_anchor(
    atoms: Sequence[Segment], scope: str, chunk_of: Mapping[str, str]
) -> AnchorEntry | None:
    """The latest summary atom of ``scope`` that retired every content atom before it.

    Args:
        atoms: The scope's whole log (content and op records, any order).
        scope: The logical scope.
        chunk_of: ``{atom id: chunk scope}`` for the atoms.

    Returns:
        The anchor, or ``None`` when no summary qualifies.
    """
    tomb = retirements(atoms, scope)
    content = sorted(
        (a for a in atoms if a.scope == scope and a.kind not in NON_CONTENT_KINDS),
        key=lambda s: s.logical_time,
    )
    best: AnchorEntry | None = None
    latest_retirement = 0  # max retirement clock over the atoms before the candidate
    all_retired = True
    lo: float | None = None
    hi: float | None = None
    for a in content:
        lo = a.order if lo is None or a.order < lo else lo
        hi = a.order if hi is None or a.order > hi else hi
        if a.kind == "summary" and all_retired and latest_retirement <= a.logical_time:
            best = AnchorEntry(
                atom_id=a.id,
                logical_time=a.logical_time,
                chunk=chunk_of[a.id],
                min_order=lo,
                max_order=hi,
            )
        retired_at = tomb.get(a.id)
        if retired_at is None:
            all_retired = False
        else:
            latest_retirement = max(latest_retirement, retired_at)
    return best


def build_index(chunks: Mapping[str, Iterable[Segment]]) -> SessionIndex:
    """The index of a lane read whole (the migration of a session stored without one).

    Args:
        chunks: ``{chunk scope: its atoms}`` for every chunk of the session's lane.

    Returns:
        The index, with each scope's anchor and post-anchor chunk list.
    """
    index = SessionIndex()
    chunk_of: dict[str, str] = {}
    by_scope: dict[str, list[Segment]] = {}
    for name in sorted(chunks, key=lambda n: parse_chunk(n) or (n, 0)):
        atoms = sorted(chunks[name], key=lambda s: s.logical_time)
        for atom in atoms:
            note_atom(index, name, atom)
            chunk_of[atom.id] = name
            by_scope.setdefault(atom.scope, []).append(atom)
        entry = index.chunks.get(name)
        if entry is not None and atoms:
            entry.min_lt = atoms[0].logical_time
    for scope, atoms in by_scope.items():
        anchor = find_anchor(atoms, scope, chunk_of)
        if anchor is None:
            continue
        index.scopes[scope] = ScopeEntry(
            chunks=sorted({chunk_of[a.id] for a in atoms if a.logical_time >= anchor.logical_time}),
            anchor=anchor,
        )
    return index
