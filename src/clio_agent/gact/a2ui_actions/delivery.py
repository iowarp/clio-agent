"""The agent-lane delivery matrix, shared by ordinary actions and errors (S5).

Split out of ``dispatcher.py`` (rather than inlined there) because
``client_state.py``'s ``VALIDATION_FAILED`` repair delivery needs the exact
SAME idle/running/waiting_user routing an ordinary ``destination: "agent"``
action gets — a repair is "one more agent-bound event," not a special case.
Sharing this module avoids a dispatcher<->client_state import cycle (the
dispatcher routes an ``error`` envelope to ``client_state`` BEFORE the
surface lookup; ``client_state`` in turn needs this routing to actually
deliver its repair).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from clio_agent.gact.a2ui_actions.record import ActionRecord, persist_transition

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.interaction_types import UserQuestion


def _correlated_pending_question(
    app: "FastAPI", session_id: str, surface_id: str, context: Mapping[str, Any]
) -> "UserQuestion | None":
    """Resolve the ONE pending question this action/error resumes, if any.

    ``context.question_id`` is an explicit caller intent: when given, it MUST
    resolve to a pending question in this session, or the caller is
    uncorrelated (no silent fallback to a surface-tag match). Otherwise the
    surface's own tag (``question.metadata["a2ui_surface_id"]``, stamped by
    the ``ask_user`` tool's optional ``surface_id`` argument) is used.
    """

    question_id = str(context.get("question_id") or "")
    for question in app.state.user_questions.values():
        if question.session_id != session_id or question.status != "pending":
            continue
        if question_id:
            if question.id == question_id:
                return question
            continue
        tagged = str((question.metadata or {}).get("a2ui_surface_id") or "")
        if tagged and tagged == surface_id:
            return question
    return None


def _publish(app: "FastAPI", session_id: str, record: ActionRecord) -> None:
    from clio_agent.gact.a2ui_actions.record import lifecycle_event_payload  # noqa: PLC0415
    from clio_agent.gact.events import Event  # noqa: PLC0415

    app.state.bus.publish(
        Event(
            type=f"a2ui.action.{record.state}",
            session_id=session_id,
            payload=lifecycle_event_payload(record),
        )
    )


async def deliver_to_agent(
    app: "FastAPI",
    session_id: str,
    record: ActionRecord,
    *,
    narration: str,
    context: Mapping[str, Any],
) -> ActionRecord:
    """Deliver ``record`` (``state="received"``) through the agent lane.

    ``context`` is the structured object stamped onto
    ``metadata["a2ui_action_context"]`` — an ordinary action's resolved
    ``context``, or a repair's ``{surface_id, path, message}`` (S5
    deliverable 2b) — the authoritative agent input either way.

    Idle -> a new turn starts (``delivery="start"``). Running -> the
    existing loop-inbox steer carrier (``delivery="steer"``). Waiting on a
    correlated question -> that question is answered and the run resumes
    (``delivery="resolve_question"``). Waiting with no correlated question ->
    ``state="failed"``, ``delivery="rejected"``,
    ``reason="a2ui_waiting_user_uncorrelated"`` (the caller turns this ONE
    reason into an HTTP 409; every other outcome here is a success).

    Returns the next persisted, published snapshot.
    """

    sess = app.state.sessions.get(session_id)
    metadata: dict[str, Any] = {
        "a2ui_action": record.id,
        "surface_id": record.surface_id,
        "a2ui_action_context": dict(context),
    }
    if record.client_data_model is not None:
        metadata["a2ui_client_data_model"] = record.client_data_model

    if sess is not None and sess.status == "waiting_user":
        question = _correlated_pending_question(app, session_id, record.surface_id, context)
        if question is None:
            failed = record.transition(
                state="failed", delivery="rejected", reason="a2ui_waiting_user_uncorrelated"
            )
            persist_transition(app, failed)
            _publish(app, session_id, failed)
            return failed
        from clio_agent.gact.interaction_types import AnswerUserQuestionRequest  # noqa: PLC0415

        answered = await app.state.answer_user_question(
            session_id,
            question.id,
            AnswerUserQuestionRequest(answer=narration, metadata=metadata),
        )
        delivered = record.transition(
            state="delivered",
            delivery="resolve_question",
            correlation={"question_id": answered.id},
        )
        persist_transition(app, delivered)
        _publish(app, session_id, delivered)
        return delivered

    from clio_agent.gact.turn_runner import session_busy_error_payload  # noqa: PLC0415

    busy = session_busy_error_payload(getattr(app.state, "turn_runner", None), session_id)
    if busy is not None:
        from clio_agent.gact.loop_inbox import enqueue_user_steer  # noqa: PLC0415

        enqueue_user_steer(app, session_id, narration, metadata)
        delivered = record.transition(state="delivered", delivery="steer")
        persist_transition(app, delivered)
        _publish(app, session_id, delivered)
        return delivered

    from clio_agent.gact.turn import _start_background_user_turn  # noqa: PLC0415

    # The gate reports idle, so this is a fresh turn: a cancellation aimed at
    # the previous one must not poison it (mirrors the pre-S5 producer).
    app.state.cancel_flags.discard(session_id)
    app.state.cancel_events.pop(session_id, None)
    user_message = _start_background_user_turn(
        app,
        session_id,
        sess,
        narration,
        metadata=metadata,
        prev_status=sess.status if sess is not None else "idle",
    )
    delivered = record.transition(
        state="delivered", delivery="start", correlation={"message_id": user_message.id}
    )
    persist_transition(app, delivered)
    _publish(app, session_id, delivered)
    return delivered


__all__ = ["deliver_to_agent"]
