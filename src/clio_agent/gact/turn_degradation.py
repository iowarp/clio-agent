"""Unified turn-degradation ledger + finalize drain (#736 no-silent-fallback unify).

#736 introduced the record-half of a no-silent-fallback mechanism as write-only
sibling ledgers that no ``src/`` code ever read. A ledger that is written but never
drained satisfies the no-silent-fallback rule in LETTER ONLY — the degradation
never reaches the trace/API, so it is still silent.

This owner module unifies the MECHANISM (not the reason-set): ONE always-on,
typed-catalog-validated, per-session LIST ledger — ``app.state.turn_degradations``
— drained ONCE at finalize onto the assistant message's
``.metadata.turn_degradations``. It is deliberately NOT folded into
``stream_fallback``: that catalog answers a DELIVERY-PATH question (streamed live
vs batch/synthetic), is a CLOSED set surfaced to clients as the
``x_clio_stream_fallback_reasons`` capability, and is SINGLE-SLOT (last write
wins). These degradations are a different axis — answer CONTENT (substituted
delegation evidence) and pack CONFIG (an un-migrated blueprint) — and a LIST so a
cause (empty final responder) and its effect (substituted answer) co-exist on the
same turn. Folding them in would mislabel them, pollute the closed capability set,
and collide on the single-slot semantics when a turn BOTH falls back to batch AND
substitutes an empty answer.

The catalog follows the same typed, reject-unknowns pattern as the
``_stream_fallback_payload`` sibling; the ledger mirrors the sibling ledgers'
consecutive-dedup + bounded-cap discipline so a long-lived session cannot grow it
without bound. The SINGLE new READ is
:func:`assemble_stream_and_degradation_metadata` — delete its drain and
``.metadata.turn_degradations`` never appears (the mechanism's teeth).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from clio_agent.gact.delegation import _fallback_answer_from_delegation

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)

# --- unified turn-degradation reason catalog (#736 unify) -------------------- #
# The MERGED catalog of the two former sibling catalogs plus the NEW substitution
# reason. Each entry is typed (category + recovery_actions + description); unknown
# reasons are rejected by :func:`_turn_degradation_payload`, mirroring
# ``_stream_fallback_payload``. These reasons are deliberately ABSENT from the
# closed ``_STREAM_FALLBACK_REASON_DEFINITIONS`` capability set — they live on a
# different (content/config) axis and must not contaminate that contract.
_TURN_DEGRADATION_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "answer_substituted_from_delegation_evidence": {
        "category": "delegation_degradation",
        "recovery_actions": [
            "inspect_child_expert_output",
            "review_substituted_delegation_evidence",
        ],
        "description": (
            "The turn's answer was empty but expert handoffs ran, so finalize "
            "substituted the latest completed parent-resume delegation evidence "
            "as the user-facing answer. Recorded so the substituted answer the "
            "user actually sees is queryable rather than a silent content swap."
        ),
    },
}

# Cap the per-session ledger so a long-lived session cannot grow it without bound;
# consecutive same-message records are de-duplicated before this cap is consulted.
_MAX_TURN_DEGRADATION_ENTRIES = 64

# One process-wide lock serializing the lazy get-or-create of a per-app ledger dict
# and its per-session read-modify-write. Two concurrent turns for DIFFERENT sessions
# that BOTH first-touch the ledger would otherwise each install a fresh dict via the
# check-then-setattr in :func:`_session_store`, silently orphaning the loser's session
# ledger -- the exact silent loss this module exists to prevent, one layer down. The
# lock is only ever held for O(1) dict ops (never across an LM/tool call), so it
# cannot bottleneck a turn.
_LEDGER_LOCK = threading.Lock()

# The attribute name of the per-app per-session ledger on ``app.state``.
_LEDGER_ATTR = "turn_degradations"


def _session_store(app: Any) -> dict[str, list[dict[str, Any]]] | None:
    """Get-or-create ``app.state.turn_degradations`` from the EXPLICIT ``app``.

    The ledger's OWN store accessor -- deliberately NOT the shared ``per_app_dict``.
    It reads only the passed ``app``, never the ``active_app()`` contextvar, so a
    record can never leak onto a sibling turn's app under a parallel / randomized
    suite; and the caller-held :data:`_LEDGER_LOCK` makes the lazy check-then-setattr
    ATOMIC so a concurrent first-touch cannot orphan a session's ledger.
    (``per_app_dict``'s check-then-set is not atomic and silently returns a throwaway
    ``{}`` when state-less; the #770 expert caches that share it are left untouched --
    only the degradation ledger is routed through this owner-module accessor.)

    Args:
        app: The FastAPI app whose ``.state`` carries the ledger (may be ``None``).

    Returns:
        The per-app ``{session_id: [payload, ...]}`` store, or ``None`` when there is
        no ``app.state`` to attribute to (app-less / state-less construction).
    """

    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return None
    store = getattr(state, _LEDGER_ATTR, None)
    if not isinstance(store, dict):
        store = {}
        setattr(state, _LEDGER_ATTR, store)
    return store


def _turn_degradation_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured, typed payload for a turn degradation.

    Mirrors :func:`clio_agent.gact.streaming._stream_fallback_payload` (validate
    against a typed catalog, reject unknowns) so a degradation records a queryable
    typed reason instead of a silent substitution / stream-visibility flip.

    Args:
        reason: A key of :data:`_TURN_DEGRADATION_REASON_DEFINITIONS`.
        message: Optional per-record detail (e.g. the parent/child ids or the
            offending expert identity).

    Returns:
        The catalog definition merged with ``reason`` (and ``message`` when given).

    Raises:
        ValueError: When ``reason`` is not a registered turn-degradation reason.
    """

    definition = _TURN_DEGRADATION_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown turn degradation reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def record_turn_degradation(
    app: Any,
    sid: str,
    reason: str,
    message: str = "",
) -> None:
    """Record a structured turn-degradation reason for a session (ALWAYS ON).

    Builds the reason from the unified catalog (via
    :func:`_turn_degradation_payload`) and appends it to the per-app
    ``turn_degradations`` LIST ledger so the degradation is queryable in a DEFAULT
    deployment — unlike ``stream_audit``, which writes nothing unless
    ``CLIO_STREAM_AUDIT_LOG`` is set. Consecutive same-message records for a
    session are collapsed and the ledger is capped, mirroring the ``stream_fallback``
    sibling, so a session cannot grow it without bound. A degradation that cannot be
    attributed to a live per-session ledger (missing app/state or session id) CANNOT
    persist -- but it is surfaced at ``WARNING`` rather than silently dropped
    (no-silent-fallback: the ``bare return`` this replaces vanished the record with no
    signal, the very failure mode the ledger exists to prevent).

    Args:
        app: The FastAPI app whose ``.state`` carries the ledger (may be ``None``).
        sid: The session id the degradation is attributed to.
        reason: A key of :data:`_TURN_DEGRADATION_REASON_DEFINITIONS`.
        message: Optional per-record detail (e.g. the parent/child ids).
    """

    with _LEDGER_LOCK:
        store = _session_store(app) if sid else None
        if store is None:
            # No-silent-swallow: the record has no live per-session ledger to land
            # on (app-less / state-less / session-less construction -- e.g. an
            # out-of-turn catalog preview). It cannot persist, but it must not
            # vanish, so surface it at WARNING (an unconditional logging.warning) so
            # the dropped downgrade reaches the logs/trace instead of a silent ``return``.
            logger.warning(
                "turn degradation not persisted (no per-session ledger: "
                "app=%s state=%s sid=%r): reason=%s message=%s",
                app is not None,
                getattr(app, "state", None) is not None,
                sid,
                reason,
                message,
            )
            return
        payload = _turn_degradation_payload(reason, message)
        entries = store.setdefault(sid, [])
        if not entries or entries[-1].get("message") != message:
            entries.append(payload)
        if len(entries) > _MAX_TURN_DEGRADATION_ENTRIES:
            del entries[:-_MAX_TURN_DEGRADATION_ENTRIES]


