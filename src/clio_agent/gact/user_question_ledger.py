"""The one write funnel into the live ``user_questions`` ledger.

``app.state.user_questions`` is written from five producers (the native ask tool's
restart replay, the elicitation bridge, plan-mode, the per-session question route
and the pause path) and read in full by the interaction projection. It was the
only such in-memory ledger with no retention bound at all, so a long-lived server
accumulated every question it had ever asked and re-scanned them on every
projection.

Routing every write through here gives the ledger the same terminal-first,
typed-eviction policy as ``permissions`` and ``turn_attempts``: a still-PENDING
question -- someone is waiting on it -- is preserved past the soft cap, and only
the hard cap force-evicts one, with a recorded reason either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.retention import enforce_dict_bound

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["record_user_question"]


def record_user_question(app: "FastAPI", question: Any) -> Any:
    """Store one question in the live ledger and enforce its retention bound."""

    app.state.user_questions[question.id] = question
    enforce_dict_bound(
        app,
        app.state.user_questions,
        "user_questions",
        session_id=str(getattr(question, "session_id", "") or ""),
    )
    return question
