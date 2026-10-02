"""One-time migration of a session stored before the content-lane index existed.

Sessions written before the context view keep their working-set atoms in unchunked
``_events/w/<span>`` partitions with no ``_events/w/_index`` record, and a per-scope
search companion holding only the live text. The first time this process opens such a
session (no index record), :func:`load_or_migrate` lists the session's content-lane
records ONCE (names only when there are none), and if any exist it

1. migrates the old loop's step atoms (below);
2. builds the index from them (each legacy partition is chunk 1 of its family; each
   scope's anchor is its latest summary that retired everything before it);
3. writes the append-only search companion for every atom ever written;
4. removes the old live-text companion records (zero-segment records; a record that
   holds real segments is never touched);
5. puts the index, then audits ``context_view.migrated``.

**Legacy step atoms.** The loop before the rebuild wrote each executed call as a
``tool_call`` ``{name, args}`` with no id, immediately followed by its own
``observation`` ``{text}`` with no ``call_id``; the fold matches results to calls by id
and refuses an id-less call. Each live id-less call gets the deterministic id
``legacy_<call atom id>``, and the id-less observation right after it (in the scope's
live render order) the same ``call_id``. A legacy call with no adjacent observation (a
cancelled turn) gets its id and no output: the fold then fails it typed
``unanswered_call``, as for any unanswered call. Nothing else is touched, so an id-less
call that is not a legacy pair still fails the fold typed ``malformed_call``.

A failed turn's reason the old loop recorded as a call-less id-less observation
``[turn escalated] <reason>: <error>``; it becomes the note the rebuilt loop records
for a failed turn (a ``user`` segment, actor ``algorithm``, source ``turn_escalated``),
so the model and the transcript read the same note. Any other observation that answers
no call still fails the fold typed ``orphan_observation``.

Every change is a ``replace`` atom (the original's ``derived_from``, at its render
slot) appended to the original's chunk and recorded on ``arc.op``, audited
``context_view.legacy_call_ids`` and flagged. The atoms themselves are not rewritten:
the data stays readable by the same chunk grammar, and an as-of read before the
migration shows the originals.

A session with no content-lane records is new and gets an empty index (put
on its first write). A record that does not decode is a typed
:class:`~clio_agent.arc.lane_index.ContextIndexError`, never an empty session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import msgspec

from clio_agent.arc.context_view import fold_atoms
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
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import Segment, SegmentKind, decode_segments, encode_segments
from clio_agent.arc.segments import ArcPersistError
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.arc.working_set_fold import FoldingSegmentStore

__all__ = ["CONTEXT_VIEW_LEGACY_CALL_IDS", "CONTEXT_VIEW_MIGRATED", "load_or_migrate"]

logger = logging.getLogger(__name__)

CONTEXT_VIEW_MIGRATED = "context_view.migrated"
CONTEXT_VIEW_LEGACY_CALL_IDS = "context_view.legacy_call_ids"
LEGACY_CALL_ID_PREFIX = "legacy_"
# The old loop's failed-turn note (``reactv2_events.instrumented_forward``, c7a87d73).
LEGACY_ESCALATION_PREFIX = "[turn escalated] "


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
    _migrate_legacy_steps(fold, session_id, chunks)
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


def _migrate_legacy_steps(
    fold: FoldingSegmentStore, session_id: str, chunks: dict[str, list[Segment]]
) -> None:
    """Give every live legacy id-less call (and its adjacent observation) its id, and
    turn every live legacy escalation observation into the loop's failed-turn note.

    Appends the ``replace`` atoms to the original's chunk in ``chunks`` and puts each
    changed chunk, so the index built next lists them (see the module docstring).

    Raises:
        ContextOpLogError: The ``arc.op`` record of a replacement was not written.
        ArcPersistError: A changed chunk was not stored.
    """
    atoms = [a for segs in chunks.values() for a in segs]
    fold._recover_clock(atoms)  # replacements are minted after every legacy atom
    chunk_of = {a.id: name for name, segs in chunks.items() for a in segs}
    changed: set[str] = set()
    pairs = unanswered = notes = 0
    for scope in sorted({a.scope for a in atoms}):
        live = fold_atoms(atoms, scope, as_of=None, include_tombstoned=False)
        pending: list[tuple[Segment, SegmentKind, dict[str, Any]]] = []
        paired: set[str] = set()
        for pos, seg in enumerate(live):
            if seg.kind == "observation" and seg.id not in paired:
                text = seg.content.get("text")
                if not seg.content.get("call_id") and isinstance(text, str):
                    if text.startswith(LEGACY_ESCALATION_PREFIX):
                        note = text.removeprefix(LEGACY_ESCALATION_PREFIX)
                        record = {"text": note, "source": "turn_escalated", "actor": "algorithm"}
                        pending.append((seg, "user", record))
                        notes += 1
                continue
            if seg.kind != "tool_call" or seg.content.get("id"):
                continue
            call_id = f"{LEGACY_CALL_ID_PREFIX}{seg.id}"
            pending.append((seg, "tool_call", {**seg.content, "id": call_id}))
            nxt = live[pos + 1] if pos + 1 < len(live) else None
            if nxt is not None and nxt.kind == "observation" and not nxt.content.get("call_id"):
                pending.append((nxt, "observation", {**nxt.content, "call_id": call_id}))
                paired.add(nxt.id)
                pairs += 1
            else:
                unanswered += 1
        for original, kind, content in pending:
            chunk = chunk_of[original.id]
            chunks[chunk].append(_replace_atom(fold, session_id, original, kind, content))
            changed.add(chunk)
    if not changed:
        return
    for chunk in sorted(changed):
        assert_store_write_off_loop("segments.put", scope=chunk)
        try:
            fold._store.put(
                "segments", fold._record_name(session_id, chunk), encode_segments(chunks[chunk])
            )
        except LoopThreadStoreWrite:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised typed; the write failed
            raise ArcPersistError(session_id, chunk, exc) from exc
    stream_audit(
        CONTEXT_VIEW_LEGACY_CALL_IDS,
        session_id=session_id,
        pairs=pairs,
        unanswered=unanswered,
        notes=notes,
        chunks=len(changed),
    )
    trace.event(
        "LEGACY-CALL-IDS",
        "session=%s pairs=%d unanswered=%d notes=%d chunks=%d (the old loop's id-less calls "
        "given deterministic %s<atom id> ids, an unanswered one fails the fold typed; its "
        "escalation observations recorded as turn_escalated notes)",
        session_id,
        pairs,
        unanswered,
        notes,
        len(changed),
        LEGACY_CALL_ID_PREFIX,
    )


def _replace_atom(
    fold: FoldingSegmentStore,
    session_id: str,
    original: Segment,
    kind: SegmentKind,
    content: dict[str, Any],
) -> Segment:
    """The ``replace`` atom of ``kind`` carrying ``content`` at ``original``'s render
    slot, its ``arc.op`` recorded first (as :meth:`FoldingSegmentStore.replace` does)."""
    atom = fold._make_atom(
        session_id,
        original.scope,
        kind,
        content,
        order=original.order,
        step=original.step,
        derived_from=[original.id],
        token_count=original.token_count,
        turn_id=original.turn_id,
        expert_span_id=original.expert_span_id,
        run_span_id=original.run_span_id,
    )
    fold._emit_op(
        "replace",
        session_id,
        original.scope,
        written=[atom],
        tombstoned=[original.id],
        step=atom.step,
        derived_from=[original.id],
        logical_time=atom.logical_time,
    )
    return atom
