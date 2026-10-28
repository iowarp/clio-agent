"""Core owns transcript content; resident rows and optional JSON are projections.

Normal materialization never imports, repairs or writes history. Legacy file
migration is an explicit startup operation. Intentional undo/clear/import still
use the existing atomic lane-generation writer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from clio_agent.arc.lane_chunking import lane_has_segments, lane_segments
from clio_agent.arc.lane_generations import current_base, replace_lane
from clio_agent.arc.schema import Segment
from clio_agent.errors import ClioError
from clio_agent.gact.messages import retained_message_position
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_SCOPE,
    append_part_atoms,
    build_message_part_atoms,
    fully_retracted,
    group_atoms_in_order,
    mint_message_part_atoms,
    reproduce_message_wire,
    retracted_part_ids,
)
from clio_agent.gact.transcript_file import (
    TranscriptEraseError,
    materialize_from_atoms,
    reload_resident_on_failure,
)
from clio_agent.gact.types import Message
from clio_agent.gact.workflow_state.state_merge import (
    drop_state_merge_lane,
    materialize_state_merge_projection,
    record_state_merge_best_effort,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Historical session-metadata key: pre-v0.8.0 servers pinned a per-session
# regime here. Single-regime now — the key is neither written nor read (stale
# stamps on old sessions are inert wire metadata).
REGIME_METADATA_KEY = "transcript_regime"

# The typed no-silent-fallback reasons (the ``stream_fallback`` catalog style, §3.4).
INGEST_FAILED_REASON = "transcript_ingest_failed"
BACKFILL_FAILED_REASON = "transcript_backfill_failed"


class TranscriptIngestError(RuntimeError):
    """An atoms-regime message could not be minted onto the canonical log.

    Raised (never swallowed) when the atom mint fails and the atoms are the ONE
    transcript copy (§3.4). Carries the typed :data:`INGEST_FAILED_REASON` so the
    failure is queryable on the trace/API rather than a silent transcript gap.
    """

    def __init__(self, session_id: str, message_id: str, cause: BaseException) -> None:
        self.reason = INGEST_FAILED_REASON
        self.session_id = session_id
        self.message_id = message_id
        super().__init__(
            f"{INGEST_FAILED_REASON}: session={session_id} message={message_id}: {cause}"
        )


class TranscriptBackfillError(RuntimeError):
    """A pre-atom ledger message could not be minted during migration backfill.

    Raised per-message (no silent skip — design (d)) when :func:`mint_atoms_from_ledger`
    cannot provision one message's atoms, so an un-migratable ledger surfaces the exact
    failing message id rather than serving a silently-truncated transcript.
    """

    def __init__(self, session_id: str, message_id: str, cause: BaseException) -> None:
        self.reason = BACKFILL_FAILED_REASON
        self.session_id = session_id
        self.message_id = message_id
        super().__init__(
            f"{BACKFILL_FAILED_REASON}: session={session_id} message={message_id}: {cause}"
        )


# --------------------------------------------------------------------------- #
# Flag + regime pinning (session-scoped, pinned at the first message)
# --------------------------------------------------------------------------- #


def _sessions(app: "FastAPI") -> Any:
    """The session store, or ``None`` when the app carries none (minimal test wiring)."""

    return getattr(getattr(app, "state", None), "sessions", None)


def _arc(app: "FastAPI") -> Any:
    """Return the process Core substrate only when it exposes canonical segments."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    return arc if getattr(arc, "_segments", None) is not None else None


def canonical_log_arc(app: "FastAPI") -> Any:
    """The capability gate (:func:`_arc`) as a public seam for cross-module callers.

    ``part_atom_minter.open_turn_minter`` binds the turn's ARC through this, so the
    minter and the persist seam agree on exactly ONE definition of "this app can hold
    the canonical log". Taking ``app.state.arc`` at face value instead adopted a
    degraded / metrics-only ARC that has no ``_segments``, and every mint then died on
    ``AttributeError`` inside finalize AND inside the failed-finalize envelope meant to
    recover it.
    """

    return _arc(app)


def atoms_active(app: "FastAPI") -> bool:
    """Whether the atom projection is in play for this app: a canonical-log
    substrate exists. Single regime (v0.8.0): every session on an app with ARC
    reads/writes through the atoms; the per-session pin + process flag are gone.
    """

    return _arc(app) is not None


# --------------------------------------------------------------------------- #
# Assembly — the persistence projection (design (a)): assemble(atoms).to_wire()
# --------------------------------------------------------------------------- #


