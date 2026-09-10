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

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.schema import Segment, SegmentKind
from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

# The atom kind (a new, additive ``SegmentKind`` member) and the reserved content
# lane for the family: a partition UNDER ``_events`` (so ``is_events_scope`` is True —
# search-excluded + lifecycle-erased with the log) distinct from the bare
# ``_events``/``_events/N`` semantic-event chunks (so it never perturbs the
# semantic-event chunk cursor) and from the S2 fold's ``_events/w`` content lane.
MESSAGE_PART_KIND: SegmentKind = "message_part"
MESSAGE_PART_SCOPE = f"{EVENTS_SCOPE}/m"

# Bumped only on a breaking change to the atom ``content`` shape; stored per-atom so a
# future reader can branch on it (design §2.3 ``schema_version``).
#
# v2 (#1337, streaming-native persistence) adds ``envelope_authority`` with two profiles:
#   * ``"inline"`` — the v1 byte-shape: every atom denormalizes the full message envelope
#     (batch paths: user messages, a2ui parts, backfill, replace/extend; the write count of
#     those paths is unchanged and no envelope atom is emitted);
#   * ``"atom"``   — the eager turn path: lean part atoms sealed WHEN THE PART BECAME FINAL
#     (``message_stub`` + ``sealed_at`` / ``seal_source``, no envelope) plus ONE trailing
#     ``atom_role == "envelope"`` atom written at finalize carrying the authoritative
#     message fields (tokens, cost, stop_reason, error_info, metadata) — the ONLY place
#     they exist for that message. A message whose envelope never landed (a crash before
#     finalize) reassembles as a TYPED incomplete message, never a silently complete one.
# ``atom_role == "retract"`` (``retracted_part_ids``) is honored by the reproducer as the
# escape hatch for a sealed part that must not be served; no live path emits it (the
# ledger only ever removes UNSEALED parts — pinned by tests).
PART_ATOM_SCHEMA_VERSION = 2
ENVELOPE_AUTHORITY_INLINE = "inline"
ENVELOPE_AUTHORITY_ATOM = "atom"
TRANSCRIPT_INCOMPLETE_REASON = "transcript_incomplete_no_envelope"


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
    # ``assistant_metadata`` before the Message is built (turn_stream.
    # assemble_stream_metadata); it is part of ``final_message``'s
    # ``metadata`` and thus reproducible. Denormalized here for queryability.
    stream_source = str((message.metadata or {}).get("stream_source", "") or "")
    content: dict[str, Any] = {
        "schema_version": PART_ATOM_SCHEMA_VERSION,
        "envelope_authority": ENVELOPE_AUTHORITY_INLINE,
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
# The eager ("atom" authority) profile — sealed part atoms + one envelope atom (#1337)
# --------------------------------------------------------------------------- #


def message_stub(*, message_id: str, turn_id: str, session_id: str, created_at: str) -> dict:
    """The identity a sealed part atom carries before the message envelope exists."""

    return {
        "id": message_id,
        "turn_id": turn_id,
        "session_id": session_id,
        "role": "assistant",
        "created_at": created_at,
    }


def build_sealed_part_atom(
    stub: Mapping[str, Any],
    part_dump: dict[str, Any],
    part_index: int,
    *,
    sealed_at: str,
    seal_source: str,
) -> dict[str, Any]:
    """One lean part atom for a part that just became final (no envelope, no usage).

    Args:
        stub: :func:`message_stub` of the in-flight assistant message.
        part_dump: ``part.model_dump()`` at seal time (``sequence`` already stamped).
        part_index: The part's 0-based position in the ledger (stable once sealed).
        sealed_at: ISO timestamp of the seal.
        seal_source: ``"live"`` (sealed by the ledger mid-turn) or ``"finalize"``.
    """

    content: dict[str, Any] = {
        "schema_version": PART_ATOM_SCHEMA_VERSION,
        "envelope_authority": ENVELOPE_AUTHORITY_ATOM,
        "atom_role": "part",
        "message_id": str(stub.get("id") or ""),
        "part_id": str(part_dump.get("id") or ""),
        "part_index": part_index,
        "created_at": str(stub.get("created_at") or ""),
        "role": str(stub.get("role") or "assistant"),
        "kind": str(part_dump.get("type") or ""),
        "stream_source": str((part_dump.get("metadata") or {}).get("stream_source") or ""),
        "status": str(part_dump.get("status") or ""),
        "part": part_dump,
        "message_stub": dict(stub),
        "sealed_at": sealed_at,
        "seal_source": seal_source,
    }
    handoff = (part_dump.get("metadata") or {}).get("expert_handoff")
    if handoff:
        content["expert_handoff"] = handoff
    compaction = _compaction_identity(part_dump)
    if compaction is not None:
        content["compaction"] = compaction
    return content


def build_envelope_atom(message: Message) -> dict[str, Any]:
    """The trailing envelope atom: the authority for every message-level field."""

    envelope = message.model_dump(exclude={"parts"})
    return {
        "schema_version": PART_ATOM_SCHEMA_VERSION,
        "envelope_authority": ENVELOPE_AUTHORITY_ATOM,
        "atom_role": "envelope",
        "message_id": message.id,
        "part_id": "",
        "part_index": len(message.parts),
        "created_at": message.created_at,
        "role": message.role,
        "kind": "",
        "stream_source": str((message.metadata or {}).get("stream_source", "") or ""),
        "usage": message.tokens.model_dump(),
        "status": "",
        "part": None,
        "message": envelope,
        "part_ids": [str(part.id or "") for part in message.parts],
        "part_count": len(message.parts),
        "complete": True,
    }


def _atom_turn_id(content: Mapping[str, Any]) -> str:
    """The TURN an atom belongs to: its message stub's (part) or envelope's ``turn_id``."""

    for key in ("message_stub", "message"):
        holder = content.get(key)
        if isinstance(holder, Mapping):
            turn_id = str(holder.get("turn_id") or "")
            if turn_id:
                return turn_id
    return ""


def group_atoms_in_order(atoms: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a lane (append order) into per-message atom groups.

    ONE rule for both profiles: a group is keyed by ``message_id`` and stays open until
    its envelope atom lands (``"atom"`` authority); an atom whose message has no open
    group opens one. The v1 / ``"inline"`` fallback keeps today's boundary: an atom whose
    ``part_index`` does not advance the group AND whose ``part_id`` the group has not
    seen opens a NEW group — two adjacent messages sharing a ``msg_asst_*`` id (observed
    in the corpus, ``sess_b2d2c710f0f4``) stay distinct. Keying by id (not append
    contiguity) is what keeps an atom of ANOTHER message interleaved mid-turn (an a2ui
    part persisted while a turn streams) out of the in-flight message's group.

    The ``"atom"`` profile needs its own boundary for the SAME duplicate-id case, because
    an envelope-less group (a turn that died between its last seal and finalize) is never
    closed and would otherwise swallow the next message that reuses its id — merging two
    messages into one AND hiding the typed ``incomplete`` reassembly behind the later
    message's envelope. The ordinal rule cannot serve here: ``mint_remainder`` legitimately
    writes a never-sealed part at a LOWER index than an already-sealed one. The
    discriminator is the TURN: one turn mints one assistant message, so an atom stamped
    with a different ``turn_id`` than the open group's is a different message.
    """

    groups: list[list[dict[str, Any]]] = []
    open_by_id: dict[str, list[dict[str, Any]]] = {}
    for content in atoms:
        mid = str(content.get("message_id") or "")
        role = str(content.get("atom_role") or "part")
        authority = str(content.get("envelope_authority") or ENVELOPE_AUTHORITY_INLINE)
        index = int(content.get("part_index", 0) or 0)
        pid = str(content.get("part_id") or "")
        group = open_by_id.get(mid)
        if group is not None and authority == ENVELOPE_AUTHORITY_INLINE:
            seen_ids = {str(a.get("part_id") or "") for a in group}
            max_index = max(int(a.get("part_index", 0) or 0) for a in group)
            # An inline envelope-only atom IS a whole (zero-part) message; a part whose
            # index does not advance the group under an unseen id is the next message.
            if role == "envelope" or (index <= max_index and pid not in seen_ids):
                group = None  # the inline boundary: a new message under the same id
        elif group is not None:
            open_turn = _atom_turn_id(group[0])
            this_turn = _atom_turn_id(content)
            if open_turn and this_turn and open_turn != this_turn:
                group = None  # a different TURN reusing this message id: a new message
        if group is None:
            group = [content]
            groups.append(group)
            open_by_id[mid] = group
        else:
            group.append(content)
        if role == "envelope" and authority == ENVELOPE_AUTHORITY_ATOM:
            open_by_id.pop(mid, None)  # closed: the next atom of this id is a new message
    return groups


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
    retracted: set[str] = set()
    envelope_atom: dict[str, Any] | None = None
    latest_by_part: dict[str, dict[str, Any]] = {}
    for atom in atoms:  # lane order: the LAST atom of a part id wins (a reseal)
        role = str(atom.get("atom_role") or "part")
        if role == "retract":
            retracted.update(str(x) for x in (atom.get("retracted_part_ids") or []))
        elif role == "envelope" and atom.get("envelope_authority") == ENVELOPE_AUTHORITY_ATOM:
            envelope_atom = atom
        elif atom.get("part") is not None:
            latest_by_part[str(atom.get("part_id") or "") or f"@{id(atom)}"] = atom
    ordered = sorted(latest_by_part.values(), key=lambda a: a.get("part_index", 0))
    part_dicts = [a["part"] for a in ordered if str(a.get("part_id") or "") not in retracted]
    if envelope_atom is not None:
        envelope = dict(envelope_atom["message"])
    else:
        inline = sorted(
            (a for a in atoms if a.get("message") is not None),
            key=lambda a: a.get("part_index", 0),
        )
        if inline:
            envelope = dict(inline[0]["message"])  # v1 / inline: every atom carries it
        else:
            envelope = _incomplete_envelope(atoms)
    message = Message(**envelope, parts=part_dicts)
    return message.model_dump(exclude_none=True)


def _incomplete_envelope(atoms: list[dict[str, Any]]) -> dict[str, Any]:
    """The TYPED envelope for sealed part atoms whose envelope never landed.

    A turn that died between its last seal and finalize left durable parts but no
    authority for tokens / cost / stop_reason. Reload serves those parts under
    ``stop_reason="incomplete"`` with ``metadata.transcript_incomplete`` naming the
    reason, and the stream audit records it — never a silently complete message.
    """

    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stubs: list[dict[str, Any]] = [
        a["message_stub"] for a in atoms if isinstance(a.get("message_stub"), dict)
    ]
    stub: dict[str, Any] = dict(stubs[0]) if stubs else {}
    sealed = sorted(str(a.get("sealed_at") or "") for a in atoms if a.get("sealed_at"))
    message_id = str(stub.get("id") or atoms[0].get("message_id") or "")
    logger.warning(
        "transcript reassembled WITHOUT its envelope: message=%s sealed_parts=%d reason=%s",
        message_id,
        len(atoms),
        TRANSCRIPT_INCOMPLETE_REASON,
    )
    stream_audit(
        "transcript.incomplete_no_envelope",
        message_id=message_id,
        session_id=str(stub.get("session_id") or ""),
        sealed_parts=len(atoms),
        reason=TRANSCRIPT_INCOMPLETE_REASON,
    )
    created = str(stub.get("created_at") or (sealed[0] if sealed else ""))
    return {
        "id": message_id,
        "turn_id": str(stub.get("turn_id") or ""),
        "session_id": str(stub.get("session_id") or ""),
        "role": str(stub.get("role") or "assistant"),
        "created_at": created,
        "updated_at": sealed[-1] if sealed else created,
        "stop_reason": "incomplete",
        "metadata": {
            "transcript_incomplete": {
                "reason": TRANSCRIPT_INCOMPLETE_REASON,
                "sealed_parts": len(atoms),
                "message_id": message_id,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }


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


def append_part_atom(store: Any, session_id: str, content: dict[str, Any]) -> Segment:
    """Append ONE atom ``content`` to the session's ``_events/m`` lane (the raw append).

    The public form of :func:`_append_segment_raw` for the eager path (the per-turn
    minter seals one part at a time) and for ``live_edge``; same lane, same kind.
    """

    return _append_segment_raw(store, session_id, MESSAGE_PART_SCOPE, MESSAGE_PART_KIND, content)


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
