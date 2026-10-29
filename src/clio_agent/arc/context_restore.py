"""Publish a recovered context to the canonical content lane as one visible change."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import msgspec

from clio_agent.arc.schema import Segment, encode_segments


def unanswered_calls(segments: Sequence[Segment]) -> list[tuple[int, Segment]]:
    """Locate missing results before the next context-step boundary, preserving render order."""
    pending: dict[str, Segment] = {}
    missing: list[tuple[int, Segment]] = []
    for position, segment in enumerate(segments):
        if segment.status != "live":
            continue
        if segment.kind in {"thought", "user", "summary", "recipient_input"}:
            missing.extend((position, call) for call in pending.values())
            pending.clear()
        if segment.kind == "tool_call":
            pending[segment.content["id"]] = segment
        elif segment.kind == "observation":
            identity = segment.content.get("call_id")
            if isinstance(identity, str):
                pending.pop(identity, None)
    missing.extend((len(segments), call) for call in pending.values())
    return missing


def recorded_segments(
    events: Iterable[Mapping[str, Any]],
    session_id: str,
    scope: str,
    retained_turns: set[str],
    confirmed_inputs: Mapping[str, Any],
) -> tuple[list[Segment], bool, bool]:
    """Fold exact-scope trace evidence without promoting input write intents to commits.

    ``confirmed_inputs`` must be read from canonical Core records before restoration.
    Trace order, rather than a possibly reset process clock, decides later edits.
    Unconfirmed recipient inputs remain outside the live context and mailbox receipts.
    """
    from clio_agent.arc.context_view import fold_atoms
    from clio_agent.arc.task_types import RecipientInput

    state: dict[str, Segment] = {}
    unconfirmed = False
    found = False
    for event in events:
        if event.get("session_id") != session_id or event.get("event_type") != "arc.op":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("scope") != scope:
            continue
        found = True
        if payload.get("op") == "restore":
            state.clear()
        for raw in payload.get("segments_written") or []:
            segment = msgspec.convert(raw, Segment)
            if segment.session_id != session_id or segment.scope != scope:
                raise ValueError("Recorded context belongs to another session or scope")
            if segment.turn_id and segment.turn_id not in retained_turns:
                continue
            if segment.kind == "recipient_input":
                if "schema_version" not in segment.content:
                    unconfirmed = True
                    continue
                candidate = RecipientInput.model_validate(segment.content)
                if confirmed_inputs.get(candidate.id) != candidate:
                    unconfirmed = True
                    continue
            state[segment.id] = segment
        for identity in payload.get("segments_tombstoned") or []:
            if identity in state:
                state[identity] = msgspec.structs.replace(
                    state[identity], status="tombstoned", tombstoned_at=int(payload["logical_time"])
                )
    # Explicit tombstones and append-only deletion/compaction atoms both survive.
    tombstones = {
        segment.id: segment for segment in state.values() if segment.status == "tombstoned"
    }
    folded = fold_atoms(list(state.values()), scope, as_of=None, include_tombstoned=True)
    return [tombstones.get(segment.id, segment) for segment in folded], unconfirmed, found


def restore_folded_context(
    store: Any, session_id: str, scope: str, segments: list[Segment]
) -> bool:
    """Stage bounded chunks, then publish their index without a partial live context.

    Existing atoms keep their IDs, order and provider continuation. Explicit
    tombstones become canonical deletion atoms so cold folds cannot revive them.
    The final single-record index write is the visibility boundary.
    """
    from clio_agent.arc.context_view import ContextView
    from clio_agent.arc.lane_index import INDEX_SCOPE, encode_index, note_atom, ws_content_partition
    from clio_agent.arc.search_companion import SearchCompanion
    from clio_agent.arc.storage import PutRecord

    if any(s.session_id != session_id or s.scope != scope for s in segments):
        raise ValueError("Recovery segments belong to another session or scope")
    if not segments:
        return False
    state = store._session(session_id)
    with state.lock:
        if scope in state.index.scopes:
            return False
        store._recover_clock(segments)
        store._emit_op("restore", session_id, scope, written=segments, logical_time=store._new_lt())
        atoms = [msgspec.structs.replace(s, status="live", tombstoned_at=0) for s in segments]
        retired: dict[int, list[str]] = {}
        for segment in segments:
            if segment.status == "tombstoned":
                retired.setdefault(segment.tombstoned_at or segment.logical_time, []).append(
                    segment.id
                )
        for tick, identities in retired.items():
            atoms.append(
                Segment(
                    session_id=session_id,
                    scope=scope,
                    kind="ws_op",
                    content={"op": "delete", "targets": identities},
                    step=-1,
                    order=0,
                    logical_time=tick,
                )
            )
        index = msgspec.convert(msgspec.to_builtins(state.index), type(state.index))
        partition = ws_content_partition("recovery_" + uuid.uuid4().hex)
        records: list[PutRecord] = []
        for start in range(0, len(atoms), 128):
            chunk = partition if start == 0 else f"{partition}/{start // 128 + 1}"
            batch = atoms[start : start + 128]
            for atom in batch:
                note_atom(index, chunk, atom)
            records.append(PutRecord(store._record_name(session_id, chunk), encode_segments(batch)))
        # The private staging chunks are not reachable through the current index.
        # Use an independent staging companion. A failed recovery cannot mutate
        # the live writer's active search chunks; successful recovery stays searchable.
        search = SearchCompanion(store._store, store._record_name)
        store._store.put_many("segments", [*records, *search.stage(session_id, atoms)])
        store._store.put(
            "segments", store._record_name(session_id, INDEX_SCOPE), encode_index(index)
        )
        state.index = index
        state.views[scope] = ContextView(scope, None, atoms)
        store._recover_clock(atoms)
        return True


def restore_plain_context(store: Any, session_id: str, scope: str, segments: list[Segment]) -> bool:
    """Atomically restore an absent scope from validated recorded segments.

    Retain identities and correlations so later rollback and compaction still
    refer to the same records. Never overwrite a concurrently populated scope.
    """
    if any(s.session_id != session_id or s.scope != scope for s in segments):
        raise ValueError("Recovery segments belong to another session or scope")
    with store._lock_for(session_id, scope):
        if store._segs(session_id, scope) or not segments:
            return False
        store._recover_clock(segments)
        store._finish_write(session_id, scope, "restore", list(segments), written=segments)
        return True


class PlainContextRestore:
    """Restore support shared by plain scope stores and the folded store facade."""

    def restore_empty(self, session_id: str, scope: str, segments: list[Segment]) -> bool:
        """Restore a missing scope, preserving recorded identities and order."""
        return restore_plain_context(self, session_id, scope, segments)