def assemble_session_messages(arc: Any, session_id: str) -> list[Message]:
    """Assemble a session's transcript BY REFERENCE from its ``message_part`` atoms.

    The persistence projection: the LIVE (non-tombstoned) atoms on the ``_events/m``
    lane are read in append order and split into per-message groups at each MESSAGE
    BOUNDARY — the first atom of a message is always ``part_index == 0`` (the mint
    enumerates parts from 0, and a zero-part message is a single ``part_index == 0``
    envelope atom). Each group is reproduced to a ``Message`` via the S4-proven
    :func:`~clio_agent.gact.part_atoms.reproduce_message_wire` (ids/timestamps used
    verbatim — never re-minted, so ``reload == live`` identity holds).

    Boundary grouping — NOT grouping by ``message_id`` — is load-bearing: real ledgers
    carry DUPLICATE message ids (two distinct assistant messages can share one
    ``msg_asst_*`` id — observed in the corpus, ``sess_b2d2c710f0f4``). Grouping by id
    would collapse them into one message and drop the count; the append-order boundary
    keeps every message distinct because each starts its own ``part_index == 0`` atom at
    a strictly-increasing ``order``. A session with no atoms yields ``[]``.

    Args:
        arc: The process ARC memory (its ``_segments`` store holds the canonical log).
        session_id: The session whose transcript to assemble.

    Returns:
        The chronological ``list[Message]`` — byte-equal to the live/persisted ledger
        under the §4.1.A normalizer.
    """

    store = arc._segments
    # #1337: ONE grouping rule for both atom profiles (keyed by message id, closed by the
    # envelope atom, the v1 ordinal boundary as the fallback) — see part_atoms.
    # #1339: the lane is a CHUNK FAMILY; ``lane_segments`` concatenates every present
    # chunk in append order, so chunking is invisible to this projection.
    # The lane's CURRENT generation (``arc.lane_generations``): a whole-lane replace
    # writes a new generation and switches to it in one put, so a read sees one whole
    # generation, never a half-replaced lane.
    base = current_base(store, session_id, MESSAGE_PART_SCOPE)
    lane = [seg.content for seg in lane_segments(store, session_id, base, include_tombstoned=False)]
    retracted = retracted_part_ids(lane)  # a compaction record whose fold failed
    messages = [
        Message(**reproduce_message_wire(atoms, retracted))
        for atoms in group_atoms_in_order(lane)
        if not fully_retracted(atoms, retracted)
    ]
    # #737 S6: workflow_state on the delegate rows is the recorded RESULT of the last
    # state_merge op for the scope — materialized schema-free here, NEVER re-folded on
    # read (design §2.8.d). A no-op when no op was recorded (rows keep their verbatim,
    # equally-frozen value).
    materialize_state_merge_projection(arc, session_id, messages)
    return messages


def ensure_accepted_message(app: "FastAPI", session_id: str, message: Message) -> None:
    """Splice only an accepted human message into the current raw lane, preserving live atoms.

    The existing lane lock and generation writer confirm the canonical change.
    Unrelated partial messages, envelopes, retractions and their relative order
    remain verbatim. Cache presence never substitutes for durable confirmation.
    """
    if message.session_id != session_id or message.role != "user":
        raise ValueError("Accepted message belongs to another session or role")
    arc = _arc(app)
    if arc is None:
        raise RuntimeError("Accepted input projection requires Core")
    from clio_agent.gact.restart_interruptions import settle_restart_interruption

    settle_restart_interruption(app, session_id)  # never splice after a dangling turn
    if arc is not None:
        store = arc._segments
        with store._lock_for(session_id, MESSAGE_PART_SCOPE):
            base = current_base(store, session_id, MESSAGE_PART_SCOPE)
            retained = list(lane_segments(store, session_id, base, include_tombstoned=False))
            atoms = [segment.content for segment in retained]
            groups = group_atoms_in_order(atoms)
            retracted = retracted_part_ids(atoms)
            if any(part.id and part.id in retracted for part in message.parts):
                raise ValueError("Accepted message contains retracted parts")
            matches = [group for group in groups if group[0].get("message_id") == message.id]
            if len(matches) > 1:
                raise ValueError("Accepted message identity is ambiguous in the transcript")
            current = matches[0] if matches else []
            complete = (
                bool(current)
                and Message.model_validate(reproduce_message_wire(current, retracted)) == message
            )
            if not complete:
                envelope = message.model_dump(exclude={"parts"})
                for atom in current:
                    if atom.get("atom_role") == "retract":
                        raise ValueError("Accepted message was retracted")
                    header = atom.get("message")
                    stub = atom.get("message_stub")
                    if isinstance(header, dict) and header != envelope:
                        raise ValueError("Accepted message envelope conflicts with retained input")
                    if isinstance(stub, dict) and any(
                        key in stub and stub[key] != getattr(message, key)
                        for key in ("id", "session_id", "turn_id", "created_at", "role")
                    ):
                        raise ValueError("Accepted message stub conflicts with retained input")
                    part = atom.get("part")
                    index = atom.get("part_index")
                    if part is not None and (
                        not isinstance(index, int)
                        or index < 0
                        or index >= len(message.parts)
                        or part != message.parts[index].model_dump()
                    ):
                        raise ValueError("Accepted message part conflicts with retained input")
                if current:
                    position = next(index for index, atom in enumerate(atoms) if atom is current[0])
                else:
                    group_position = retained_message_position(
                        (str(group[0].get("created_at") or "") for group in groups),
                        message.created_at,
                    )
                    position = (
                        next(
                            index
                            for index, atom in enumerate(atoms)
                            if atom is groups[group_position][0]
                        )
                        if group_position < len(groups)
                        else len(atoms)
                    )
                if not current and position == len(atoms):
                    # Ordinary chronological input uses the existing confirmed
                    # append writer. A partial/earlier input still needs the
                    # atomic splice below; neither path owns another history.
                    from clio_agent.gact.transcript_recording import record_message

                    record_message(arc, session_id, message)
                else:
                    removed = {id(atom) for atom in current}
                    replacement = build_message_part_atoms(message)
                    merged: list[Segment | dict[str, Any]] = [
                        *retained[:position],
                        *replacement,
                        *(
                            segment
                            for segment in retained[position:]
                            if id(segment.content) not in removed
                        ),
                    ]

                    def write(lane: str) -> None:
                        append_part_atoms(store, session_id, merged, lane=lane)

                    replace_lane(store, session_id, MESSAGE_PART_SCOPE, write)
    from clio_agent.gact.transcript_recording import project_message

    project_message(app, session_id, message, newly_recorded=not complete, ordered=True)


