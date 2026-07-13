"""The gact-visible transcript as a PROJECTION of the ``message_part`` atoms (#737 S5).

S4 (:mod:`clio_agent.gact.part_atoms`) provisioned a wire-identity atom family on the
canonical ``_events/m`` log, **dual-written** alongside the ``final_message`` byte-copy
and the messages-store ledger, with *no* reader consuming it. This slice (design
``docs/design/unified-arc-highway.md`` §2.8c, §4.2 step 5) flips the read regime: under
the **atoms** regime a session's transcript is *assembled by reference* from those
atoms — ``GET /messages`` and every ``app.state.messages`` reader materialize the
ledger from the log, the ``final_message`` byte-copy in ``turn_finalize`` is no longer
emitted, and the atom lane becomes the single source of truth for the transcript.

The regime is **session-scoped and pinned at the session's FIRST message** (design
§4.4b/c — never a mid-session flip): the process flag ``gact.transcript_projection``
(env ``CLIO_TRANSCRIPT_PROJECTION``) is resolved ONCE, on message #1, and — when ON —
stamped as ``metadata["transcript_regime"] = "atoms"`` on the session record. Every
later read/write consults the *pinned* value, never the live flag, so a session is
EITHER atoms-regime or legacy end-to-end. Under the legacy regime (flag =0) NOTHING is
pinned and NOTHING changes — the legacy messages-store read path and the
``final_message`` embed are byte-for-byte preserved (the shipped default; see the
module-level "shipped default" note below).

Design decisions (each answering a named constraint):

* **The projection IS ``assemble(atoms).to_wire()`` (design (a), §2.8c).** A ``Message``
  is its grouped part atoms in append order, reproduced via the S4-proven
  :func:`~clio_agent.gact.part_atoms.reproduce_message_wire`. Byte-equal to today under
  the §4.1.A normalizer because the atoms carry the WHOLE part + message dumps minted
  once at persist (never re-minted on read — the ``reload == live`` identity pin).
* **The MessageStore ledger is RETAINED as a re-derivable atom-fold cache + boot index
  (design (e), §3.2).** It is NOT the atoms-regime read source — the atoms are — but it
  is kept: (1) as the ``#889`` boot index (``session_ids`` / ``has_session``, no body
  read); (2) as the legacy-regime store and the rollback read path (§4.3 dual-write
  window, F2); (3) as the backfill SOURCE for pre-atom ledgers (:func:`mint_atoms_from_ledger`);
  (4) as a warm, re-derivable materialization of the same projection. Demoting it from
  source-of-truth to cache — rather than deleting it in this one slice — keeps the
  session-scoped rollback trivially sound and the boot index unchanged. Documented here
  as the residency choice §3.2/(e) asks for.
* **The atom lane IS the gact-visible transcript projection, distinct from ARC memory
  (frozen (f), §2.5 ``transcript_delete``).** Undo/rewind/``DELETE``/fork/compact/import
  re-materialize the ``_events/m`` lane (:func:`on_ledger_replaced` /
  :func:`on_ledger_deleted`) and NEVER touch the ARC working-set scopes — so
  ``memory_scope:"gact_visible_transcript_only"`` holds by construction (the sabotage-c
  guard: a transcript delete that reached ARC memory would fail its frozen test).
* **No silent fallback; must-succeed under atoms-regime (§3.4).** Once the atoms are the
  ONE copy, a dropped mint is permanent transcript loss, so under the atoms regime a
  mint failure raises a typed :class:`TranscriptIngestError` (fails the turn, loud).
  Under the legacy regime the messages-store copy is still authoritative, so minting
  stays best-effort-but-loud exactly as S4 landed it.

Shipped default — **flag ON (atoms regime for NEW sessions)** since the whole-surface
bar was met: the reload==live corpus sweep is green on the full real-session corpus
(:mod:`tests.test_equivalence`), scripted live turns byte-match
(:mod:`tests.test_gact.test_transcript_projection`), and the campaign's final live
web gate runs the entire stack under this default before any release tag (release
authority 2026-07-12, #893/#737). ``CLIO_TRANSCRIPT_PROJECTION=0`` /
``gact.transcript_projection`` opts back into the legacy regime. Existing sessions
are untouched either way — the regime is pinned per session at message #1, so a
legacy session stays legacy for life and its wire stays byte-identical. If
``reload == live`` could not be green under §4.1.A the switch would NOT ship at all
(§5.2 Q4) — it is; a divergence, were there one, would name the exact field via the
S0 field-path differ.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_SCOPE,
    build_message_part_atoms,
    mint_message_part_atoms,
    reproduce_message_wire,
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

# The two session-scoped read regimes. ``atoms`` = assemble the transcript by reference
# from the ``message_part`` log; ``legacy`` = read the messages-store ledger (today).
REGIME_ATOMS = "atoms"
REGIME_LEGACY = "legacy"

# The session-metadata key the regime is pinned under (only ever written for the atoms
# regime, so the default legacy path leaves the session wire byte-identical).
REGIME_METADATA_KEY = "transcript_regime"

# The process flag (file -> env -> default). Default True => atoms is the shipped
# regime for NEW sessions (whole-surface proofs green; see the module docstring);
# =0 opts back into legacy. Existing sessions keep their pinned regime either way.
_FLAG_KEY = "gact.transcript_projection"
_FLAG_ENV = "CLIO_TRANSCRIPT_PROJECTION"

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


def _flag_on() -> bool:
    """Resolve the process transcript-projection flag (file -> env -> default True)."""

    return conf.resolve(_FLAG_KEY, env=_FLAG_ENV, default=True, cast=conf.as_bool)


def _sessions(app: "FastAPI") -> Any:
    """The session store, or ``None`` when the app carries none (minimal test wiring)."""

    return getattr(getattr(app, "state", None), "sessions", None)


def _arc(app: "FastAPI") -> Any:
    """The process ARC memory ONLY when it can hold the canonical log, else ``None``.

    Returns the ARC only if it exposes the segment store the atom lane needs
    (``_segments``); an app with no ARC (minimal test wiring) OR a metrics-only / degraded
    ARC stub without a segment store has NO canonical log to project, so the atoms regime
    cannot engage — the pin refuses atoms and every write/read seam falls back to the
    legacy messages-store path. This is a capability gate (like the LocalFS degradation),
    not a silent scrub: without a segment store there is simply no log to write or read.
    """

    arc = getattr(getattr(app, "state", None), "arc", None)
    return arc if getattr(arc, "_segments", None) is not None else None


def pinned_regime(app: "FastAPI", session_id: str) -> str:
    """Return a session's PINNED transcript regime (read-only; never resolves the flag).

    Reads ``metadata[transcript_regime]`` off the session record; a session with no pin
    (the default legacy path, or one predating S5) is :data:`REGIME_LEGACY`. The read
    path uses this so a session's regime is fixed for its whole life — a live flag flip
    can never change how an existing session is read (design §4.4b/c).
    """

    sessions = _sessions(app)
    if sessions is None:
        return REGIME_LEGACY
    record = sessions.get(session_id)
    if record is None:
        return REGIME_LEGACY
    regime = (getattr(record, "metadata", None) or {}).get(REGIME_METADATA_KEY)
    return REGIME_ATOMS if regime == REGIME_ATOMS else REGIME_LEGACY


def _pin_regime_on_first_message(app: "FastAPI", session_id: str, is_session_start: bool) -> str:
    """Resolve + pin the regime for a session, ONLY on its first message.

    On message #1 the flag is resolved once; when ON the session is stamped
    ``metadata[transcript_regime]="atoms"`` (persisted). On every LATER message the
    already-pinned value is returned; an unpinned session (default legacy, or one whose
    first message predated S5) stays :data:`REGIME_LEGACY` and is NEVER re-resolved — so
    a mid-life flag flip cannot flip an in-progress session (design §4.4b/c). Legacy is
    never written to metadata, so the default session wire is byte-identical.
    """

    current = pinned_regime(app, session_id)
    if current == REGIME_ATOMS:
        return REGIME_ATOMS
    if not is_session_start:
        return REGIME_LEGACY
    if not _flag_on():
        return REGIME_LEGACY
    if _arc(app) is None:  # no canonical-log substrate -> the atoms regime cannot engage
        return REGIME_LEGACY
    sessions = _sessions(app)
    if sessions is None:
        return REGIME_LEGACY
    updated = sessions.update(session_id, metadata_patch={REGIME_METADATA_KEY: REGIME_ATOMS})
    if updated is None:  # session vanished between append and pin — stay legacy, loud
        logger.warning(
            "transcript_projection: could not pin atoms regime (session %s absent)", session_id
        )
        return REGIME_LEGACY
    logger.debug("transcript_projection: pinned atoms regime session=%s", session_id)
    return REGIME_ATOMS


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
    groups: list[list[dict[str, Any]]] = []
    for seg in store.list_segments(session_id, MESSAGE_PART_SCOPE, include_tombstoned=False):
        content = seg.content
        if int(content.get("part_index", 0) or 0) == 0 or not groups:
            groups.append([content])  # a fresh message begins (part_index resets to 0)
        else:
            groups[-1].append(content)
    messages = [Message(**reproduce_message_wire(atoms)) for atoms in groups]
    # #737 S6: workflow_state on the delegate rows is the recorded RESULT of the last
    # state_merge op for the scope — materialized schema-free here, NEVER re-folded on
    # read (design §2.8.d). A no-op when no op was recorded (rows keep their verbatim,
    # equally-frozen value).
    materialize_state_merge_projection(arc, session_id, messages)
    return messages


def has_atoms(arc: Any, session_id: str) -> bool:
    """Whether the session has ANY live ``message_part`` atom on the canonical log."""

    store = arc._segments
    segs = store.list_segments(session_id, MESSAGE_PART_SCOPE, include_tombstoned=False)
    return bool(segs)


# --------------------------------------------------------------------------- #
# Backfill — the documented migration seam for pre-atom ledgers (design (d))
# --------------------------------------------------------------------------- #


def mint_atoms_from_ledger(arc: Any, session_id: str, messages: list[Message]) -> None:
    """Mint the ``message_part`` atoms for a ledger that PREDATES the atom family.

    The documented migration seam (design (d)): the local 60-ledger corpus (and any
    pre-S5 install session) has messages-store rows but no atoms, so the atoms-regime
    read path backfills them once from the ledger before assembling. Minting is
    per-message and raises a typed :class:`TranscriptBackfillError` on the FIRST
    un-mintable message — never a silent skip that would serve a truncated transcript.

    Args:
        arc: The process ARC memory.
        session_id: Owning session.
        messages: The ledger rows to provision atoms for (in chronological order).
    """

    for message in messages:
        try:
            mint_message_part_atoms(arc, session_id, message)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed, per-message reason
            raise TranscriptBackfillError(session_id, getattr(message, "id", ""), exc) from exc
        # #737 S6: re-provision the state_merge op from the ledger too, so the recorded
        # result survives a lifecycle-erased (#762) or pre-S6 ledger backfill.
        record_state_merge_best_effort(arc, session_id, message)


# --------------------------------------------------------------------------- #
# The resident-set materializer (read-path switch) — design (c)
# --------------------------------------------------------------------------- #


def materialize_ledger(app: "FastAPI", session_id: str) -> Optional[list[Message]]:
    """Materialize a session's ledger for the resident set (the read-path switch).

    Repoints :class:`~clio_agent.gact.resident_ledgers.ResidentLedgerSet` rehydration:
    under the **atoms** regime the ledger is assembled from the canonical log
    (:func:`assemble_session_messages`), backfilling once from the RETAINED messages-store
    ledger (:func:`mint_atoms_from_ledger`) when the atom lane is absent — either because
    the session predates the atoms (migration) OR because the ``_events/m`` lane was
    lifecycle-erased by a trace-enabled ``release_session`` (#762: under
    ``trace.backend=file/factory`` the whole ``_events`` family is dropped on session
    release, and the atoms are NOT in the durable JSONL trace). The retained store copy
    is precisely the re-derivable fallback that makes the atoms regime robust to that
    erase — the reason the residency decision keeps it (see the module docstring). Under
    the **legacy** regime it falls through to ``MessageStore.load_session`` exactly as
    today. The LRU/TTL/pinning semantics (#889) are unchanged — only the SOURCE moves.

    Preserves the store's contract precisely: ``None`` => the session has no ledger (a
    cache-miss ``KeyError`` upstream), ``[]`` => an existing-but-empty ledger,
    :class:`LedgerReadError` propagates (never cached as an empty transcript, §3.3).

    Args:
        app: The FastAPI app.
        session_id: The session to materialize.

    Returns:
        The session's ``list[Message]``, or ``None`` when it has never been persisted.
    """

    store = getattr(getattr(app, "state", None), "message_store", None)
    arc = _arc(app)
    if pinned_regime(app, session_id) != REGIME_ATOMS or arc is None:
        # Legacy regime (the default) OR no canonical log available: today's path.
        return None if store is None else store.load_session(session_id)

    if has_atoms(arc, session_id):
        return assemble_session_messages(arc, session_id)

    # Atoms regime, but no atoms yet: either a brand-new session (no ledger) or a
    # pre-atom ledger to backfill once. Distinguish via the store (LedgerReadError
    # propagates — never a silent empty).
    ledger = None if store is None else store.load_session(session_id)
    if not ledger:
        return ledger  # None (never persisted) or [] (empty) — both pass through
    mint_atoms_from_ledger(arc, session_id, ledger)
    return assemble_session_messages(arc, session_id)


# --------------------------------------------------------------------------- #
# Write-seam hooks — keep the atom lane the source of truth (design (b))
# --------------------------------------------------------------------------- #


def on_message_appended(app: "FastAPI", session_id: str, message: Message) -> None:
    """Persist-seam hook: pin the regime (first message) + mint the message's atoms.

    Called from ``session_store._append_session_message`` — the single append-one
    persist choke point. On message #1 it resolves + pins the session regime; then it
    mints the ``message_part`` atoms. Under the **atoms** regime the mint is
    MUST-SUCCEED (a failure raises :class:`TranscriptIngestError` and fails the turn —
    the atoms are the one copy, §3.4); under the **legacy** regime the messages-store
    copy is still authoritative so the mint is best-effort-but-loud, exactly as S4
    landed it.

    Args:
        app: The FastAPI app (``app.state.arc`` is the canonical-log home).
        session_id: Owning session.
        message: The just-persisted gact message.
    """

    arc = _arc(app)
    if arc is None:
        logger.debug(
            "transcript_projection: no ARC on app.state; skipping mint (session=%s message=%s)",
            session_id,
            getattr(message, "id", ""),
        )
        return
    messages_state = getattr(getattr(app, "state", None), "messages", None)
    resident = messages_state.get(session_id, []) if messages_state is not None else []
    is_session_start = len(resident) <= 1
    regime = _pin_regime_on_first_message(app, session_id, is_session_start)

    try:
        mint_message_part_atoms(arc, session_id, message)
    except Exception as exc:  # noqa: BLE001 - policy branches on the pinned regime
        if regime == REGIME_ATOMS:
            # The atoms are the ONE copy: fail loud, never a silent transcript gap.
            logger.error(
                "transcript_projection: mint FAILED reason=%s session=%s message=%s (atoms "
                "regime — the turn fails; no half-committed transcript is served)",
                INGEST_FAILED_REASON,
                session_id,
                getattr(message, "id", ""),
                exc_info=True,
            )
            raise TranscriptIngestError(session_id, getattr(message, "id", ""), exc) from exc
        # Legacy regime: final_message / the messages-store copy is authoritative.
        logger.error(
            "transcript_projection: mint FAILED reason=%s session=%s message=%s (legacy regime "
            "— messages-store copy is authoritative; atoms invisible)",
            INGEST_FAILED_REASON,
            session_id,
            getattr(message, "id", ""),
            exc_info=True,
        )
        return
    # #737 S6: the mint landed — record the delegated-turn workflow_state as a
    # state_merge op (atoms regime only; the legacy transcript is served from the
    # messages-store ledger, which carries workflow_state verbatim and is never
    # re-folded, so an op there is dead weight). Best-effort-but-loud: the verbatim
    # message-part copy is the frozen fallback (§3.4).
    if regime == REGIME_ATOMS:
        record_state_merge_best_effort(arc, session_id, message)


def on_messages_extended(app: "FastAPI", session_id: str, messages: list[Message]) -> None:
    """Extend-seam hook: mint atoms for each appended message under the atoms regime.

    Backs ``session_store._extend_session_messages`` (nanoagent sub-turn ledgers). Under
    the legacy regime this is a no-op (the messages-store copy is authoritative); under
    the atoms regime each message is minted so the assembled projection is complete.
    """

    if not messages or pinned_regime(app, session_id) != REGIME_ATOMS:
        return
    arc = _arc(app)
    if arc is None:
        return
    for message in messages:
        try:
            mint_message_part_atoms(arc, session_id, message)
        except Exception as exc:  # noqa: BLE001 - atoms are the one copy under this regime
            raise TranscriptIngestError(session_id, getattr(message, "id", ""), exc) from exc
        record_state_merge_best_effort(arc, session_id, message)  # #737 S6


def on_ledger_replaced(app: "FastAPI", session_id: str, messages: list[Message]) -> None:
    """Replace-seam hook: re-materialize the atom lane to EXACTLY the new ledger.

    Backs ``session_store._replace_session_messages`` — undo/rewind/fork/compact/import.
    Under the atoms regime the ``_events/m`` lane is dropped and re-minted from the new
    ledger, so the assembled projection matches the replaced list. This is the
    ``transcript_delete``+re-append of design §2.5, scoped to the gact-visible transcript
    projection ONLY: it touches the ``_events/m`` lane and NEVER the ARC working-set
    scopes, so ``memory_scope:"gact_visible_transcript_only"`` holds by construction (the
    sabotage-c guard). A no-op under the legacy regime.
    """

    if pinned_regime(app, session_id) != REGIME_ATOMS:
        return
    arc = _arc(app)
    if arc is None:
        return
    arc._segments.drop_scope(session_id, MESSAGE_PART_SCOPE)
    drop_state_merge_lane(arc, session_id)  # #737 S6: re-materialise the op lane too
    for message in messages:
        try:
            mint_message_part_atoms(arc, session_id, message)
        except Exception as exc:  # noqa: BLE001 - atoms are the one copy under this regime
            raise TranscriptIngestError(session_id, getattr(message, "id", ""), exc) from exc
        record_state_merge_best_effort(arc, session_id, message)  # #737 S6


def on_ledger_deleted(app: "FastAPI", session_id: str) -> None:
    """Delete-seam hook: drop the session's atom lane (transcript projection erasure).

    Backs ``session_store._delete_session_messages`` (``DELETE /messages`` /
    ``session.cleared`` / the ``DELETE /sessions`` cascade). Drops ONLY the
    ``_events/m`` transcript lane; the ARC working-set scopes (ARC memory) are untouched
    — the frozen ``gact_visible_transcript_only`` semantics (1.11, sabotage-c). Runs
    under BOTH regimes so a legacy session that later flips is never left with a stale
    lane; it is a cheap partition drop when no atoms exist.
    """

    arc = _arc(app)
    if arc is None:
        return
    arc._segments.drop_scope(session_id, MESSAGE_PART_SCOPE)
    drop_state_merge_lane(arc, session_id)  # #737 S6: erase the op lane with the transcript


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

    if pinned_regime(app, session_id) == REGIME_ATOMS:
        return {}
    return {"final_message": assistant_msg.model_dump(exclude_none=True)}


# Kept importable for tests/tools that build atoms without a full app (pure helper).
__all__ = [
    "REGIME_ATOMS",
    "REGIME_LEGACY",
    "REGIME_METADATA_KEY",
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
    "on_message_appended",
    "on_messages_extended",
    "pinned_regime",
]
