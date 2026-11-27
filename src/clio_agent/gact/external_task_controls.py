"""Cancellation RPC delivery for the existing external SDK owner."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from clio_agent.gact import external_plain_calls
from clio_agent.gact.external_task_inputs import close_input_questions
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller

if TYPE_CHECKING:
    from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks
    from clio_agent.tools.task_client import AcceptedNativeCall

logger = logging.getLogger(__name__)


async def cancel(
    owner: ExternalTasks, handle: str, *, caller: TaskCaller | None = None
) -> dict[str, Any]:
    """Control the verified SDK owner without making Core recording a prerequisite."""
    with owner.history.read_view():
        current = (
            TaskAccess(owner.history, caller).require(handle, "control")
            if caller is not None
            else owner.history.task_snapshot(handle)
        )
    if current is None:
        raise ValueError("External task is unavailable")
    if current.terminal_at is not None:
        return {"handle": handle, "cancellation_requested": False, "status": current.status}
    requested_at = current.cancellation_requested_at or now()

    def intent() -> None:
        with owner.history.serialized():
            selected = owner.history.task_snapshot(handle)
            if selected is None:
                raise ValueError("External task is unavailable")
            if selected.cancellation_requested_at is None:
                owner.history.record_snapshot(
                    selected.model_copy(
                        update={
                            "cancellation_requested_at": requested_at,
                            "activity": "cancelling"
                            if selected.terminal_at is None
                            else selected.activity,
                            "updated_at": now(),
                        }
                    ),
                    observation_id=uuid4().hex,
                )

    owner.recovery.defer_recording("external-cancel:" + handle, intent)
    for host in owner.connections.values():
        call = next((row for row in host.calls.values() if row.submission.handle == handle), None)
        if call is None:
            continue
        bound_call: AcceptedNativeCall = call

        async def deliver(
            bound: AcceptedNativeCall = bound_call, connection: ExternalConnection = host
        ) -> bool:
            with owner.history.read_view():
                latest = (
                    TaskAccess(owner.history, caller).require(handle, "control")
                    if caller is not None
                    else owner.history.task_snapshot(handle)
                )
            if (
                owner._closing
                or connection.calls.get(bound.task.task_id) is not bound
                or latest is not None
                and (
                    latest.terminal_at is not None
                    or latest.raw_status in {"completed", "failed", "cancelled"}
                )
            ):
                return False  # adoption arms delivery; old RPC custody ends without an ACK
            if latest is None or latest.backend != connection.backend.model_copy(
                update={"task_id": bound.task.task_id}
            ):
                raise ValueError("External cancellation differs from its original SDK identity")

            def close_inputs() -> None:
                with owner.history.serialized():
                    current = owner.history.task_snapshot(handle)
                    if current is None or current.backend != latest.backend:
                        raise ValueError("Input cancellation differs from its accepted owner")
                    close_input_questions(owner, current)

            # This owner action is admitted only after the final rights/identity
            # check above. Later revocation cannot make recording an already
            # accepted effect retry forever. Core recovery still precedes the
            # Question write, without delaying the native cancellation RPC.
            owner.recovery.defer_recording("external-cancel-inputs:" + handle, close_inputs)
            await bound.request_cancel()
            return True

        async def retry_delivery() -> None:
            await deliver()

        try:
            acknowledged = await deliver()
        except Exception as error:
            logger.exception(
                "Task boundary reason=external_cancel_delivery_unconfirmed error_type=%s",
                type(error).__name__,
            )
            owner.recovery.defer("external-cancel-delivery:" + handle, retry_delivery)
            owner.recovery.defer_recording(
                "external-cancel-attention:" + handle,
                partial(
                    owner._attention, handle, str(error), lost=False, episode="cancel:" + handle
                ),
            )
            return {
                "handle": handle,
                "cancellation_requested": True,
                "settlement": "unknown",
                "recording": "unconfirmed",
            }
        return {
            "handle": handle,
            "cancellation_requested": True,
            "settlement": "external_acknowledged" if acknowledged else "unknown",
            "recording": "unconfirmed"
            if owner.recovery.is_pending("external-cancel:" + handle)
            or owner.recovery.is_pending("external-cancel-inputs:" + handle)
            else "confirmed",
        }
    plain = await external_plain_calls.cancel(owner, handle)  # a call with no MCP task
    if plain is not None:
        return plain
    return {
        "handle": handle,
        "cancellation_requested": True,
        "settlement": "unknown",
        "recording": "unconfirmed",
        "error": "Original task transport is unavailable",
    }