def has_atoms(arc: Any, session_id: str) -> bool:
    """Whether the session has ANY live ``message_part`` atom on the canonical log.

    #1339: family-aware by construction (:func:`~clio_agent.arc.lane_chunking.lane_has_segments`) —
    chunk 1 is always filled first, so ONE read of it answers the question for the
    whole lane.
    """

    store = arc._segments
    return lane_has_segments(store, session_id, current_base(store, session_id, MESSAGE_PART_SCOPE))


# --------------------------------------------------------------------------- #
# Backfill — the documented migration seam for pre-atom ledgers (design (d))
# --------------------------------------------------------------------------- #


def mint_atoms_from_ledger(arc: Any, session_id: str, messages: list[Message]) -> None:
    """Mint explicit supplied data for import/equivalence tooling, never from a read.

    Runtime migration retains ambiguous legacy content separately. Only a caller
    that deliberately owns an import may use this raw, per-message algorithm.
    """

    for message in messages:
        try:
            mint_message_part_atoms(arc, session_id, message)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed, per-message reason
            raise TranscriptBackfillError(session_id, getattr(message, "id", ""), exc) from exc
        # #737 S6: re-provision the state_merge op from the ledger too, so the recorded
        # result survives a divergent-lane repair or a pre-S6 ledger backfill.
        record_state_merge_best_effort(arc, session_id, message)


# --------------------------------------------------------------------------- #
# The resident-set materializer (read-path switch) — design (c)
# --------------------------------------------------------------------------- #


def materialize_ledger(app: "FastAPI", session_id: str) -> Optional[list[Message]]:
    """Read canonical history without consulting or repairing a file cache."""
    return materialize_from_atoms(app, session_id)


# --------------------------------------------------------------------------- #
# Write-seam hooks — keep the atom lane the source of truth (design (b))
# --------------------------------------------------------------------------- #


def replace_atom_lane(arc: Any, session_id: str, messages: list[Message]) -> None:
    """Replace the session's atom lane with EXACTLY ``messages``, atomically.

    The ONE whole-lane replace of the transcript
    (:func:`~clio_agent.arc.lane_generations.replace_lane`): the atoms are minted into
    a new lane generation, the lane's pointer is switched to it in one put, then the
    old generation is dropped. A failure before the switch leaves the previous
    transcript intact. The ``state_merge`` op lane is re-materialised after the switch
    (#737 S6, best-effort-but-loud as everywhere).

    Raises:
        LaneReplaceError: A stage of the replace failed (its ``committed`` names
            which transcript readers now see).
    """

    def _mint_into(lane: str) -> None:
        for message in messages:
            try:
                mint_message_part_atoms(arc, session_id, message, lane=lane)
            except Exception as exc:  # noqa: BLE001 - re-raised typed, per message
                raise TranscriptIngestError(session_id, getattr(message, "id", ""), exc) from exc

    replace_lane(
        arc._segments,
        session_id,
        MESSAGE_PART_SCOPE,
        _mint_into,
        metadata={"transcript_visibility": uuid4().hex},
    )
    drop_state_merge_lane(arc, session_id)
    for message in messages:
        record_state_merge_best_effort(arc, session_id, message)


