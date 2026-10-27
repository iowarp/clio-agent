"""Native-owner cancellation and physical settlement algorithms; no independent driver."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.task_types import (
    SubmissionContext,
    TaskSnapshot,
)
from clio_agent.gact.native_identity import local_runtime_identity
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.tools.task_client import (
    AcceptedNativeCall,
)
from clio_agent.tools.task_receipt import validate_terminal

if TYPE_CHECKING:
    from clio_agent.gact.native_tools import NativeTools


logger = logging.getLogger(__name__)


async def _stop_foreground(self: NativeTools, handle: str) -> None:
    async def settle() -> None:
        await self.cancel(handle)
        await self.await_settlement(handle, allow_uncertainty=False)

    cleanup = asyncio.create_task(settle())
    while True:
        try:
            await asyncio.shield(cleanup)
            return
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise RuntimeError("Foreground physical-settlement owner was cancelled") from None


def _settle(self: NativeTools, current: TaskSnapshot, accepted: AcceptedNativeCall | None) -> None:
    with self.history.serialized():
        current = self._current(current.handle)
        if current.terminal_at is not None:
            publish_task_notice(self.history, current)
            return
        attempts = self.history.native_attempts(current.handle)
        context: SubmissionContext | None = None
        if (
            current.submission is not None
            and current.backend_observation is not None
            and current.backend is not None
        ):
            context = SubmissionContext.model_validate(
                self.history.read_content(current.submission)
            )
            continuation = context.provider_continuation
            raw = self.history.read_content(current.backend_observation)
            current_leg = [
                attempt
                for attempt in attempts
                if attempt.backend.task_id == current.backend.task_id
            ]
            if (
                continuation is not None
                and isinstance(raw, dict)
                and raw.get("status") in {"completed", "failed", "cancelled"}
                and all(attempt.finished_at is not None for attempt in current_leg)
            ):
                for attempt in attempts:
                    if (
                        attempt.incarnation in continuation.previous_incarnations
                        and attempt.cleanup_state == "pending"
                    ):
                        self.history.record_attempt(
                            attempt.model_copy(update={"cleanup_state": "unknown"})
                        )
                attempts = self.history.native_attempts(current.handle)
        if any(attempt.cleanup_state == "unknown" for attempt in attempts) and (
            current.status != "task_state_uncertain" or current.attention_episode is None
        ):
            self._uncertain(current.handle, "Physical settlement is unconfirmed")
            current = self._current(current.handle)
        if current.backend_observation is None:
            return
        reply = ClientGetTaskResult.model_validate(
            self.history.read_content(current.backend_observation)
        )
        if reply.status not in {"completed", "failed", "cancelled"}:
            return
        status = reply.status
        if accepted is None:
            if current.submission is None:
                return
            context = SubmissionContext.model_validate(
                self.history.read_content(current.submission)
            )
            if current.backend != context.backend.model_copy(update={"task_id": reply.task_id}):
                raise ValueError("Late native result differs from original submission custody")
            schema = context.output_schema
        else:
            schema = accepted.submission.output_schema
        validation_problem = ""
        try:
            validate_terminal(schema, reply)
        except ValueError as error:
            validation_problem = "Backend result contract failure: " + str(error)
            status = "failed"
            current = current.model_copy(
                update={
                    "progress": current.progress.model_copy(update={"message": validation_problem})
                }
            )
        if any(attempt.cleanup_state in {"pending", "unknown"} for attempt in attempts):
            # Preserve the captured-contract result without using a fresh receipt
            # as proof for another incarnation. Explicit reads remain nonfinal.
            if validation_problem:
                self._uncertain(
                    current.handle, "Physical settlement is unconfirmed. " + validation_problem
                )
            elif (
                context is not None
                and (
                    current.status == "task_state_uncertain"
                    or current.attention_episode is not None
                )
                and current.connection_freshness == "fresh"
                and context.local_runtime == local_runtime_identity()
                and current.backend == context.backend.model_copy(update={"task_id": reply.task_id})
                and (
                    accepted is None
                    or (
                        current.submission == accepted.submission.context
                        and reply.task_id == accepted.task.task_id
                        and schema == context.output_schema
                    )
                )
                and any(attempt.cleanup_state == "pending" for attempt in attempts)
                and all(
                    attempt.cleanup_state in {"confirmed", "never_started"}
                    or (
                        attempt.cleanup_state == "pending"
                        and attempt.backend == current.backend
                        and attempt.cleanup_receipt is None
                        and attempt.finished_at is None
                        and (
                            context.provider_continuation is None
                            or attempt.incarnation
                            not in context.provider_continuation.previous_incarnations
                        )
                    )
                    for attempt in attempts
                )
            ):
                # Fresh matching observation resolves lost visibility. The
                # same-runtime worker still owns every unresolved incarnation.
                # Keep its raw terminal reply nonfinal until cleanup receipts arrive.
                progressing = current.model_copy(
                    update={
                        "status": "running",
                        "activity": "cancelling"
                        if reply.status == "cancelled" or current.cancellation_requested_at
                        else "executing",
                        "settlement": "cleanup_pending",
                        "attention_episode": None,
                        "progress": current.progress.model_copy(
                            update={"message": "Required cleanup is in progress."}
                        ),
                        "updated_at": now(),
                    }
                )
                self.history.record_snapshot(progressing, observation_id=uuid4().hex)
            return
        if reply.result is not None:
            result = reply.result
            structured = result.get("structuredContent") or {}
            if isinstance(structured, dict) and structured.get("state") == "cancelled":
                status = "cancelled"
            elif result.get("isError") or (
                isinstance(structured, dict)
                and (
                    structured.get("error")
                    or structured.get("timed_out")
                    or structured.get("state") in {"failed", "interrupted", "cancelled"}
                    or structured.get("exit_code") not in {None, 0}
                )
            ):
                status = "failed"
        withdrawn = (
            current.cancellation_requested_at is not None or current.handle in self._control_holds
        ) and not attempts
        if withdrawn:
            status = "cancelled"
        stamp = now()
        settled = current.model_copy(
            update={
                "status": status,
                "activity": "",
                "settlement": "never_started" if withdrawn or not attempts else "confirmed",
                "terminal_at": stamp,
                "updated_at": stamp,
                "result": current.backend_observation,
                "supported_actions": ("get_result",),
            }
        )
        self.history.record_snapshot(settled, observation_id=uuid4().hex)
        publish_task_notice(self.history, settled)


async def cancel(
    self: NativeTools, handle: str, *, caller: TaskCaller | None = None
) -> dict[str, Any]:
    """Close owned admission before native cancellation; acknowledgement is not settlement."""
    with self.history.read_view():
        current = (
            TaskAccess(self.history, caller).require(handle, "control")
            if caller is not None
            else self._current(handle)
        )
        if current.terminal_at is None:
            self._control_holds.add(handle)
    if current.terminal_at is not None:
        return {"handle": handle, "cancellation_requested": False, "status": current.status}
    requested_at = current.cancellation_requested_at or now()

    def intent() -> None:
        with self.history.serialized():
            current = self._current(handle)
            if current.cancellation_requested_at is None:
                self.history.record_snapshot(
                    current.model_copy(
                        update={
                            "cancellation_requested_at": requested_at,
                            "activity": "cancelling"
                            if current.terminal_at is None
                            else current.activity,
                            "updated_at": now(),
                        }
                    ),
                    observation_id=uuid4().hex,
                )

    # The verified owner closes admission above and sends cancellation below.
    # A pending Core RPC cannot become a prerequisite for either operation.
    self.recovery.defer_recording("native-cancel:" + handle, intent)
    provider_owned = False
    if current.kind == "Download" and self.provider_cancel is not None:
        try:
            provider_owned = await self.provider_cancel(current)
        except Exception as error:
            logger.exception(
                "Task boundary reason=provider_cancellation_failed error_type=%s",
                type(error).__name__,
            )

            async def retry_provider_cancel() -> None:
                if not self._resources_closed:
                    await self.cancel(handle)

            self.recovery.defer("provider-cancel-delivery:" + handle, retry_provider_cancel)
            reason = str(error)
            self.recovery.defer_recording(
                "provider-cancel-attention:" + handle,
                lambda: self._uncertain(
                    handle, "Provider cancellation delivery is unconfirmed: " + reason
                ),
            )
            return {
                "handle": handle,
                "cancellation_requested": True,
                "settlement": "unknown",
                "recording": "unconfirmed",
            }
    if provider_owned:
        live_leg = handle in self._calls
        recover_provider = self.provider_recovery
        if not live_leg and recover_provider is not None:

            async def reconnect_cancel() -> bool:
                latest = await self.history.io.run(self._current, handle)
                if latest.terminal_at is not None or handle in self._calls:
                    return True
                if latest.submission is None:
                    await self._record_or_defer(
                        "provider-cancel-attention:" + handle,
                        lambda: self._uncertain(
                            handle,
                            "Original provider submission context is unavailable; inspect the retained job identity.",
                        ),
                    )
                    return True
                context = SubmissionContext.model_validate(
                    await self.history.io.run(self.history.read_content, latest.submission)
                )
                if not await recover_provider(latest, context):
                    await self._record_or_defer(
                        "provider-cancel-attention:" + handle,
                        lambda: self._uncertain(
                            handle,
                            "The existing provider owner cannot safely reattach this job; inspect its original identity.",
                        ),
                    )
                return True

            self.recovery.defer("provider-cancel-reconnect:" + handle, reconnect_cancel)
        return {
            "handle": handle,
            "cancellation_requested": True,
            "settlement": "cleanup_pending" if live_leg else "unknown",
            "recording": "unconfirmed"
            if "native-cancel:" + handle in self.recovery.pending
            else "confirmed",
        }
    accepted = self._calls.get(handle)
    if accepted is None:
        self.recovery.defer(
            "native-cancel-delivery:" + handle, partial(self._deliver_cancel, handle)
        )
        self.recovery.defer_recording(
            "native-cancel-unknown:" + handle,
            lambda: self._uncertain(
                handle,
                "Cancellation admission is closed; physical settlement is unconfirmed."
                if current.backend is not None and current.backend.task_id
                else "Cancellation admission is closed, but backend acceptance remains uncertain.",
            ),
        )
        return {
            "handle": handle,
            "cancellation_requested": True,
            "settlement": "unknown",
            "recording": "unconfirmed",
        }

    try:
        if not await self._deliver_cancel(handle):
            self.recovery.defer(
                "native-cancel-delivery:" + handle, partial(self._deliver_cancel, handle)
            )
    except Exception as error:
        logger.exception(
            "Task boundary reason=native_cancellation_failed error_type=%s", type(error).__name__
        )
        self.recovery.defer(
            "native-cancel-delivery:" + handle, partial(self._deliver_cancel, handle)
        )
        reason = str(error)
        self.recovery.defer_recording(
            "native-cancel-attention:" + handle,
            lambda: self._uncertain(handle, "Cancellation delivery is unconfirmed: " + reason),
        )
    return {
        "handle": handle,
        "cancellation_requested": True,
        "recording": "unconfirmed"
        if "native-cancel:" + handle in self.recovery.pending
        else "confirmed",
    }


async def _deliver_cancel(self: NativeTools, handle: str) -> bool:
    """Retry cancellation only against this submission's adopted native identity."""
    if self._resources_closed:
        return True  # durable intent survives; closed RPC resources cannot deliver it
    current = self._current(handle)
    if current.terminal_at is not None:
        return True
    if (
        current.raw_status in {"completed", "failed", "cancelled"}
        and current.backend_observation is not None
    ):
        return True  # delivery is obsolete; cleanup and attention remain independently unresolved
    bound = self._calls.get(handle)
    if bound is None or current.submission != bound.submission.context:
        return current.cancellation_requested_at is not None
    if current.backend is not None and current.backend.task_id != bound.task.task_id:
        return False
    await bound.request_cancel()
    return True


