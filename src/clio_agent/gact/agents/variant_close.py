"""A human-judged run that ends without a pick: superseded, cancelled or expired (Phase 9).

A human-judged run pauses on a ``choice`` question (:mod:`variant_drafts`); the session
row points at it (:data:`PENDING_META`). Three things end it without a pick, each a
deliberate, typed terminal state of the run in clio-core:

* ``superseded`` -- a new turn starts in the session while the question is still
  pending: the user sent a new message instead of picking. Like an unanswered
  ``ask_user`` question, the drafts do not block the user; unlike an ``ask_user``
  answer (new information, appended where it arrives), a pick chooses how the drafting
  turn ended, and the newer turn already continued from that turn as it stood. A later
  pick could only splice its line into the past, so the new turn closes the question
  (``cancelled``, ``metadata.variant_resolution: "superseded"``) through the one atomic
  transition every answer takes: no later answer can resume the run (it gets 409).
* ``cancelled`` -- the user dismissed the question.
* ``expired`` -- the pick's window the agent asked for (``expiresInSeconds``) ended.

Each close records the run (status, ``closed_reason``, ``closed_at``, ``question_id``,
``superseded_by_message_id``), emits ``variant.closed`` so the variant tabs show it,
clears the session's pointer, and leaves a notice the next turn's agent is told as an
injection (``source: variant_closed``): none of the drafts is part of the
conversation. The preference record keeps the candidates, with no pick.

Restart-safe: the question rows and the run are durable. A close the process did not
finish (the question is terminal, the run still ``awaiting_pick``) is completed at the
session's next turn (:func:`turn_start_notices`) or the agent's next forward.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.agents.variant_events import emit_closed
from clio_agent.gact.agents.variant_records import CLOSED_STATUSES, VariantRun, load_run, save_run

logger = logging.getLogger(__name__)

__all__ = [
    "CLOSED_SOURCE",
    "NOTICE_META",
    "PENDING_META",
    "close_after_question",
    "close_for_question",
    "close_run",
    "latest_question",
    "turn_start_notices",
]

#: Session metadata: the human-judged run waiting on this session's user.
PENDING_META = "variant_pending"
#: Session metadata: a run closed without a pick, which the next turn's agent is told.
NOTICE_META = "variant_notice"
#: The injection source of that notice.
CLOSED_SOURCE = "variant_closed"
#: ``metadata.variant_resolution`` of a drafts question a new turn superseded.
SUPERSEDED = "superseded"
_REASONS = {
    "superseded": "variant_pick_superseded",
    "cancelled": "variant_pick_cancelled",
    "expired": "variant_pick_expired",
}
#: One close at a time per process: the cancel route, the deadline timer and a turn's
#: start may race on the same run; the first records it, the others see it closed.
_LOCK = threading.Lock()


def latest_question(app: Any, variants_id: str) -> Any:
    """The newest question asked for run ``variants_id`` (``None``: none surfaced)."""
    rows = [
        q
        for q in getattr(app.state, "user_questions", {}).values()
        if (q.metadata or {}).get("variants_id") == variants_id
    ]
    return max(rows, key=lambda q: str(q.created_at or "")) if rows else None


def _pointer(app: Any, sid: str) -> Mapping[str, Any] | None:
    row = app.state.sessions.get(sid)
    pending = (getattr(row, "metadata", None) or {}).get(PENDING_META)
    if isinstance(pending, Mapping) and pending.get("variants_id"):
        return pending
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def close_run(
    app: Any,
    sid: str,
    variants_id: str,
    status: str,
    *,
    question_id: str = "",
    superseded_by: str = "",
) -> VariantRun | None:
    """Close run ``variants_id`` of session ``sid`` without a pick, as ``status``.

    Only a run awaiting its pick closes (``None`` otherwise: it was picked or closed
    already). The pointer is cleared and the notice left either way the run names it.
    """
    if status not in CLOSED_STATUSES:
        raise ValueError(f"not a closing status: {status!r}")
    with _LOCK:
        run = load_run(app, sid, variants_id)
        if run.status != "awaiting_pick":
            closed = None
        else:
            run.status = status  # type: ignore[assignment]
            run.closed_reason = _REASONS[status]
            run.closed_at = _now()
            run.question_id = question_id or run.question_id
            run.superseded_by_message_id = superseded_by
            save_run(app, run)
            closed = run
    if closed is not None:
        emit_closed(app, sid, closed)
        logger.info(
            "variant.closed session=%s variants_id=%s status=%s reason=%s",
            sid,
            variants_id,
            status,
            closed.closed_reason,
        )
    _release(app, sid, variants_id, question_id, closed)
    return closed


def _release(
    app: Any, sid: str, variants_id: str, question_id: str, closed: VariantRun | None
) -> None:
    """Clear the session's pointer at ``variants_id`` (and its surfaced question)."""
    from clio_agent.gact.ask_user_tool import PENDING_ASK_USER_META  # noqa: PLC0415

    row = app.state.sessions.get(sid)
    metadata = (getattr(row, "metadata", None) or {}) if row is not None else {}
    patch: dict[str, Any] = {}
    pointer = metadata.get(PENDING_META)
    if isinstance(pointer, Mapping) and pointer.get("variants_id") == variants_id:
        patch[PENDING_META] = {}
    asked = metadata.get(PENDING_ASK_USER_META)
    if (
        question_id
        and isinstance(asked, Mapping)
        and asked.get("question_id") == question_id
        and not asked.get("resolved_status")
    ):
        question = app.state.user_questions.get(question_id)
        patch[PENDING_ASK_USER_META] = {
            **asked,
            "resolved_status": str(getattr(question, "status", "") or "cancelled"),
            "resolved_at": _now(),
        }
    if closed is not None:
        patch[NOTICE_META] = {
            "variants_id": closed.variants_id,
            "status": closed.status,
            "drafts": sum(1 for t in closed.tries if t.status == "completed"),
        }
    if patch:
        app.state.sessions.update(sid, metadata_patch=patch)


