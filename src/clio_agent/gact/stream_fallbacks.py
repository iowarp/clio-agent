"""Typed live-stream fallback ledger."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.gact.runtime.capabilities import _STREAM_FALLBACK_REASON_DEFINITIONS

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def stream_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build structured metadata for a degraded live-delivery path."""

    definition = _STREAM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stream fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def stream_fallback_reasons(app: "FastAPI") -> dict[str, dict[str, Any]]:
    reasons = getattr(app.state, "stream_fallback_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.stream_fallback_reasons = reasons
    return reasons


def record_stream_fallback(app: "FastAPI", sid: str, reason: str, message: str = "") -> None:
    payload = stream_fallback_payload(reason, message)
    stream_fallback_reasons(app)[sid] = payload
    logger.warning(
        "live delivery degraded session_id=%s reason=%s message=%s",
        sid,
        reason,
        message or payload.get("description", ""),
    )


def peek_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    """Read a turn's live-delivery degradation without consuming it."""

    return dict(stream_fallback_reasons(app).get(sid, {}))


def pop_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    return stream_fallback_reasons(app).pop(sid, {})


# --- non-delivery degradation notes ---------------------------------------- #
# The slot above answers ONE question: how was this turn's text delivered? It is
# single-valued by design, and a live-streamed turn discards it entirely. A
# degradation that is NOT a delivery-path claim -- native model inputs the
# executing agent could not accept -- therefore cannot live there: the next
# delivery-path reason overwrites it, and a turn that streamed cleanly drops it.
# Notes are a bounded APPEND ledger over the SAME audited catalog, so such a
# degradation stays queryable instead of being clobbered into silence.


def _max_notes_per_session() -> int:
    """Cap per session so a long-lived session cannot grow the ledger unbounded.

    Config: ``gact.ledger_retention.stream_fallback_notes.max`` /
    ``CLIO_LEDGER_STREAM_FALLBACK_NOTES_MAX``. Sits in the same
    ``gact.ledger_retention.*`` namespace as every other in-process ledger bound
    (``runtime/retention.py``); this ledger is a per-session dict of lists rather
    than one flat list, so it resolves its own bound instead of riding
    ``LEDGER_BOUNDS``.
    """

    return max(
        1,
        conf.resolve(
            "gact.ledger_retention.stream_fallback_notes.max",
            env="CLIO_LEDGER_STREAM_FALLBACK_NOTES_MAX",
            default=32,
            cast=conf.as_int,
        ),
    )


def stream_fallback_notes(app: "FastAPI") -> dict[str, list[dict[str, Any]]]:
    """Return the per-session note ledger, creating it on first use."""

    notes = getattr(app.state, "stream_fallback_notes", None)
    if not isinstance(notes, dict):
        notes = {}
        app.state.stream_fallback_notes = notes
    return notes


def record_stream_fallback_note(app: "FastAPI", sid: str, reason: str, message: str = "") -> None:
    """Append a typed degradation that does NOT describe the delivery path.

    Validated against the same audited catalog as :func:`record_stream_fallback`
    (unknown reasons raise), so a note is never an ad-hoc string. Consecutive
    same-message records are collapsed, mirroring the workflow-schema ledger.
    """

    if app is None or getattr(app, "state", None) is None or not sid:
        return
    payload = stream_fallback_payload(reason, message)
    entries = stream_fallback_notes(app).setdefault(sid, [])
    if not entries or entries[-1].get("message") != message or entries[-1]["reason"] != reason:
        entries.append(payload)
    max_notes = _max_notes_per_session()
    if len(entries) > max_notes:
        del entries[:-max_notes]
    logger.warning(
        "turn degraded without changing the delivery path session_id=%s reason=%s message=%s",
        sid,
        reason,
        message or payload.get("description", ""),
    )


def pop_stream_fallback_notes(app: "FastAPI", sid: str) -> list[dict[str, Any]]:
    """Drain a session's degradation notes for the finishing turn's metadata."""

    return stream_fallback_notes(app).pop(sid, [])
