"""The mutable live-edge streaming atom — read-model coalescing of stream deltas (#737 S7).

The LAST highway slice (design ``docs/design/unified-arc-highway.md`` §2.3 provisions
the atom, §4.2 step 7 implements it after equivalence landed). S4/S5 made the
persisted transcript a projection of the ``message_part`` atom family, minted ONCE per
part at persist and read back byte-equal (``reload == live``). What S4/S5 do NOT model
is the *in-flight* edge: while a text/thinking part is still streaming, its coalesced
text lives only in the ``TurnTranscript`` buffer + the live SSE ``message.part.delta``
transport, so a canonical-log read mid-stream (the atoms-regime projection, or a reload
carrying "one live in-flight assistant message", surface 1.3) sees an EMPTY part until
close. This slice adds the identity-stable atom that **grows in place during streaming**
so read-models coalesce the deltas into it — enabling streaming-thinking cadence off the
log — while the log itself gains exactly ONE sealed atom per part (S5's mint, unchanged).

THE DESIGN QUESTION (the plan's open question, §4.2 step 7): can an in-place-mutable atom
coexist with append-only-within-a-session (§2.6) WITHOUT a per-token tombstone storm? The
naive shape — a durable atom the stream ``replace``\\s on every token — tombstones the
prior atom per token: genuine write-amplification (§4.2 step 7 risk note), inverting the
O(chunk) append budget (§2.10) and deepening the duplication RULE 4 forbids.

CHOSEN SHAPE — **(i) a bounded mutable head slot per streaming part that is NOT a log
record until sealed.** The log records only the *seal* (S5's single ``message_part`` atom
per part, minted at persist); the in-flight buffer lives in the projection/read-model
layer — the honest reading of "read-models coalesce deltas" (§2.8 catch 3). Consequences,
each a cited constraint:

* **Coexistence with append-only (§2.6) holds by construction.** The canonical log only
  ever receives appends (the seal atom, exactly one per part). The growing edge is a
  :class:`LiveEdgeSlot` in the read-model — a bounded, evictable, re-derivable projection
  cache (RULE 4: a projection cache, not a fifth store — its content is exactly the sum
  of the live deltas, which are re-streamable; on eviction it re-derives to empty and the
  next delta re-opens it). No ``replace``, no tombstone, is ever emitted while streaming.
* **Write-amplification is O(1) atoms per part**, independent of token count — the budget
  the §4.2 step-7 risk note demands. The mechanism's *own* durable footprint is bounded
  by :data:`LIVE_EDGE_MAX_ATOMS_PER_PART` (default policy writes ZERO extra atoms; the
  seal is S5's, always one). :func:`durable_atoms_for_part` reports it so the budget is a
  standing test assertion, not an aspiration.
* **The seal byte-matches S4/S5.** The slot does NOT mint the sealed ``_events/m`` atom —
  S5's ``on_message_appended`` still does, from the transcript's closed part. The slot's
  only seal obligation is that its coalesced text EQUALS that closed-part text (both are
  ``"".join(chunks)`` over the SAME deltas); :meth:`LiveEdgeSlot.seal` verifies it and
  raises a typed :class:`LiveEdgeSealMismatchError` on divergence (no silent fallback,
  §3.4). So the S5 ``reload == live`` sweep stays green — the memory win is not regressed.

REJECTED shapes (documented, per the plan):

* **(ii) chunked appends** — every N chars an append atom, sealed by a coalesce record
  with per-chunk ``derived_from`` tombstones. Log-native and *bounded* (amplification
  ``ceil(chars/N)+1``), but it still writes ``O(chars/N)`` chunk atoms + as many
  tombstones per part onto the hot ``_events/m`` lane (§2.10 first-GET fold must then
  merge+coalesce them) and complicates the seal byte-match (the fold must prove the
  coalesced chunk atoms equal S4's single-atom text). It buys nothing shape (i) lacks:
  the durable representation is identical (one sealed part), and mid-stream visibility is
  served from the read-model either way. Chunked appends only pay off if the log must be
  crash-durable for partial text mid-stream — which the design explicitly does NOT require
  (the live deltas are the transport plane, §2.8/1.12, skip-listed from the log; a crash
  mid-stream loses the in-flight edge in BOTH shapes because the SSE *resume buffer*, not
  the log, is the mid-stream durability plane). Its ``checkpoint_every`` knob is still
  PROVISIONED here (§2.10 Q8: "provision the checkpoint atom kind now; implement only if
  the measured budget is missed") — default OFF — so the bound has a real, testable knob.
* **(iii) a distinct storage primitive** — REJECTED by RULE 4 (no fifth store) unless
  proven a projection cache. The head slot IS a projection cache (above), i.e. exactly
  shape (i), not a new durable primitive.

Rides its own flag ``gact.live_edge_streaming`` (env ``CLIO_LIVE_EDGE_STREAMING``),
default OFF, and is engaged ONLY under the S5 **atoms** regime — justified: the seal IS
the S5 ``message_part`` atom, so without the atoms regime there is no atom lane to seal to
and no canonical-log read-model to coalesce into; the live edge has nothing to attach to
(a capability gate, like the S5 ARC gate, not a silent scrub). So both the flag AND the
regime pin must be on. Default OFF -> nothing changes: the frozen SSE shape (surface 1.2)
and the sealed reload (1.3) are byte-identical.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_KIND,
    MESSAGE_PART_SCOPE,
    _append_segment_raw,
)
from clio_agent.gact.runtime.globals import _iso_from_epoch
from clio_agent.gact.transcript_projection import atoms_active
from clio_agent.gact.types import Part
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# The process flag (file -> env -> default). Default False => the live edge is OFF and
# every seam is a no-op; the frozen wire (1.2) and sealed reload (1.3) are unchanged.
_FLAG_KEY = "gact.live_edge_streaming"
_FLAG_ENV = "CLIO_LIVE_EDGE_STREAMING"

# The ephemeral checkpoint lane (rejected shape (ii), provisioned per §2.10 Q8). A
# partition UNDER ``_events/m`` so it is search-excluded + lifecycle-erased with the log,
# distinct from the SEALED ``_events/m`` transcript lane :func:`assemble_session_messages`
# reads — so a checkpoint NEVER pollutes the sealed transcript, and the seal stays
# byte-identical regardless of checkpoint policy. Dropped at seal + session release.
LIVE_EDGE_CHECKPOINT_SCOPE = f"{MESSAGE_PART_SCOPE}/edge"

# The write-amplification budget (§4.2 step-7 risk note): the maximum DURABLE edge atoms
# the mechanism itself may write per part. Default policy (``checkpoint_every == 0``)
# writes ZERO; a single optional final-flush checkpoint is the ceiling. The seal atom is
# S5's, always exactly one, and is NOT counted here. The budget test asserts
# :func:`durable_atoms_for_part` <= this over a scripted 5k-char stream; the sabotage
# (``checkpoint_every = 1`` — "chunk size 1 / disable the bound") blows past it -> red.
LIVE_EDGE_MAX_ATOMS_PER_PART = 1

# Default checkpoint policy: 0 chars => NEVER write a durable checkpoint (pure shape-i,
# the honest default). >0 => the rejected shape-(ii) knob (chars per durable checkpoint).
DEFAULT_CHECKPOINT_EVERY = 0

# Typed no-silent-fallback reasons (the ``stream_fallback`` catalog style, §3.3/§3.4).
SEAL_MISMATCH_REASON = "live_edge_seal_mismatch"
BUDGET_EXCEEDED_REASON = "live_edge_budget_exceeded"


class LiveEdgeSealMismatchError(RuntimeError):
    """The coalesced live-edge text diverged from the transcript's closed-part text.

    Raised (never swallowed) at :meth:`LiveEdgeSlot.seal` when the head slot's coalesced
    text does not equal the authoritative buffer the transcript sealed — a divergence
    would make the in-flight edge a fabricated view of the model's output. Carries the
    typed :data:`SEAL_MISMATCH_REASON` so the failure is queryable on the trace/API.
    """

    def __init__(self, part_id: str, edge_len: int, sealed_len: int) -> None:
        self.reason = SEAL_MISMATCH_REASON
        self.part_id = part_id
        super().__init__(
            f"{SEAL_MISMATCH_REASON}: part={part_id} edge_len={edge_len} sealed_len={sealed_len}"
        )


class LiveEdgeSlot:
    """The identity-stable, in-place-growing head slot for ONE streaming part (shape i).

    The mutable atom the design highlights: it carries a stable ``part_id`` and grows in
    place as deltas arrive (:meth:`append_delta` mutates ``_chunks`` — never a new atom
    per token), so a read-model coalesces the stream into it without any log write. It is
    NOT a log record until sealed; the log gains the sealed atom from S5's persist-time
    mint, not from here. A projection cache: evictable + re-derivable (its content is the
    re-streamable sum of the deltas).
    """

    def __init__(
        self,
        *,
        part_id: str,
        agent_id: str,
        field: str,
        kind: str,
        created_at: str,
        checkpoint_every: int,
    ) -> None:
        self.part_id = part_id
        self.agent_id = agent_id
        self.field = field
        self.kind = kind
        self.created_at = created_at
        self._checkpoint_every = max(0, int(checkpoint_every))
        # THE growing buffer — mutated in place, never rebound (the "atom that grows").
        self._chunks: list[str] = []
        # Chars accumulated since the last durable checkpoint (shape-(ii) knob).
        self._since_checkpoint = 0
        # Durable edge atoms this slot has written (the write-amplification counter).
        self.durable_atoms = 0
        self.sealed = False

    def coalesced_text(self) -> str:
        """The coalesced-so-far text (the read-model's view of the growing edge)."""

        return "".join(self._chunks)

    def append_delta(self, chunk: str, *, store: Any, session_id: str) -> None:
        """Grow the head slot in place by one delta; optionally checkpoint (shape ii).

        Pure in-memory coalescing by default (``checkpoint_every == 0``): appends the
        chunk and returns — ZERO log writes, so streaming a P-char part costs O(1) atoms
        (the eventual seal), not O(P). When ``checkpoint_every > 0`` (the rejected
        shape-(ii) knob, default OFF) it writes a durable checkpoint to the ephemeral
        :data:`LIVE_EDGE_CHECKPOINT_SCOPE` every N chars — deliberately UNBOUNDED at
        ``N == 1`` so the sabotage (write-amplification) is a real config toggle the
        budget test catches.

        Args:
            chunk: The streamed text delta (empty is ignored).
            store: The ARC ``SegmentStore`` (only touched when checkpointing).
            session_id: Owning session (the checkpoint lane partition key).
        """

        if not chunk or self.sealed:
            return
        self._chunks.append(chunk)
        if self._checkpoint_every <= 0:
            return
        self._since_checkpoint += len(chunk)
        while self._since_checkpoint >= self._checkpoint_every:
            self._since_checkpoint -= self._checkpoint_every
            self._write_checkpoint(store, session_id)

    def _write_checkpoint(self, store: Any, session_id: str) -> None:
        """Write one durable checkpoint atom to the ephemeral edge lane (shape ii)."""

        _append_segment_raw(
            store,
            session_id,
            LIVE_EDGE_CHECKPOINT_SCOPE,
            MESSAGE_PART_KIND,
            {
                "atom_role": "live_edge_checkpoint",
                "part_id": self.part_id,
                "coalesced_len": len("".join(self._chunks)),
            },
        )
        self.durable_atoms += 1

    def seal(self, sealed_text: str) -> None:
        """Seal the slot; verify the coalesced edge equals the authoritative sealed text.

        The seal obligation (design: the sealed result MUST byte-match S4/S5): the slot's
        coalesced text is the read-model view of the SAME deltas the transcript buffered,
        so it must equal the transcript's closed-part text. A divergence is a fabricated
        edge and raises a typed :class:`LiveEdgeSealMismatchError` (no silent fallback).

        Args:
            sealed_text: The authoritative text the transcript sealed for this part.

        Raises:
            LiveEdgeSealMismatchError: When the coalesced edge != the sealed text.
        """

        if self.sealed:
            return
        edge = self.coalesced_text()
        if edge != sealed_text:
            stream_audit(
                "live_edge.seal_mismatch",
                part_id=self.part_id,
                reason=SEAL_MISMATCH_REASON,
                edge_len=len(edge),
                sealed_len=len(sealed_text),
            )
            raise LiveEdgeSealMismatchError(self.part_id, len(edge), len(sealed_text))
        self.sealed = True

    def overlay_part(self) -> Part:
        """Build the in-flight part carrying the coalesced-so-far text (read-model).

        The identity-stable part a mid-stream read surfaces: the same ``part_id`` /
        ``agent_id`` / ``kind`` the SSE opened, with the coalesced text filled in place
        and ``stream_source="live"`` + a ``live_edge`` marker so a consumer knows it is
        the growing edge (surface 1.3's "one live in-flight assistant message").
        """

        return Part(
            id=self.part_id,
            type=self.kind,
            agent_id=self.agent_id,
            text=self.coalesced_text(),
            metadata={
                "stream_source": "live",
                "signature_field_name": self.field,
                "live_edge": True,
            },
        )


class LiveEdgeRegistry:
    """``app.state.live_edge`` — the per-session in-flight streaming head slots.

    Holds at most one OPEN slot per session (like ``TurnTranscript._open_part``): a new
    part opening seals the prior. A durable-atom tally per part backs the write-
    amplification budget test. Thread-safe: streaming deltas arrive on the turn loop
    thread while a reader may coalesce concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open: dict[str, LiveEdgeSlot] = {}
        # part_id -> durable edge atoms written (survives the slot for the budget probe).
        self._atoms_by_part: dict[str, int] = {}

    def open_slot(
        self,
        session_id: str,
        *,
        part_id: str,
        agent_id: str,
        field: str,
        kind: str,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    ) -> LiveEdgeSlot:
        """Open (or return) the head slot for ``part_id``, sealing any prior open slot."""

        with self._lock:
            current = self._open.get(session_id)
            if current is not None and current.part_id == part_id:
                return current
            if current is not None:
                # A new part opened before the prior sealed: retire it WITHOUT a
                # byte-match assertion (the transcript owns the authoritative close; this
                # is the read-model boundary), banking its durable-atom tally.
                self._retire_locked(session_id, current)
            slot = LiveEdgeSlot(
                part_id=part_id,
                agent_id=agent_id,
                field=field,
                kind=kind,
                created_at=_iso_from_epoch(time.time()),
                checkpoint_every=checkpoint_every,
            )
            self._open[session_id] = slot
            return slot

    def append_delta(self, session_id: str, part_id: str, chunk: str, *, store: Any) -> None:
        """Grow the open slot for ``part_id`` (a no-op when it is not the open part)."""

        with self._lock:
            slot = self._open.get(session_id)
            if slot is None or slot.part_id != part_id:
                return
            slot.append_delta(chunk, store=store, session_id=session_id)

    def current_slot(self, session_id: str) -> Optional[LiveEdgeSlot]:
        """The session's open head slot, or ``None``."""

        with self._lock:
            return self._open.get(session_id)

    def seal_open(self, session_id: str, sealed_text: str) -> None:
        """Seal the session's open slot against ``sealed_text`` (the transcript's close).

        Raises :class:`LiveEdgeSealMismatchError` when the coalesced edge diverges — the
        seal byte-match guard that keeps ``reload == live`` honest.
        """

        with self._lock:
            slot = self._open.get(session_id)
            if slot is None:
                return
            slot.seal(sealed_text)
            self._retire_locked(session_id, slot)

    def durable_atoms_for_part(self, part_id: str) -> int:
        """Durable EDGE atoms written for ``part_id`` (the budget probe).

        Sums the banked tally (retired slots) with any still-OPEN slot's live count, so
        the probe is correct whether or not the part has sealed/settled yet.
        """

        with self._lock:
            total = self._atoms_by_part.get(part_id, 0)
            for slot in self._open.values():
                if slot.part_id == part_id:
                    total += slot.durable_atoms
            return total

    def drop_session(self, session_id: str, *, store: Any = None) -> None:
        """Forget a session's open slot + drop its ephemeral checkpoint lane (settle)."""

        with self._lock:
            slot = self._open.pop(session_id, None)
            if slot is not None:
                self._atoms_by_part[slot.part_id] = (
                    self._atoms_by_part.get(slot.part_id, 0) + slot.durable_atoms
                )
        if store is not None:
            try:
                store.drop_scope(session_id, LIVE_EDGE_CHECKPOINT_SCOPE)
            except Exception:  # noqa: BLE001 - an ephemeral-lane drop must never break settle
                logger.debug("live_edge: checkpoint-lane drop failed session=%s", session_id)

    def _retire_locked(self, session_id: str, slot: LiveEdgeSlot) -> None:
        """Bank a slot's durable-atom tally and clear it from the open map (lock held)."""

        self._atoms_by_part[slot.part_id] = (
            self._atoms_by_part.get(slot.part_id, 0) + slot.durable_atoms
        )
        if self._open.get(session_id) is slot:
            self._open.pop(session_id, None)


# --------------------------------------------------------------------------- #
# Flag + regime gate, and the app-state seams (default OFF => every seam a no-op)
# --------------------------------------------------------------------------- #


def _flag_on() -> bool:
    """Resolve the process live-edge flag (file -> env -> default False)."""

    return conf.resolve(_FLAG_KEY, env=_FLAG_ENV, default=False, cast=conf.as_bool)


def _arc(app: "FastAPI") -> Any:
    """The process ARC only when it holds a canonical log (has ``_segments``), else None."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    return arc if getattr(arc, "_segments", None) is not None else None


def live_edge_enabled(app: "FastAPI", session_id: str) -> bool:
    """Whether the live edge is engaged for ``session_id`` (flag AND atoms regime).

    Both gates must hold: the process flag is ON, and the session is pinned to the S5
    **atoms** regime (the seal IS the S5 atom, so the edge has nothing to attach to
    without it). Default OFF on either -> the live edge is inert.
    """

    if not _flag_on():
        return False
    if _arc(app) is None:
        return False
    return atoms_active(app)


def registry_for(app: "FastAPI") -> LiveEdgeRegistry:
    """The app's :class:`LiveEdgeRegistry`, created lazily on ``app.state.live_edge``."""

    state = app.state
    registry = getattr(state, "live_edge", None)
    if registry is None:
        registry = LiveEdgeRegistry()
        state.live_edge = registry
    return registry


def feed_delta(
    app: "FastAPI",
    session_id: str,
    *,
    part_id: str,
    agent_id: str,
    field: str,
    kind: str,
    chunk: str,
) -> None:
    """Stream-tap seam: coalesce one delta into the session's live-edge head slot.

    Called from the SSE chunk emitter AFTER the transcript appended the delta (so the
    ``part_id`` exists). Opens the slot on the first delta of a part and grows it in
    place thereafter. A no-op unless the live edge is engaged (flag + atoms regime), so
    the default streaming path is byte-identical. Best-effort-but-loud: the edge is a
    read-model overlay, never authoritative, so a coalescing failure must not break the
    turn (the transcript + the SSE deltas remain the source of truth).
    """

    if not part_id or not chunk or not live_edge_enabled(app, session_id):
        return
    arc = _arc(app)
    if arc is None:
        return
    try:
        registry = registry_for(app)
        registry.open_slot(session_id, part_id=part_id, agent_id=agent_id, field=field, kind=kind)
        registry.append_delta(session_id, part_id, chunk, store=arc._segments)
    except Exception:  # noqa: BLE001 - a read-model overlay must never break a turn
        logger.warning(
            "live_edge: feed_delta failed session=%s part=%s", session_id, part_id, exc_info=True
        )


def overlay_in_flight_part(app: "FastAPI", session_id: str, parts: list[Part]) -> list[Part]:
    """Overlay the coalesced live-edge text onto the matching in-flight part (read-model).

    Surface 1.3's "one live in-flight assistant message" carries the streaming parts; a
    still-open part's ``text`` is empty until the transcript closes it, so a mid-stream
    reload shows a blank edge. Under the live edge this fills that part's text with the
    coalesced-so-far buffer IN PLACE — the streaming-thinking cadence enablement. A no-op
    (returns ``parts`` unchanged) unless engaged and an open slot matches a part id, so
    the default projection is byte-identical.

    Args:
        app: The FastAPI app.
        session_id: The in-flight session.
        parts: The live in-flight parts (from the transcript alias).

    Returns:
        ``parts`` with the open edge part's text coalesced in place, or unchanged.
    """

    if not parts or not live_edge_enabled(app, session_id):
        return parts
    slot = registry_for(app).current_slot(session_id)
    if slot is None or slot.sealed or not slot.coalesced_text():
        return parts
    out: list[Part] = []
    for part in parts:
        if part.id == slot.part_id and not (part.text or ""):
            patched = part.model_copy(deep=True)
            patched.text = slot.coalesced_text()
            patched.metadata = {**(patched.metadata or {}), "live_edge": True}
            out.append(patched)
        else:
            out.append(part)
    return out


def seal_and_settle(app: "FastAPI", session_id: str, final_parts: list[Part]) -> None:
    """Turn-settle seam: seal the open slot against its finalized part, then clean up.

    Called on every turn exit with the transcript's FINALIZED parts. Pairs the open head
    slot with its closed part and seals it against that part's authoritative text — the
    in-process byte-match check that the coalesced edge equalled the model's output. The
    strict guard lives in :meth:`LiveEdgeSlot.seal` (raises on divergence, unit-tested);
    the *sealed-vs-finalize* invariant is owned by the S5 ``reload == live`` sweep (the
    live edge never writes the sealed atom, so that sweep stays green by construction).
    Here the mismatch is surfaced LOUD but is non-fatal — the live edge is a read-model
    overlay, never authoritative, so it must not break a turn exit (§3.4, dual-read
    best-effort-but-loud). Always drops the ephemeral checkpoint lane so none lingers. A
    no-op unless engaged.

    Args:
        app: The FastAPI app.
        session_id: The settling session.
        final_parts: The transcript's finalized parts (the sealed-text source).
    """

    if not live_edge_enabled(app, session_id):
        return
    arc = _arc(app)
    if arc is None:
        return
    registry = registry_for(app)
    slot = registry.current_slot(session_id)
    if slot is not None and not slot.sealed:
        sealed_text = next(
            (p.text for p in final_parts if p.id == slot.part_id and (p.text or "")), None
        )
        if sealed_text is not None:
            try:
                registry.seal_open(session_id, sealed_text)
            except LiveEdgeSealMismatchError:
                logger.error(
                    "live_edge: SEAL MISMATCH reason=%s session=%s part=%s (edge overlay is a "
                    "read-model, non-authoritative — turn continues; S5 sweep owns the sealed "
                    "byte-match)",
                    SEAL_MISMATCH_REASON,
                    session_id,
                    slot.part_id,
                    exc_info=True,
                )
    try:
        registry.drop_session(session_id, store=arc._segments)
    except Exception:  # noqa: BLE001 - settle cleanup must never break a turn exit
        logger.debug("live_edge: settle cleanup failed session=%s", session_id)


__all__ = [
    "DEFAULT_CHECKPOINT_EVERY",
    "LIVE_EDGE_CHECKPOINT_SCOPE",
    "LIVE_EDGE_MAX_ATOMS_PER_PART",
    "LiveEdgeRegistry",
    "LiveEdgeSealMismatchError",
    "LiveEdgeSlot",
    "feed_delta",
    "live_edge_enabled",
    "overlay_in_flight_part",
    "registry_for",
    "seal_and_settle",
]