def on_ledger_replaced(app: "FastAPI", session_id: str, messages: list[Message]) -> None:
    """Replace-seam hook: re-materialize the atom lane to EXACTLY the new ledger.

    Backs ``session_store._replace_session_messages`` -- undo/rewind/fork/compact/import --
    through :func:`replace_atom_lane`, so the assembled projection matches the replaced
    list or, on a failure, still the previous one. This is the ``transcript_delete`` +
    re-append of design §2.5, scoped to the gact-visible transcript projection ONLY: it
    touches the ``_events/m`` lane and NEVER the ARC working-set scopes, so
    ``memory_scope:"gact_visible_transcript_only"`` holds by construction (the
    sabotage-c guard). With ``transcript.file`` off a failure also drops the session's
    in-memory ledger (already holding the new rows), so every later read serves the
    transcript clio-core kept. A no-op without a canonical log.
    """

    arc = _arc(app)
    if arc is None:
        return
    with reload_resident_on_failure(app, session_id):
        replace_atom_lane(arc, session_id, messages)


def on_ledger_deleted(app: "FastAPI", session_id: str) -> None:
    """Delete-seam hook: erase the session's atom lane (transcript projection erasure).

    Backs ``session_store._delete_session_messages`` (``DELETE /messages`` /
    ``session.cleared`` / the ``DELETE /sessions`` cascade). Erases ONLY the
    ``_events/m`` transcript lane, as a replace with nothing
    (:func:`~clio_agent.arc.lane_generations.erase_lane`: never a half-erased
    transcript); the ARC working-set scopes (ARC memory) are untouched -- the frozen
    ``gact_visible_transcript_only`` semantics (1.11, sabotage-c).

    Runs BEFORE the caller removes anything else. Every step is idempotent, so a
    failure is raised typed and a retry finishes the erase; with ``transcript.file``
    off a failure also drops the in-memory ledger so reads serve what clio-core kept.

    Raises:
        TranscriptEraseError: clio-core did not finish the erase.
    """

    arc = _arc(app)
    if arc is None:
        return
    try:
        with reload_resident_on_failure(app, session_id):
            replace_lane(
                arc._segments,
                session_id,
                MESSAGE_PART_SCOPE,
                lambda _lane: None,
                metadata={"transcript_visibility": uuid4().hex},
            )
            drop_state_merge_lane(arc, session_id)  # #737 S6: the op lane goes with it
    except (ClioError, OSError, RuntimeError) as exc:
        raise TranscriptEraseError(session_id, exc) from exc


# --------------------------------------------------------------------------- #
# The final_message embed gate (design (b) / (e)) — the byte-copy dies under atoms
# --------------------------------------------------------------------------- #


def final_message_embed(app: "FastAPI", session_id: str, assistant_msg: Message) -> dict[str, Any]:
    """Return the ``{"final_message": ...}`` durable-trace embed, or ``{}`` under atoms.

    The ``final_message`` byte-copy embedded in the durable ``turn.completed`` /
    ``turn.failed`` event exists ONLY so the messages store is derivable from the trace
    (a pre-S5 need). Under the **atoms** regime the ``message_part`` atoms already carry
    the full wire identity, minted at the same persist moment, so the byte-copy is
    redundant and is DROPPED (design §4.2 step 5 — "kill ``final_message``"). Under the
    **legacy** regime it is still emitted verbatim (the dual-write/rollback window, §4.3).

    Returned as a spreadable fragment so ``turn_finalize`` composes it with ``**`` and
    stays at its file-size ratchet (no net lines added).
    """

    if atoms_active(app):
        return {}
    # No canonical-log substrate: the embed remains the only trace-derivable copy.
    return {"final_message": assistant_msg.model_dump(exclude_none=True)}


# Kept importable for tests/tools that build atoms without a full app (pure helper).
__all__ = [
    "REGIME_METADATA_KEY",
    "atoms_active",
    "TranscriptBackfillError",
    "TranscriptIngestError",
    "assemble_session_messages",
    "build_message_part_atoms",
    "final_message_embed",
    "has_atoms",
    "materialize_ledger",
    "mint_atoms_from_ledger",
    "on_ledger_deleted",
    "on_ledger_replaced",
    "replace_atom_lane",
]
