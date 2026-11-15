"""Human task controls with owner effects independent of notification admission."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from clio_agent.arc.task_types import MailItem, TaskSnapshot
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_publication import task_owner_recipient

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from clio_agent.gact.task_history import TaskHistory


def _record_intervention(
    history: TaskHistory,
    current: TaskSnapshot,
    intent: dict[str, Any],
    phase: str,
    effect: dict[str, Any] | None = None,
) -> None:
    with history.serialized():
        history.record_intervention(
            current.owner.session_id,
            intent["id"] + ":" + phase,
            {**intent, "phase": phase, "effect": effect},
        )
        if phase != "effect":
            return
        recipient = task_owner_recipient(history, current)
        if recipient is None:
            return
        content = history.write_content(recipient, {**intent, "effect": effect})
        history.record_mail(
            MailItem(
                id=intent["id"] + ":parent",
                recipient_session=recipient,
                sender_human=intent["actor_human"],
                kind="control",
                content=content,
                created_at=intent["requested_at"],
                handle=current.handle,
            )
        )


def relay_confirmed_human_input(history: TaskHistory, item: MailItem) -> None:
    """Notify the owning parent of accepted human steering, without sending it twice.

    Use the assignment pinned in confirmed mail, including on acceptance replay.
    A later successor never becomes the target of the original intervention.
    """
    if item.kind not in {"human", "composer"} or item.sender_human is None:
        return
    with history.serialized():
        if history.mail_item(item.recipient_session, item.id) != item:
            raise ValueError("Human steering must already have confirmed Core acceptance")
        body = history.read_content(item.content)
        if not isinstance(body, dict):
            return
        handle = body.get("assignment_id")
        if handle is None:
            return  # Legacy acceptance has no proven assignment to attribute.
        if not isinstance(handle, str):
            raise ValueError("Human steering assignment identity is invalid")
        current = history.task_snapshot(handle)
        if (
            current is None
            or current.kind not in {"Session", "Subagent"}
            or current.owner.session_id != item.recipient_session
        ):
            raise ValueError("Human steering assignment identity is unavailable")
        recipient = task_owner_recipient(history, current)
        identity = "intervention:message:" + item.id
        if recipient is None or history.mail_item(recipient, identity + ":parent") is not None:
            return
        intent = {
            "id": identity,
            "handle": current.handle,
            "action": "message",
            "actor_human": item.sender_human,
            "requested_at": item.created_at,
            "message": body.get("text", ""),
        }
        _record_intervention(history, current, intent, "intent")
        _record_intervention(
            history, current, intent, "effect", {"accepted": True, "message_id": item.id}
        )


async def intervene(
    app: Any,
    handle: str,
    action: str,
    *,
    message: str = "",
    reason: str = "",
    stop_reservation: threading.Event | None = None,
    stop_reservation_captured: bool = False,
) -> dict[str, Any]:
    """Apply an authenticated human action and queue its attributed parent notice."""
    history = app.state.task_history
    caller = TaskCaller("", app.state.human_principal, human_action=True)
    current = TaskAccess(history, caller).require(handle, "control")
    if action == "stop" and not stop_reservation_captured:
        stop_reservation = app.state.turn_runner.request_stop(current.owner.session_id)
    identity, stamp = "intervention_" + uuid4().hex, now()
    intent = {
        "id": identity,
        "handle": handle,
        "action": action,
        "actor_human": caller.human_id,
        "requested_at": stamp,
        "message": message,
        # Why the person acted, when an operation names it (``conversation_stop``).
        **({"reason": reason} if reason else {}),
    }

    def record(phase: str, effect: dict[str, Any] | None = None) -> None:
        _record_intervention(history, current, intent, phase, effect)

    critical = action in {"pause", "cancel", "stop"}
    if not critical:
        # New steering/resume/restart work requires confirmed durable intent.
        await history.io.run(record, "intent")

    if action == "cancel":
        named = replace(caller, intent=reason) if reason else caller
        effect = (await app.state.task_operations.cancel(named, handle))["outcomes"][0]
    elif action in {"pause", "resume", "restart"}:
        effect = await getattr(app.state.conversations, action)(caller, handle)
    elif action == "stop":
        sid = current.owner.session_id
        reservation = stop_reservation
        stopping = reservation is not None

        async def stop() -> None:
            if reservation is not None:
                await app.state.turn_runner.stop(sid, reservation=reservation)

        app.state.conversations.retain_control(identity, asyncio.create_task(stop()))
        busy = app.state.turn_runner.busy(sid)
        still_stopping = busy and app.state.turn_runner.cancellation(sid) is reservation
        effect = {
            "handle": handle,
            "turn_stop_requested": stopping,
            "status": "stopping" if still_stopping else "running" if busy else "idle",
            "accepted_tasks_continue": True,
        }
    elif action == "message":

        def steer() -> dict[str, Any]:
            content = history.write_content(current.owner.session_id, message)
            accepted = history.record_mail(
                MailItem(
                    id=identity + ":steer",
                    recipient_session=current.owner.session_id,
                    sender_human=caller.human_id,
                    kind="human",
                    content=content,
                    created_at=stamp,
                    handle=handle,
                )
            )
            return {"message_id": accepted.id, "accepted": True}

        effect = await history.io.run(steer)
    else:
        raise ValueError("Unknown human task control")

    if critical:

        def reconcile_control() -> None:
            record("intent")
            record("effect", effect)

        app.state.task_recovery.defer_recording(identity, reconcile_control)
        return {**effect, "intervention_id": identity, "recording": "unconfirmed"}

    async def confirm() -> None:
        await history.io.run(record, "effect", effect)

    recording = "confirmed"
    try:
        await confirm()
    except Exception as error:
        logger.exception(
            "Task boundary reason=human_control_recording_unconfirmed error_type=%s",
            type(error).__name__,
        )
        recording = "unconfirmed"
    if recording == "unconfirmed":

        async def reconcile() -> None:
            await history.io.run(record, "intent")
            await confirm()

        app.state.task_recovery.defer(identity, reconcile)
    return {**effect, "intervention_id": identity, "recording": recording}