def close_for_question(app: Any, question: Any) -> VariantRun | None:
    """Close the run a cancelled or expired drafts question was asked for."""
    metadata = getattr(question, "metadata", None) or {}
    variants_id = str(metadata.get("variants_id") or "")
    if not variants_id or question.status not in {"cancelled", "expired"}:
        return None
    resolution = str(metadata.get("variant_resolution") or "")
    return close_run(
        app,
        question.session_id,
        variants_id,
        SUPERSEDED if resolution == SUPERSEDED else str(question.status),
        question_id=question.id,
        superseded_by=str(metadata.get("superseded_by_message_id") or ""),
    )


def close_after_question(app: Any, question: Any) -> None:
    """:func:`close_for_question`, off the server loop when called on it (a route).

    The close writes clio-core; a loop thread never waits on it. A failure is audited
    and logged (typed), and the session's next turn completes the close.
    """
    if not (getattr(question, "metadata", None) or {}).get("variants_id"):
        return
    from clio_agent.gact.off_loop import schedule_off_loop  # noqa: PLC0415

    schedule_off_loop(lambda: close_for_question(app, question), label="variant.close")


def _supersede(app: Any, sid: str, variants_id: str, message_id: str) -> None:
    """A new turn started while run ``variants_id`` waits on its pick: close it."""
    from clio_agent.gact.elicitation_bridge import claim_question_transition  # noqa: PLC0415
    from clio_agent.gact.events import Event  # noqa: PLC0415

    run = load_run(app, sid, variants_id)
    if run.status != "awaiting_pick":
        if run.status in CLOSED_STATUSES or run.status in {"selected", "failed"}:
            _release(app, sid, variants_id, "", None)  # closed elsewhere: a stale pointer
        return
    question = latest_question(app, variants_id)
    if question is not None and question.status == "pending":
        claimed = claim_question_transition(
            app,
            question.id,
            "cancelled",
            metadata_patch={
                "variant_resolution": SUPERSEDED,
                "superseded_by_message_id": message_id,
            },
        )
        if claimed is not None:
            still = sorted(
                (
                    q
                    for q in app.state.user_questions.values()
                    if q.session_id == claimed.session_id
                    and q.status == "pending"
                    and q.response_mode == "blocking"
                ),
                key=lambda q: str(q.created_at or ""),
            )
            app.state.sessions.update(
                claimed.session_id,
                metadata_patch={"pending_user_question_id": still[0].id if still else ""},
            )
            app.state.bus.publish(
                Event(
                    type="user_question.cancelled",
                    session_id=claimed.session_id,
                    payload=claimed.model_dump(exclude_none=True),
                )
            )
        question = app.state.user_questions.get(question.id)
    if question is None:
        # The drafting turn ended before its question surfaced: nothing can pick now.
        close_run(app, sid, variants_id, SUPERSEDED, superseded_by=message_id)
    elif question.status in {"cancelled", "expired"}:
        close_for_question(app, question)
    # answered: the answer resumes the run at the agent's forward.


def _notice_text(notice: Mapping[str, Any]) -> str:
    drafts = int(notice.get("drafts") or 0)
    what = f"The {drafts} draft(s) shown to the user (run {notice.get('variants_id')})"
    status = str(notice.get("status") or "")
    why = {
        "superseded": "were never picked: the user sent a new message instead",
        "cancelled": "were dismissed by the user without a pick",
        "expired": "were never picked: the pick expired",
    }.get(status, f"were closed ({status})")
    return f"{what} {why}. None of them is part of the conversation."


def turn_start_notices(app: Any, sid: str, *, message_id: str) -> list[tuple[str, str]]:
    """At a turn's start: supersede the session's pending pick, and take any notice.

    Returns the injections the turn's agent is told (``[(source, text)]``).
    """
    pointer = _pointer(app, sid)
    if pointer is not None:
        _supersede(app, sid, str(pointer["variants_id"]), message_id)
    row = app.state.sessions.get(sid)
    notice = (getattr(row, "metadata", None) or {}).get(NOTICE_META)
    if not isinstance(notice, Mapping) or not notice.get("variants_id"):
        return []
    app.state.sessions.update(sid, metadata_patch={NOTICE_META: {}})
    return [(CLOSED_SOURCE, _notice_text(notice))]
