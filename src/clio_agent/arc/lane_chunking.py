"""Generic chunk-family lane grammar for the reserved ``_events`` scope tree (#1339).

Two lanes already ride ``_events``: the semantic-event log (``_events``, chunk 1 the
legacy bare scope) and the ``message_part`` atom lane (``_events/m``, chunk 1 the bare
partition). Both need the SAME thing: a single append re-encodes only the ACTIVE chunk
(O(chunk)) instead of the whole growing scope (O(N) per append, O(N^2) per session), and
readers must concatenate the family back into one ordered stream without ever falling
back to a body-downloading ``scan`` (:meth:`~clio_agent.arc.storage.ClioCoreStore.scan`
pulls every record body for the prefix; a ``get`` of a missing record is one cheap
``GetBlobSize`` and is cached in :attr:`SegmentStore._loaded`). This module is the ONE
owner of that grammar so neither lane re-implements it (RULE 4 / RULE 5: no fifth store,
no accretion onto the already-baselined ``memory.py`` / ``segments.py``).

Grammar (injective, disjoint from every sibling lane under the same ``base``):
    * Chunk 1 is the bare ``base`` scope itself (the legacy, pre-chunking shape — a
      migration by construction: an unchunked lane already looks like "one chunk").
    * Chunk ``N >= 2`` is ``<base>/<N>`` with ``N`` a canonical decimal (no leading
      zero, no sign, no non-digit tail). ``<base>/edge``, ``<base>/02``, ``<base>/1``
      are therefore NOT chunks of ``base`` — a sibling partition (e.g. the live-edge
      checkpoint scope) or a malformed name never collides with the family.

Ordering: chunk (discovery) order is NOT append order under concurrency. A single
writer thread fills chunks in ascending order, but with multiple concurrent writers one
thread can reserve the LAST slot of chunk N while a second thread rolls to chunk N+1 and
its ``store.append`` lands FIRST — chunk N then holds a later ``logical_time`` than
chunk N+1, even though the dense walk still discovers N before N+1. The true, total
append order is :class:`~clio_agent.arc.segments.SegmentStore`'s ``logical_time`` — a
store-wide monotonic clock, recovered past the persisted max on cold load, ticked once
per actual ``append()`` (never at ``chunk_for_append`` reservation time) — so every
reader that needs append order (:func:`lane_segments`) SORTS the dense-walk
concatenation by it rather than trusting chunk order alone.

Dense-prefix invariant (documented + tested): chunk ``i + 1`` exists only once chunk
``i`` reached ``capacity``, and chunks are removed only as a whole family. This is what
lets every reader here WALK ascending chunk indices via
:meth:`~clio_agent.arc.segments.SegmentStore.list_segments` (a cheap per-scope ``get``,
never a ``scan``) and stop at the first absent/empty one, instead of discovering the
family through an expensive scope scan.

Writer cursor: kept OUTSIDE the store (a module-level, per-``(store, session_id, base)``
cache) so the append hot path never re-derives ``(index, count)`` from disk on a warm
process. The cursor is trusted only while its active chunk is still present in the
store's own ``_loaded`` cache — every eraser (``drop_scope`` / ``release`` / ``clear``)
discards that key, which gives the invalidation a zero-extra-RPC signal for free: a
stale cursor (its chunk erased out from under it) is detected without touching the
store again, so the next append safely recovers instead of silently writing past a hole
(the reviewer risk this module is built to close first).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.arc.schema import Segment
    from clio_agent.arc.segments import SegmentStore

# Look-ahead-past-the-first-hole audited when an erase finds a chunk beyond a gap (an
# index the dense walk skipped over as absent) — see :func:`drop_lane`.
ATOM_LANE_CHUNK_GAP = "atom_lane_chunk_gap"
# Emitted (debug severity, greppable) whenever a cached writer cursor is discarded by an
# erase so the next append for that (session, base) recovers from scratch.
LANE_CHUNK_CURSOR_RESET = "lane_chunk_cursor_reset"

# How many indices past the first absent chunk :func:`drop_lane` still probes for a
# surviving chunk record (a hole the dense-prefix invariant says should never occur, but
# an erase must still finish cleanly if one does — never leave an orphaned chunk behind).
_HOLE_LOOKAHEAD = 2

# A canonical chunk-index tail: decimal digits, no leading zero (chunk 1 is the bare
# base, never "<base>/1", so the smallest legal tail is "2").
_CANONICAL_TAIL = re.compile(r"^[1-9][0-9]*$")


@dataclass
class _LaneCursor:
    """The active-chunk writer state for one ``(session_id, base)`` lane."""

    index: int
    count: int


# One lock guards the whole cursor structure (module-level, per store instance) — the
# critical section is a dict lookup plus, on a cold/invalidated cursor, the same
# recovery walk the old per-ARCMemory ``_events_writer_lock`` already serialized a
# store's appends through, so this is not a new contention shape, only a shared one.
_CURSORS_LOCK = threading.Lock()
_CURSORS: "WeakKeyDictionary[SegmentStore, dict[tuple[str, str], _LaneCursor]]" = (
    WeakKeyDictionary()
)


# --------------------------------------------------------------------------- #
# Pure grammar
# --------------------------------------------------------------------------- #


def chunk_scope(base: str, index: int) -> str:
    """Scope name for the ``index``-th chunk of ``base`` (1-indexed).

    Args:
        base: The lane's base scope (e.g. ``"_events"`` or ``"_events/m"``).
        index: 1-indexed chunk number.

    Returns:
        ``base`` for chunk 1, ``f"{base}/{index}"`` for chunk ``N >= 2``.
    """
    return base if index <= 1 else f"{base}/{index}"


def is_chunk_of(base: str, scope: str) -> bool:
    """Whether ``scope`` is a legitimate chunk of ``base``'s family.

    True for the bare ``base`` (chunk 1) and for ``base/N`` where ``N`` is a canonical
    decimal ``>= 2``. False for a sibling partition (``base/edge``), a non-canonical
    tail (``base/02``), or ``base/1`` (chunk 1 is never spelled that way) — the
    collision proof: no sibling lane or malformed name is ever swept into this family.

    Args:
        base: The lane's base scope.
        scope: The scope to test.

    Returns:
        Whether ``scope`` belongs to ``base``'s chunk family.
    """
    if scope == base:
        return True
    prefix = f"{base}/"
    if not scope.startswith(prefix):
        return False
    tail = scope[len(prefix) :]
    return bool(_CANONICAL_TAIL.match(tail)) and int(tail) >= 2


def chunk_index(base: str, scope: str) -> int:
    """Chunk index of ``scope`` within ``base``'s family (inverse of :func:`chunk_scope`).

    A scope that is not a legitimate chunk of ``base`` (see :func:`is_chunk_of`) maps to
    ``1`` so callers that use this purely as a total-ordering sort key never raise on an
    unexpected or malformed name.

    Args:
        base: The lane's base scope.
        scope: A scope to index.

    Returns:
        The 1-indexed chunk number, or ``1`` when ``scope`` is not a chunk of ``base``.
    """
    if not is_chunk_of(base, scope) or scope == base:
        return 1
    return int(scope[len(base) + 1 :])


# --------------------------------------------------------------------------- #
# Dense-prefix discovery (never a body-downloading scan)
# --------------------------------------------------------------------------- #


def _dense_walk(
    store: "SegmentStore", session_id: str, base: str
) -> list[tuple[str, list["Segment"]]]:
    """Ascending chunk walk, stopping at the first absent (or persisted-empty) chunk.

    Each step is one :meth:`~clio_agent.arc.segments.SegmentStore.list_segments` call —
    a cached per-scope ``get`` (cheap even cold: one ``GetBlobSize`` on a miss), never a
    ``scan``. Relies on the dense-prefix invariant: a present chunk never follows an
    absent one, so the first empty result ends the family.

    Note (documented risk, not fixed here): a chunk emptied entirely by
    :meth:`SegmentStore._persist`'s non-poisoning drop (every segment failed to encode)
    is indistinguishable from an absent one and truncates the walk — it never causes an
    erroneous erase, only a short read of an already-degraded scope.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.

    Returns:
        ``[(scope, segments_including_tombstoned), ...]`` for every present chunk, in
        ascending chunk order.
    """
    out: list[tuple[str, list["Segment"]]] = []
    index = 1
    while True:
        scope = chunk_scope(base, index)
        segs = store.list_segments(session_id, scope, include_tombstoned=True)
        if not segs:
            return out
        out.append((scope, segs))
        index += 1


def lane_scopes(store: "SegmentStore", session_id: str, base: str) -> list[str]:
    """The lane's present chunk scopes, in ascending chunk order (``[]`` if none).

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.

    Returns:
        The dense-prefix scope list, in ascending CHUNK (discovery) order — NOT
        necessarily append order under concurrent writers (see the module docstring's
        Ordering paragraph); a caller that needs append order reads segments through
        :func:`lane_segments`, which sorts by ``logical_time``.
    """
    return [scope for scope, _segs in _dense_walk(store, session_id, base)]


def lane_segments(
    store: "SegmentStore", session_id: str, base: str, *, include_tombstoned: bool = False
) -> list["Segment"]:
    """Concatenate the lane's chunks into one APPEND-ORDER segment list.

    Chunk (discovery) order is NOT append order under concurrent writers (see the module
    docstring's Ordering paragraph): a thread can reserve the last slot of chunk N while
    another thread's append into the just-opened chunk N+1 lands first, giving chunk N a
    later ``logical_time`` than chunk N+1. So the dense-walk concatenation is re-sorted
    by ``logical_time`` (a stable sort; the clock is store-wide monotonic and total, so
    this recovers the true append order regardless of which chunk each segment landed
    in) — this reproduces exactly what a single ever-growing scope would have rendered,
    chunking (and any writer-side race) invisible to every reader.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.
        include_tombstoned: Include tombstoned segments (replay / provenance reads).

    Returns:
        The concatenated segment list, sorted by ``logical_time`` (append order).
    """
    out: list["Segment"] = []
    for _scope, segs in _dense_walk(store, session_id, base):
        if include_tombstoned:
            out.extend(segs)
        else:
            out.extend(s for s in segs if s.status == "live")
    out.sort(key=lambda s: s.logical_time)
    return out


def lane_has_segments(store: "SegmentStore", session_id: str, base: str) -> bool:
    """Whether the lane holds ANY live segment — ONE read (chunk 1 only).

    Chunk 1 is always filled first (the dense-prefix invariant), so if the lane has any
    live content at all, chunk 1 has at least one live segment; checking only it avoids
    walking the whole family just to answer a boolean.

    PREMISE this correctness depends on: no code path tombstones an INDIVIDUAL atom of
    this lane. The only erasers are :func:`drop_lane` (the whole family, chunk 1
    included) and :meth:`SegmentStore._persist`'s non-poisoning drop (an encode failure,
    also whole-chunk). If a future writer ever adds a per-atom delete/tombstone (e.g. a
    single retracted atom via ``SegmentStore.delete``), chunk 1 could end up with zero
    live segments while a later chunk still holds live ones, and this one-read shortcut
    would silently return ``False`` for a non-empty lane — check the WHOLE family
    (:func:`lane_segments`) instead if that ever becomes possible.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.

    Returns:
        Whether chunk 1 currently holds a live segment.
    """
    return bool(store.list_segments(session_id, base, include_tombstoned=False))


# --------------------------------------------------------------------------- #
# Writer cursor (the ONLY append seam)
# --------------------------------------------------------------------------- #


def recover_writer(store: "SegmentStore", session_id: str, base: str) -> tuple[int, int]:
    """Cold-start cursor: resume at the last persisted chunk instead of overwriting it.

    Walks the family (:func:`lane_scopes`); with none persisted the cursor starts at
    chunk 1, empty. Otherwise it resumes the last chunk with its current (including
    tombstoned) segment count, so the next append continues it until it rolls.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.

    Returns:
        ``(chunk_index, segments_in_chunk)`` to resume from.
    """
    walk = _dense_walk(store, session_id, base)
    if not walk:
        return (1, 0)
    return (len(walk), len(walk[-1][1]))


def _cursor_key(session_id: str, base: str) -> tuple[str, str]:
    return (session_id, base)


def chunk_for_append(store: "SegmentStore", session_id: str, base: str, *, capacity: int) -> str:
    """Reserve a slot in the lane's active chunk and return its scope — THE writer seam.

    Advances the per-``(store, session_id, base)`` cursor, rolling to the next chunk
    once the active one has reached ``capacity`` segments (so an append re-encodes only
    the active chunk, not the whole lane). The cursor is trusted only while its active
    chunk is still present in the store's own ``_loaded`` cache; an eraser
    (``drop_scope`` / ``release`` / ``clear``) discards that key as a side effect, which
    this function reads as "the cursor is stale" and recovers from
    (:func:`recover_writer`) rather than silently writing past a hole left by an erase
    that ran between two appends.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.
        capacity: Segments held per chunk before rolling to the next.

    Returns:
        The scope to append this ONE segment into.
    """
    key = _cursor_key(session_id, base)
    with _CURSORS_LOCK:
        store_cursors = _CURSORS.setdefault(store, {})
        cur = store_cursors.get(key)
        if cur is None or (session_id, chunk_scope(base, cur.index)) not in store._loaded:
            index, count = recover_writer(store, session_id, base)
            cur = _LaneCursor(index=index, count=count)
        if cur.count >= capacity:
            cur = _LaneCursor(index=cur.index + 1, count=0)
        cur.count += 1
        store_cursors[key] = cur
        return chunk_scope(base, cur.index)


def forget_cursor(store: "SegmentStore", session_id: str, base: str) -> None:
    """Discard the cached writer cursor for ``(session_id, base)``, if any.

    Called after a family erase (:func:`drop_lane`) so the state is not left pointing at
    an index that no longer exists; harmless even without this call (:func:`chunk_for_append`
    already re-validates against ``store._loaded``), but keeps the cursor structure
    honest and gives the reset a greppable, typed trace.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.
    """
    key = _cursor_key(session_id, base)
    with _CURSORS_LOCK:
        store_cursors = _CURSORS.get(store)
        popped = store_cursors.pop(key, None) if store_cursors is not None else None
    if popped is not None:
        stream_audit(LANE_CHUNK_CURSOR_RESET, session_id=session_id, base=base)


# --------------------------------------------------------------------------- #
# Lifecycle erase
# --------------------------------------------------------------------------- #


def drop_lane(store: "SegmentStore", session_id: str, base: str) -> int:
    """Erase every present chunk of the lane, tolerating a hole, then forget the cursor.

    Drops each chunk the dense walk finds, then probes :data:`_HOLE_LOOKAHEAD` (2)
    indices past the first absent one for a chunk that survived past a gap (never
    expected under the dense-prefix invariant, but an erase must still finish cleanly
    and audibly if one is found — a read would otherwise have stopped short of it).

    LIMIT, stated plainly: the look-ahead is exactly :data:`_HOLE_LOOKAHEAD` deep. A hole
    of 3 or more consecutive absent indices leaves any surviving chunk beyond it an
    ORPHAN — dropped from neither the dense walk nor the look-ahead, so it is never
    erased (though it was already unreachable by every reader, which also stops at the
    dense-prefix boundary). Measured: chunks present at 1, 2, 5 (a 2-index hole at 3, 4)
    erase chunks 1 and 2 only; chunk 5 is left an orphan on disk. This is a bounded,
    documented gap in an already-anomalous case the dense-prefix invariant says should
    never occur — not a silent data-loss risk (nothing reachable is left unread; the
    orphan is unreachable dead weight, not a resurrection hazard).

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.

    Returns:
        Total segments dropped across every erased chunk.
    """
    walk = _dense_walk(store, session_id, base)
    dropped = sum(store.drop_scope(session_id, scope) for scope, _segs in walk)
    first_absent = len(walk) + 1
    for offset in range(_HOLE_LOOKAHEAD):
        index = first_absent + offset
        scope = chunk_scope(base, index)
        segs = store.list_segments(session_id, scope, include_tombstoned=True)
        if segs:
            stream_audit(
                ATOM_LANE_CHUNK_GAP,
                session_id=session_id,
                base=base,
                scope=scope,
                hole_index=first_absent,
            )
            dropped += store.drop_scope(session_id, scope)
    forget_cursor(store, session_id, base)
    return dropped


__all__ = [
    "ATOM_LANE_CHUNK_GAP",
    "LANE_CHUNK_CURSOR_RESET",
    "chunk_for_append",
    "chunk_index",
    "chunk_scope",
    "drop_lane",
    "forget_cursor",
    "is_chunk_of",
    "lane_has_segments",
    "lane_scopes",
    "lane_segments",
    "recover_writer",
]
