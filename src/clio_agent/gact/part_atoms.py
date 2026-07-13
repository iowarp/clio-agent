"""The ``message_part`` atom family — wire-identity atoms on the canonical log (#737 S4).

The unified-ARC highway (``docs/design/unified-arc-highway.md`` §2.3, §2.8c, §4.2 step
4) collapses the four parallel conversation materializations onto ONE
operation-sourced log. Today the full assistant ``Message`` — its ``msg_``/``part_``
ids, ``created_at``, ``stream_source``, ``usage``, ``expert_handoff`` — lives ONLY
inside the ``final_message`` byte-copy embedded in the durable ``turn.completed``
event (``turn_finalize.py``) and inside the gact messages store. A later slice (S5)
kills that byte-copy and makes ``GET /messages`` + the SSE spine assemble a Message
BY REFERENCE from part atoms. That read switch cannot land until the atoms carrying
the wire identity already exist on the log — this slice PROVISIONS them.

This module mints a ``message_part`` atom family and **dual-writes** it alongside the
existing ``final_message`` at the message-persist seam (``_append_session_message``):
no reader switches here (design §4.2: "not yet replacing it"), so the atoms are
INVISIBLE on every served surface until S5. The slice's gate (design §4.2 step 4) is
the reproducibility proof in :func:`reproduce_message_wire`: EVERY wire field of the
persisted message (``Message.model_dump(exclude_none=True)`` — the exact shape of
``final_message``) must be reconstructable from the part atoms alone.

Design decisions (each answering a named constraint):

* **Minted once, stored durably (§2.3, §2.8c).** The atom copies the ids/timestamps
  the message ALREADY carries (``message.id`` / ``part.id`` / ``created_at``) — it
  never mints its own — and stores them verbatim, so eviction + rehydration
  reproduces ``reload == live`` identity byte-exactly. A re-mint on read would break
  that invariant; :func:`reproduce_message_wire` reads the stored ids, never a fresh
  ``uuid4``.
* **Additive kind, msgspec back-compat (§2.3).** ``message_part`` is a NEW
  :data:`~clio_agent.arc.schema.SegmentKind` member (additive to the Literal, exactly
  as the S2 fold added ``ws_op`` / ``step_open``): old records still decode (their
  kinds are unchanged), and the new kind is produced only by new code.
* **On the ``_events/m`` sibling lane (§2.10), raw (§2.9).** Atoms ride a dedicated
  partition of the reserved ``_events`` chunk family (:data:`MESSAGE_PART_SCOPE`), so
  they are search-excluded and lifecycle-erased with the log, and — being neither
  ``semantic_event`` kind nor a working-set kind — are IGNORED by the live
  semantic-event reader (``LiveRuntimeContext._turns`` keeps only ``semantic_event``)
  and never reach a prompt or a working-set render. They are appended through
  :func:`_append_segment_raw`, which — exactly like the S2 fold's ``_append_raw`` —
  NEVER invokes ``_finish_write`` / the ``op_logger`` (routing a log write back
  through the op-logger re-forms the documented ``record -> op_logger -> arc.op ->
  record`` recursion, §2.9).
* **No silent fallback, best-effort during dual-write (§3.4).** ``final_message`` is
  NOT removed in this slice, so the old copy is still the authoritative fallback —
  design §3.4 makes best-effort acceptable UNTIL the old write is removed (S5), with
  the must-succeed promotion landing together with that removal. So a failed atom
  mint here is LOUD (a typed ``part_atom_mint_failed`` reason on the logs/trace) but
  non-fatal: it must never break the turn baseline (RULE 2) over an invisible
  provisioning write.

The atom-BUILDING + reproduce logic is pure and gact-side (it speaks the gact
``Message``/``Part`` shapes); the low-level log append reaches the ARC segment store's
raw primitives directly (the ``arc/`` owner modules ``memory.py`` / ``segments.py``
are at their CI file-size ratchet baselines with zero room to add a method), mirroring
the internals the S2 fold's ``FoldingSegmentStore._append_raw`` already uses.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.schema import Segment, SegmentKind
from clio_agent.gact.types import Message

# The atom kind (a new, additive ``SegmentKind`` member) and the reserved content
# lane for the family: a partition UNDER ``_events`` (so ``is_events_scope`` is True —
# search-excluded + lifecycle-erased with the log) distinct from the bare
# ``_events``/``_events/N`` semantic-event chunks (so it never perturbs the
# semantic-event chunk cursor) and from the S2 fold's ``_events/w`` content lane.
MESSAGE_PART_KIND: SegmentKind = "message_part"
MESSAGE_PART_SCOPE = f"{EVENTS_SCOPE}/m"

# Bumped only on a breaking change to the atom ``content`` shape; stored per-atom so a
# future reader can branch on it (design §2.3 ``schema_version``).
PART_ATOM_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Pure atom construction (§2.3) — one atom per part, message envelope denormalized
# --------------------------------------------------------------------------- #


def _compaction_identity(part_dump: dict[str, Any]) -> dict[str, str] | None:
    """Return ``{msg_compact_id, memory_event_id}`` for a compaction part, else ``None``.

    A compaction part (SPEC §4.5 / §6.25, produced by the ``/compact`` route) carries
    the synthetic-summary identity in its ``id`` (``msg_compact_*``) + ``metadata``
    (``memory_event_id``). Captured so S5 can reproduce the compaction wire identity
    (frozen surface 1.8). ``None`` for every non-compaction part (the finalize path
    never produces one; only ``/compact`` does — both flow through the same persist
    seam, so both are covered).
    """
    if part_dump.get("type") != "compaction":
        return None
    meta = part_dump.get("metadata") or {}
    return {
        "msg_compact_id": str(part_dump.get("id") or ""),
        "memory_event_id": str(meta.get("memory_event_id") or ""),
    }


def _atom_content(
    message: Message,
    envelope: dict[str, Any],
    part_dump: dict[str, Any] | None,
    part_index: int,
) -> dict[str, Any]:
    """Build one atom's ``content`` dict: the §2.3 wire-identity header + the full
    reproduction payload (the whole ``part`` dump + the message ``envelope``).

    The header fields (``message_id`` / ``part_id`` / ``created_at`` / ``role`` /
    ``kind`` / ``stream_source`` / ``usage`` / ``status`` [+ ``expert_handoff`` /
    ``compaction``]) are the queryable identity the design highlights; the ``part`` +
    ``message`` sub-dicts are the byte-exact reproduction source. Every field is copied
    from the ALREADY-assembled ``message`` — nothing is minted here (§2.8c).

    Args:
        message: The persisted gact message (its ids/timestamps are authoritative).
        envelope: ``message.model_dump(exclude={"parts"})`` — the message-level fields
            (denormalized onto every part atom so each atom is self-describing).
        part_dump: ``part.model_dump()`` for this atom's part, or ``None`` for the
            single envelope-only atom of a zero-part message.
        part_index: This part's 0-based position in ``message.parts`` (the persisted
            ``parts[]`` order; the reproduction sort key).

    Returns:
        The atom ``content`` dict (JSON-native; msgpack-safe after ``_coerce_content``).
    """
    # ``stream_source`` is the MESSAGE-level provenance the finalize seam stamps onto
    # ``assistant_metadata`` before the Message is built (turn_degradation.
    # assemble_stream_and_degradation_metadata); it is part of ``final_message``'s
    # ``metadata`` and thus reproducible. Denormalized here for queryability.
    stream_source = str((message.metadata or {}).get("stream_source", "") or "")
    content: dict[str, Any] = {
        "schema_version": PART_ATOM_SCHEMA_VERSION,
        "atom_role": "part" if part_dump is not None else "envelope",
        "message_id": message.id,
        "part_id": str(part_dump.get("id") or "") if part_dump is not None else "",
        "part_index": part_index,
        "created_at": message.created_at,
        "role": message.role,
        "kind": str(part_dump.get("type") or "") if part_dump is not None else "",
        "stream_source": stream_source,
        "usage": message.tokens.model_dump(),
        "status": str(part_dump.get("status") or "") if part_dump is not None else "",
        "part": part_dump,
        "message": envelope,
    }
    if part_dump is not None:
        handoff = (part_dump.get("metadata") or {}).get("expert_handoff")
        if handoff:
            # Verbatim — never server-authored (frozen surface 1.9, #880 baseline-0).
            content["expert_handoff"] = handoff
        compaction = _compaction_identity(part_dump)
        if compaction is not None:
            content["compaction"] = compaction
    return content


def build_message_part_atoms(message: Message) -> list[dict[str, Any]]:
    """Build the ``message_part`` atom ``content`` dicts for one message (pure).

    One atom per part, carrying the wire-identity header + the full part dump + the
    message envelope (denormalized). A zero-part message yields exactly ONE
    envelope-only atom (``part=None``, ``part_id=""``) so its message-level identity
    still lands on the log (e.g. the finalize error-settle path persists ``parts=[]``).

    Args:
        message: The persisted gact message.

    Returns:
        The list of atom ``content`` dicts, in ``parts[]`` order.
    """
    envelope = message.model_dump(exclude={"parts"})
    if not message.parts:
        return [_atom_content(message, envelope, None, 0)]
    atoms: list[dict[str, Any]] = []
    for i, part in enumerate(message.parts):
        atoms.append(_atom_content(message, envelope, part.model_dump(), i))
    return atoms


# --------------------------------------------------------------------------- #
# Reproduction (§4.2 step-4 gate) — Message.model_dump(exclude_none=True) from atoms
# --------------------------------------------------------------------------- #


def reproduce_message_wire(atoms: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct ``Message.model_dump(exclude_none=True)`` from a message's atoms.

    The step-4 reproducibility gate (design §4.2): the reconstruction must equal the
    persisted ``final_message`` (``assistant_msg.model_dump(exclude_none=True)``) FIELD
    FOR FIELD. The envelope is read from the (denormalized) ``message`` sub-dict; the
    parts are the atoms' ``part`` sub-dicts in ``part_index`` order. NO id or timestamp
    is minted — the stored values are used verbatim, so a reload reproduces the live
    identity (the sabotage-b guard: a re-mint here would diverge the id field).

    Args:
        atoms: The ``content`` dicts of ONE message's atoms (any order).

    Returns:
        The reconstructed ``model_dump(exclude_none=True)`` dict.

    Raises:
        ValueError: When ``atoms`` is empty (no message to reproduce).
    """
    if not atoms:
        raise ValueError("reproduce_message_wire: no atoms for the message")
    ordered = sorted(atoms, key=lambda a: a.get("part_index", 0))
    envelope = dict(ordered[0]["message"])
    part_dicts = [a["part"] for a in ordered if a.get("part") is not None]
    message = Message(**envelope, parts=part_dicts)
    return message.model_dump(exclude_none=True)