async def await_settlement(
    self: NativeTools, handle: str, *, allow_uncertainty: bool = True
) -> TaskSnapshot:
    """Wait on confirmed owner changes; caller cancellation cannot stop native work."""
    changed, loop = asyncio.Event(), asyncio.get_running_loop()

    def wake(_revision: int) -> None:
        loop.call_soon_threadsafe(changed.set)

    remove = self.history.listen(wake, handles=(handle,))
    try:
        while True:
            changed.clear()
            current = await self.history.io.run(self._current, handle)
            if current.terminal_at is not None:
                return current
            if not allow_uncertainty and current.cancellation_requested_at is not None:
                attempts = await self.history.io.run(self.history.native_attempts, handle)
                if attempts and all(
                    attempt.cleanup_state in {"confirmed", "never_started"} for attempt in attempts
                ):
                    return (
                        current  # physical settlement can be known while the result stays uncertain
                    )
            if allow_uncertainty and current.status == "task_state_uncertain":
                return current
            await changed.wait()
    finally:
        remove()


async def settle_shutdown(self: NativeTools) -> tuple[BaseException, ...]:
    """Confirm physical retirement while native transports and Core remain available."""
    if self._resources_closed:
        return ()
    await self.request_shutdown()
    # Resolve outstanding submissions before cancelling their verified identities.
    if self._rpc:
        await asyncio.shield(asyncio.gather(*tuple(self._rpc.values()), return_exceptions=True))
    await self.request_shutdown()
    while self._calls:
        await self.recovery.drain()
        for handle in tuple(self._calls):
            current = await self.history.io.run(self._current, handle)
            remote = self.provider_shutdown is not None and await self.provider_shutdown(current)
            driver = self._drivers.get(handle)
            if remote and driver is not None:
                await asyncio.shield(driver)
                await self.recovery.drain()
            if not remote or not await self.history.io.run(provider_locally_retired, self, handle):
                await self.await_settlement(handle, allow_uncertainty=False)
            if driver is not None:
                await asyncio.shield(driver)
            await self._retire_resources(handle)
    self._resources_closed = True
    for driver in tuple(self._drivers.values()):
        if not driver.done():
            driver.cancel()  # observation only; owned effects/cleanup have settled above
    if self._drivers:
        await asyncio.shield(asyncio.gather(*tuple(self._drivers.values()), return_exceptions=True))
    results = (
        await asyncio.shield(asyncio.gather(*tuple(self._retiring), return_exceptions=True))
        if self._retiring
        else ()
    )
    return tuple(result for result in results if isinstance(result, BaseException))