def pop_turn_degradations(app: Any, sid: str) -> list[dict[str, Any]]:
    """Destructively drain a session's turn-degradation payloads (finalize seam).

    Mirrors :func:`clio_agent.gact.streaming._pop_stream_fallback` — returns the
    session's accumulated payloads and clears them from the ledger, so each turn's
    finalize drains exactly its own degradations onto the assistant message. A
    missing app/state or session id returns an empty list (nothing to drain).

    Args:
        app: The FastAPI app whose ``.state`` carries the ledger (may be ``None``).
        sid: The session id whose payloads to drain.

    Returns:
        The list of typed payloads for ``sid`` (empty when none / app-less).
    """

    if not sid:
        return []
    with _LEDGER_LOCK:
        store = _session_store(app)
        if store is None:
            return []
        return store.pop(sid, [])


def substitute_answer_from_delegation_evidence(state: "TurnState") -> str:
    """Return delegation evidence as the answer, recording the substitution.

    Wraps the UNCHANGED pure
    :func:`clio_agent.gact.delegation._fallback_answer_from_delegation` (the latest
    completed parent-resume output) and, when it yields a NON-EMPTY substitution,
    records ``answer_substituted_from_delegation_evidence`` on the always-on ledger
    so the content swap the user actually sees is queryable rather than silent. An
    empty result is NOT a substitution — finalize raises ``empty_response`` for
    that case — so nothing is recorded.

    Args:
        state: The active turn state (carries ``expert_handoffs``, ``app``, ``sid``).

    Returns:
        The substituted answer text (``""`` when no evidence was available).
    """

    answer = _fallback_answer_from_delegation(state.expert_handoffs)
    if answer:
        record_turn_degradation(
            state.app,
            state.sid,
            "answer_substituted_from_delegation_evidence",
            "latest completed parent.resumed delegation evidence",
        )
    return answer


