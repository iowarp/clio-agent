"""One-time migration of a session stored before the content-lane index existed.

Sessions written before the context view keep their working-set atoms in unchunked
``_events/w/<span>`` partitions with no ``_events/w/_index`` record, and a per-scope
search companion holding only the live text. The first time this process opens such a
session (no index record), :func:`load_or_migrate` lists the session's content-lane
records ONCE (names only when there are none), and if any exist it

1. builds the index from them (each legacy partition is chunk 1 of its family; each
   scope's anchor is its latest summary that retired everything before it);
2. writes the append-only search companion for every atom ever written;
3. removes the old live-text companion records (zero-segment records; a record that
   holds real segments is never touched);
4. puts the index, then audits ``context_view.migrated``.

The atoms themselves are not rewritten: the data stays readable by the same chunk
grammar. A session with no content-lane records is new and gets an empty index (put
on its first write). A record that does not decode is a typed
:class:`~clio_agent.arc.lane_index.ContextIndexError`, never an empty session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import msgspec

from clio_agent.arc.lane_index import (
    INDEX_SCOPE,
    WS_CONTENT_FAMILY,
    ContextIndexError,
    SessionIndex,
    build_index,
    decode_index,
    encode_index,
    parse_chunk,
)
from clio_agent.arc.loop_guard import assert_store_write_off_loop
from clio_agent.arc.schema import Segment, decode_segments
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.arc.working_set_fold import FoldingSegmentStore

__all__ = ["CONTEXT_VIEW_MIGRATED", "load_or_migrate"]

logger = logging.getLogger(__name__)

CONTEXT_VIEW_MIGRATED = "context_view.migrated"


def load_or_migrate(fold: FoldingSegmentStore, session_id: str) -> SessionIndex:
    """The session's index: read it, or migrate a session stored without one.

    Raises:
        ContextIndexError: The index or a legacy lane record does not decode.
    """
    store = fold._store
    raw = store.get("segments", fold._record_name(session_id, INDEX_SCOPE))
    if raw is not None:
        return decode_index(session_id, raw)
    prefix = fold._record_name(session_id, f"{WS_CONTENT_FAMILY}/")
    head = fold._record_name(session_id, "")
    chunks: dict[str, list[Segment]] = {}
    for name, blob in store.scan("segments", prefix=prefix):
        scope = fold.scope_of_record(name[len(head) :])
        if parse_chunk(scope) is None:
            continue
        try:
            chunks[scope] = decode_segments(blob)
        except msgspec.DecodeError as exc:
            raise ContextIndexError(session_id, f"undecodable_legacy_chunk {scope}", exc) from exc
    if not chunks:
        return SessionIndex()
    index = build_index(chunks)
    atoms = sorted((a for segs in chunks.values() for a in segs), key=lambda s: s.logical_time)
    fold._search.add(session_id, atoms)
    removed = _drop_live_text_companions(fold, session_id, list(index.scopes))
    assert_store_write_off_loop("segments.put", scope=INDEX_SCOPE)
    store.put("segments", fold._record_name(session_id, INDEX_SCOPE), encode_index(index))
    stream_audit(
        CONTEXT_VIEW_MIGRATED,
        session_id=session_id,
        chunks=len(chunks),
        atoms=len(atoms),
        scopes=len(index.scopes),
        companions_removed=removed,
    )
    logger.info(
        "context view: migrated session=%s chunks=%d atoms=%d scopes=%d companions_removed=%d",
        session_id,
        len(chunks),
        len(atoms),
        len(index.scopes),
        removed,
    )
    return index


def _drop_live_text_companions(
    fold: FoldingSegmentStore, session_id: str, scopes: list[str]
) -> int:
    """Delete the old per-scope live-text companion records (zero-segment records only)."""
    removed = 0
    for scope in scopes:
        name = fold._record_name(session_id, scope)
        raw = fold._store.get("segments", name)
        if raw is None:
            continue
        try:
            held = decode_segments(raw)
        except msgspec.DecodeError as exc:
            raise ContextIndexError(session_id, f"undecodable_scope_record {scope}", exc) from exc
        if held:
            continue  # a record with real segments is not a companion: never touched
        assert_store_write_off_loop("segments.delete", scope=scope)
        fold._store.delete("segments", name)
        removed += 1
    return removed