async def request_shutdown(self: NativeTools) -> None:
    """Close admission and request local cancellation while transports stay available."""
    self._closing = True

    def reserved() -> set[str]:
        # Only work this process took custody of. Work it never reattached stays open
        # in clio-core and is recovered at the next start, as after a crash.
        with self.history.read_view():
            return {
                handle
                for handle in self._custody - set(self._calls) - set(self._rpc)
                if (task := self.history.task_snapshot(handle)) is not None
                and task.terminal_at is None
                and task.kind in {"Shell", "Download", "Indexing"}
                and task.submission is not None
                and (
                    task.backend is None
                    or any(
                        task.backend.owner == "native"
                        and task.backend.server == root.server
                        and task.backend.session == root.session
                        and task.backend.queue == root.queue
                        for root in self.backends.values()
                    )
                )
            }

    handles = set(self._calls) | set(self._rpc) | await self.history.io.run(reserved)

    async def request(handle: str) -> None:
        try:
            current = self._current(handle)
            if self.provider_shutdown is not None and await self.provider_shutdown(current):
                if current.cancellation_requested_at is None:
                    return  # workload retires its local observation cooperatively
            await self.cancel(handle)
        except Exception as error:
            logger.exception(
                "Task boundary reason=native_shutdown_settlement_failed error_type=%s",
                type(error).__name__,
            )
            await self._record_or_defer(
                "native-shutdown:" + handle, partial(self._uncertain, handle, str(error))
            )

    await asyncio.gather(*(request(handle) for handle in handles))


def provider_locally_retired(self: NativeTools, handle: str) -> bool:
    """Require incarnation-specific joined-observer receipts, never generic uncertainty."""
    with self.history.serialized():
        current = self.history.task_snapshot(handle)
        if current is None or current.kind != "Download" or current.backend is None:
            return False
        attempts = [
            attempt
            for attempt in self.history.native_attempts(handle)
            if attempt.backend == current.backend
        ]
        if not attempts:
            return False
        for attempt in attempts:
            if attempt.finished_at is None or attempt.cleanup_receipt is None:
                return False
            receipt = self.history.read_content(attempt.cleanup_receipt)
            if not isinstance(receipt, dict):
                return False
            detail = receipt.get("local_retirement")
            if (
                not isinstance(detail, dict)
                or detail.get("kind") != "provider_observer"
                or detail.get("local_work_joined") is not True
                or detail.get("operation_id") != handle
                or current.resource_id is not None
                and detail.get("source_id") != current.resource_id
                or receipt.get("incarnation") != attempt.incarnation
                or receipt.get("execution_key") != attempt.execution_key
            ):
                return False
        return True