def assemble_stream_and_degradation_metadata(
    state: "TurnState",
    *,
    stream_fallback: dict[str, Any],
    current_stream_part_id: str | None,
    has_live_parts: bool,
) -> None:
    """Stamp stream provenance + drain turn degradations onto assistant metadata.

    Relocated VERBATIM from ``turn_finalize.finalize_turn`` (behaviour byte-identical
    for ``stream_source`` / ``stream_fallback``) and additionally drains the unified
    turn-degradation ledger onto ``.metadata.turn_degradations`` — the SINGLE new
    read of that ledger. Delete the drain block below and the ledger becomes
    write-only again (the mechanism's teeth).

    ``current_stream_part_id`` (captured BEFORE the finalize appends) keeps the
    legacy semantic: a text part opened since the last mid-turn runtime boundary
    marks the turn's text as live-streamed even after it closed. Per-part
    ``stream_source`` is no longer restamped here — every part carries the
    provenance its producer appended it with (#767 PR3: finalize never rewrites the
    ledger).

    Args:
        state: The active turn state (mutated: ``assistant_metadata`` is written).
        stream_fallback: The popped single-slot stream-fallback payload for the turn.
        current_stream_part_id: The pre-append open streamed text part id (or ``None``).
        has_live_parts: Whether any live part streamed this turn.
    """

    should_report_stream_provenance = (
        bool(state.answer_text) or state.error_info is not None or has_live_parts
    )
    text_stream_source = ""
    if bool(state.answer_text) or state.error_info is not None:
        text_stream_source = "live" if current_stream_part_id is not None else "batch"
    elif has_live_parts:
        text_stream_source = "live"
    if should_report_stream_provenance and text_stream_source:
        state.assistant_metadata["stream_source"] = text_stream_source
    if text_stream_source == "batch" and (bool(state.answer_text) or state.error_info is not None):
        state.assistant_metadata["stream_fallback"] = stream_fallback
    # The SINGLE read of the unified turn-degradation ledger: drain this session's
    # accumulated content/config degradations onto the assistant message metadata so
    # they reach the persisted Message + turn.completed trace (no-silent-fallback).
    payloads = pop_turn_degradations(state.app, state.sid)
    if payloads:
        state.assistant_metadata["turn_degradations"] = payloads