# --------------------------------------------------------------------------- #
# The raw log append (§2.9) — never _finish_write / the op_logger
# --------------------------------------------------------------------------- #


def _append_segment_raw(
    store: Any, session_id: str, scope: str, kind: SegmentKind, content: dict[str, Any]
) -> Segment:
    """Append one segment to ``scope`` WITHOUT invoking the op-logger (§2.9 raw lane).

    Persists the atom and keeps the store's per-scope segment list + parallel locator
    in sync under the scope lock, but — unlike ``SegmentStore.append`` — does NOT call
    ``_finish_write`` (which would fire the ``op_logger`` and re-form the ``arc.op``
    recursion). Mirrors ``FoldingSegmentStore._append_raw`` exactly; reaches the store's
    raw primitives directly because ``arc/segments.py`` is at its file-size ratchet
    baseline with no room to expose a public method.

    Args:
        store: The ARC ``SegmentStore`` (``arc_memory._segments``).
        session_id: Owning session.
        scope: The physical content-lane scope (:data:`MESSAGE_PART_SCOPE`).
        kind: The atom kind (:data:`MESSAGE_PART_KIND`).
        content: The atom payload (coerced to a msgpack-safe form on the way in).

    Returns:
        The appended segment (its store-assigned ``logical_time`` + ``order``).
    """
    from clio_agent.arc.segments import _coerce_content  # noqa: PLC0415 - avoid import cycle

    with store._lock_for(session_id, scope):
        segs = store._segs(session_id, scope)
        order = max((s.order for s in segs), default=0.0) + 1.0
        seg = Segment(
            scope=scope,
            kind=kind,
            content=_coerce_content(content),
            session_id=session_id,
            step=-1,
            order=order,
            logical_time=store._new_lt(),
        )
        segs.append(seg)
        store._index.add(session_id, scope, seg)
        store._persist(session_id, scope, just_written=[seg])
        return seg


def mint_message_part_atoms(arc: Any, session_id: str, message: Message) -> list[Segment]:
    """Mint + durably append one message's ``message_part`` atoms to the canonical log.

    Builds the atoms (:func:`build_message_part_atoms`) and appends each to the
    ``_events/m`` lane via the raw append (§2.9). The atoms are written ALONGSIDE the
    existing ``final_message`` / messages-store copy (dual-write); no reader consumes
    them until S5.

    Args:
        arc: The process ARC memory (``ARCMemory``); its ``_segments`` store is used.
        session_id: Owning session.
        message: The persisted gact message to provision atoms for.

    Returns:
        The appended segments (one per atom).
    """
    store = arc._segments
    return [
        _append_segment_raw(store, session_id, MESSAGE_PART_SCOPE, MESSAGE_PART_KIND, content)
        for content in build_message_part_atoms(message)
    ]


def load_message_part_atoms(arc: Any, session_id: str) -> dict[str, list[dict[str, Any]]]:
    """Read a session's persisted ``message_part`` atoms, grouped by ``message_id``.

    Loads the ``_events/m`` lane (re-reading from the store when the hot copy was
    evicted — the eviction+rehydration path the identity pin exercises), returning
    ``{message_id: [atom-content, ...]}`` with each group sorted into ``parts[]`` order.
    Ready to feed straight to :func:`reproduce_message_wire`.

    Args:
        arc: The process ARC memory.
        session_id: Owning session.

    Returns:
        The per-message atom-content groups (empty when the session has none).
    """
    store = arc._segments
    groups: dict[str, list[dict[str, Any]]] = {}
    for seg in store.list_segments(session_id, MESSAGE_PART_SCOPE, include_tombstoned=True):
        content = seg.content
        groups.setdefault(str(content.get("message_id") or ""), []).append(content)
    for atoms in groups.values():
        atoms.sort(key=lambda a: a.get("part_index", 0))
    return groups

# NOTE (#737 S5): the persist-seam hook that dual-wrote atoms in S4
# (``record_message_parts_for_message``) is superseded by
# :func:`clio_agent.gact.transcript_projection.on_message_appended`, which pins the
# session regime and applies the regime-aware must-succeed / best-effort mint policy.
# ``mint_message_part_atoms`` above remains the low-level mint primitive it calls.
