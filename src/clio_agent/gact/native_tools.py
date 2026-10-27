"""Application-owned native roots, SDK clients and their single result observers.

These resources submit directly to FastMCP/Docket. Core records all observations;
the maps below retain live RPC/transport resources, never a second task registry.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from functools import partial
from types import TracebackType
from typing import Any
from uuid import uuid4

from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp_tasks.client_models import ClientGetTaskResult
from fastmcp_tasks.components import coerce_task_arguments
from mcp.client.session import ClientSession
from pydantic import JsonValue, TypeAdapter

from clio_agent.arc.task_types import (
    BackendReference,
    CoreReference,
    ProviderContinuation,
    SubmissionContext,
    TaskKind,
    TaskOwner,
    TaskProgress,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact import native_task_recovery, native_task_settlement
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.native_identity import local_runtime_identity
from clio_agent.gact.native_shell import register_native_shell
from clio_agent.gact.native_work import SUBMISSION_ARGUMENT, NativeWork, SubmissionAuthorizer, now
from clio_agent.gact.task_access import TaskAccessDenied
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.tools.native_roots import NativeRootKind, build_native_roots
from clio_agent.tools.task_client import (
    AcceptedCallResult,
    AcceptedNativeCall,
    TaskAwareClient,
    TaskSubmission,
    own_native_runtime,
    submission_failure,
)
from clio_agent.tools.task_receipt import AcceptedTaskResult, accepted_result
from clio_agent.tools.task_transport import TaskObservationTransport

logger = logging.getLogger(__name__)
_ARGUMENTS_JSON = TypeAdapter(dict[str, Any])


class NativeTools:
    """Lifespan resources for independent native execution roots on the app loop."""

    def __init__(
        self,
        history: TaskHistory,
        authorize: SubmissionAuthorizer,
        recovery: CoreRecovery,
        *,
        admission: Callable[[TaskSnapshot], None],
    ) -> None:
        self.history, self.authorize = history, authorize
        self.recovery = recovery
        self._control_holds: set[str] = set()
        self.roots = build_native_roots()
        self.backends = {
            kind: BackendReference(
                owner="native",
                server=f"clio-{kind}",
                queue=root.settings.queue,
                transport="in_process",
                session=hashlib.sha256(root.settings.url.encode()).hexdigest(),
            )
            for kind, root in self.roots.items()
        }
        self.work = {
            kind: NativeWork(
                history, self.backends[kind], authorize, self._cleanup, recovery, admission
            )
            for kind in self.roots
        }
        register_native_shell(self.roots["shell"], self.work["shell"])
        self.clients: dict[NativeRootKind, TaskAwareClient] = {}
        self._calls: dict[str, AcceptedNativeCall] = {}
        self._identities: dict[tuple[NativeRootKind, str], AcceptedNativeCall] = {}
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self._rpc: dict[str, asyncio.Task[Any]] = {}
        # Work this process submitted or reattached; only it is cancelled at shutdown.
        self._custody: set[str] = set()
        self._retiring: set[asyncio.Task[None]] = set()
        self._stack = AsyncExitStack()
        self._closing = False
        self._resources_closed = False
        self._reconnect_locks = {kind: asyncio.Lock() for kind in self.roots}
        self.provider_recovery: (
            Callable[[TaskSnapshot, SubmissionContext], Awaitable[bool]] | None
        ) = None
        self.provider_cancel: Callable[[TaskSnapshot], Awaitable[bool]] | None = None
        self.provider_shutdown: Callable[[TaskSnapshot], Awaitable[bool]] | None = None

    def admission_closed(self, handle: str) -> bool:
        """Expose only this owner's provisional cancellation intent to the shared gate."""
        return handle in self._control_holds

    async def __aenter__(self) -> NativeTools:
        """Start both independent SDK/native lifespans on the application event loop."""
        try:
            for kind, root in self.roots.items():

                async def observe(
                    session: ClientSession,
                    task_id: str,
                    reply: ClientGetTaskResult,
                    *,
                    root_kind: NativeRootKind = kind,
                ) -> None:
                    await self._observe(root_kind, task_id, reply)

                async def guard(
                    session: ClientSession, task_id: str, answers: dict[str, Any]
                ) -> None:
                    # Shell does not request input. Storage controls and external MCP
                    # install their identity-bound canonical answer implementation.
                    raise ValueError("Native workload has no pending input request")

                transport = TaskObservationTransport(FastMCPTransport(root.server), observe, guard)

                async def accepted(
                    call: AcceptedNativeCall, *, root_kind: NativeRootKind = kind
                ) -> AcceptedTaskResult:
                    return await self._accepted(root_kind, call)

                client = TaskAwareClient(transport, on_task_accepted=accepted)
                await self._stack.enter_async_context(own_native_runtime(root.server))
                self.clients[kind] = await self._stack.enter_async_context(client)
            return self
        except BaseException as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close admission, settle owned native work, then retire clients and Core hints."""
        errors = list(await self.settle_shutdown())
        try:
            await self._stack.__aexit__(exc_type, exc, traceback)
        except BaseException as error:
            logger.exception("Native resource close failed after physical settlement")
            errors.append(error)
        if errors:
            raise BaseExceptionGroup("Native resource cleanup failed", errors)

    settle_shutdown = native_task_settlement.settle_shutdown

    request_shutdown = native_task_settlement.request_shutdown

    async def public_tools(self, kind: NativeRootKind) -> list[dict[str, Any]]:
        """Expose ordinary arguments only; caller-supplied internal references are refused."""
        result = []
        for tool in await self.clients[kind].list_tools():
            if tool.name == "connected_data_continue":
                continue  # host-only recovery leg; no model-facing submission surface
            row = tool.model_dump(mode="json", by_alias=True)
            schema = copy.deepcopy(row["inputSchema"])
            schema.get("properties", {}).pop(SUBMISSION_ARGUMENT, None)
            schema["required"] = [
                name for name in schema.get("required", []) if name != SUBMISSION_ARGUMENT
            ]
            row["inputSchema"] = schema
            result.append(row)
        return result

    restore = native_task_recovery.restore

    async def submit(
        self,
        root: NativeRootKind,
        tool: str,
        arguments: dict[str, JsonValue],
        *,
        kind: TaskKind,
        owner: TaskOwner,
        invocation_id: str,
        workspace: str,
        permissions: dict[str, JsonValue],
        confinement: dict[str, JsonValue],
        attribution: dict[str, JsonValue],
        execution_limits: dict[str, JsonValue],
        description: str,
        background: bool = True,
    ) -> dict[str, Any]:
        """Confirm custody, submit once, then return acceptance or await the same native work."""
        if self._closing:
            raise ValueError("Native admission is closed")
        if SUBMISSION_ARGUMENT in arguments:
            raise ValueError("Submission references are application-owned")
        if tool == "connected_data_continue":
            raise ValueError("Provider continuations are host-owned, not public submissions")
        component = await self.roots[root].server.get_tool(tool)
        if component is None:
            raise ValueError("Native workload is unavailable")
        if not component.task_config.supports_tasks():
            raise ValueError("Workload is not registered for native task execution")
        reference, handle = uuid4().hex, "task_" + uuid4().hex
        wire = _ARGUMENTS_JSON.dump_python(
            coerce_task_arguments(
                component, {**arguments, SUBMISSION_ARGUMENT: reference}, strict=True
            ),
            mode="json",
            warnings="error",
        )
        public = {key: value for key, value in wire.items() if key != SUBMISSION_ARGUMENT}
        context = SubmissionContext(
            reference=reference,
            handle=handle,
            owner=owner,
            invocation_id=invocation_id,
            tool=tool,
            argument_digest=argument_digest(public),
            arguments=public,
            workspace=workspace,
            permissions=permissions,
            confinement=confinement,
            attribution=attribution,
            execution_limits=execution_limits,
            tool_version=component.version,
            output_schema=component.output_schema,
            backend=self.backends[root],
            local_runtime=await self.history.io.run(local_runtime_identity),
        )
        content = await self.history.io.run(self._confirm, context, kind, description)
        if isinstance(content, TaskSnapshot):
            return {
                "handle": content.handle,
                "kind": content.kind,
                "accepted": True,
                "status": content.status,
                "activity": content.activity,
                "reused": True,
            }
        return await self._invoke(root, context, content, wire, background=background)

    async def _invoke(
        self,
        root: NativeRootKind,
        context: SubmissionContext,
        content: CoreReference,
        wire: dict[str, JsonValue],
        *,
        background: bool,
    ) -> dict[str, Any]:
        """Submit confirmed native custody once, using the same SDK acceptance path."""
        handle, tool = context.handle, context.tool
        binding = TaskSubmission(
            handle=handle,
            tool_name=tool,
            context=content,
            tool_version=context.tool_version,
            output_schema=context.output_schema,
            argument_digest=argument_digest(wire),
        )
        client = self.clients[root]

        async def call() -> Any:
            with client.submission(binding):
                return await client.call_tool(tool, wire, raise_on_error=False)

        rpc = asyncio.create_task(call())
        self._rpc[handle] = rpc
        stopped = False
        try:
            while True:
                try:
                    response = await asyncio.shield(rpc)
                    break
                except asyncio.CancelledError:
                    stopped = True
                    if rpc.cancelled():
                        raise
            if not isinstance(response, AcceptedCallResult):
                raise RuntimeError("Native-required workload returned without acceptance")
            if stopped:
                if not background:
                    await self._stop_foreground(handle)
                raise asyncio.CancelledError
            if background:
                if response.structured_content is None:
                    raise RuntimeError("Accepted native task has no local receipt")
                return response.structured_content
            from clio_agent.runtime.commitment_activity import track

            with track(True, session_id=context.owner.session_id):
                driver = self._drivers.get(handle)
                if driver is not None:
                    await asyncio.shield(driver)
                current = await self.await_settlement(handle)
                return {
                    "handle": handle,
                    "status": current.status,
                    "pending": current.terminal_at is None,
                    "result": await self.history.io.run(self.history.read_content, current.result)
                    if current.result is not None
                    else None,
                }
        except asyncio.CancelledError:
            if not background and handle in self._calls:
                await self._stop_foreground(handle)
            raise
        except Exception as error:
            logger.exception(
                "Task boundary reason=native_submission_failed error_type=%s", type(error).__name__
            )
            # A lost reply is not a safe-to-retry rejection. Preserve the public
            # handle and last confirmed custody even if recording is unavailable.
            reason = str(error)
            failure = submission_failure(error)
            if failure == "rejected" and context.provider_continuation is None:
                await self._record_or_defer(
                    "native-rejected:" + handle, lambda: self._rejected(handle, reason)
                )
                return {
                    "handle": handle,
                    "accepted": False,
                    "acceptance": "rejected",
                    "error": reason,
                    "retry_safe": True,
                }
            await self._record_or_defer(
                "native-uncertain:" + handle,
                lambda: self._uncertain(handle, reason, lost=failure == "uncertain"),
            )
            return {
                "handle": handle,
                "accepted": True if failure == "recording_unconfirmed" else None,
                "acceptance": failure,
                "error": str(error),
                "retry_safe": False,
            }
        finally:
            if rpc.done():
                self._rpc.pop(handle, None)
                await self._retire_resources(handle)

    async def continue_provider(
        self, current: TaskSnapshot, original: SubmissionContext, continuation: ProviderContinuation
    ) -> dict[str, Any]:
        "Create one host-only local leg for verified provider work after process loss."
        return await native_task_recovery.continue_provider(self, current, original, continuation)

    async def _stop_foreground(self, handle: str) -> None:
        return await native_task_settlement._stop_foreground(self, handle)

    def _confirm(self, context: SubmissionContext, kind: TaskKind, description: str) -> Any:
        with self.history.serialized():
            if self._closing:
                raise ValueError("Native admission is closed")
            parent = self.history.task_snapshot(context.owner.assignment_id)
            if (
                parent is None
                or parent.kind not in {"Session", "Subagent"}
                or parent.owner.session_id != context.owner.session_id
                or parent.owner.human_id != context.owner.human_id
                or context.owner.parent_handle != parent.handle
            ):
                raise ValueError("Submission requires its confirmed owning assignment")
            self.work["shell"]._check_admission(parent)
            self.authorize(context)
            resource_id = (
                context.arguments.get("source_id") if kind in {"Download", "Indexing"} else None
            )
            if resource_id is not None and not isinstance(resource_id, str):
                raise ValueError("Storage source identity must be a string")
            deduplication = None
            if kind == "Indexing":
                deduplication = argument_digest(
                    {
                        "tool": context.tool,
                        "arguments": context.arguments,
                        "workspace": context.workspace,
                        "permissions": context.permissions,
                        "source_identity": context.attribution.get("source_identity"),
                    }
                )
                existing = self.history.equivalent_active_task(
                    context.owner.session_id, deduplication
                )
                if existing is not None:
                    return existing
            content = self.history.record_submission(context)
            self._custody.add(context.handle)
            stamp = now()
            self.history.record_snapshot(
                TaskSnapshot(
                    handle=context.handle,
                    kind=kind,
                    owner=context.owner,
                    description=description,
                    description_missing=False,
                    label=context.tool,
                    invocation_id=context.invocation_id,
                    tool=context.tool,
                    created_at=stamp,
                    updated_at=stamp,
                    submission=content,
                    activity="submitting",
                    supported_actions=("observe", "wait", "cancel"),
                    deduplication_key=deduplication,
                    resource_id=resource_id,
                ),
                observation_id=uuid4().hex,
            )
            return content

    async def _accepted(
        self, root: NativeRootKind, accepted: AcceptedNativeCall
    ) -> AcceptedTaskResult:
        handle = accepted.submission.handle
        previous = self._calls.get(handle)
        if previous is not None and previous.task.task_id != accepted.task.task_id:
            raise ValueError("Public handle cannot adopt a second native task")
        identity_owner = self._identities.get((root, accepted.task.task_id))
        if identity_owner is not None and identity_owner.submission.handle != handle:
            raise ValueError("Native server reused a task identity for another submission")
        self._calls[handle] = accepted
        self._identities[(root, accepted.task.task_id)] = accepted
        if handle in self._control_holds:
            self.recovery.defer(
                "native-cancel-delivery:" + handle, partial(self._deliver_cancel, handle)
            )
        try:
            return await self._confirm_acceptance(root, accepted)
        except Exception:

            async def retry_recording() -> None:
                client = self.clients[root]
                if any(error.accepted is accepted for error in client.unconfirmed_adoptions):
                    await client.retry_adoption(handle)
                else:
                    await self._confirm_acceptance(root, accepted)

            self.recovery.defer("native-acceptance:" + handle, retry_recording)
            raise
        finally:
            if handle not in self._drivers:
                driver = asyncio.create_task(self._drive(accepted))
                self._drivers[handle] = driver
                driver.add_done_callback(lambda finished: self._schedule_retirement(handle))

    async def _confirm_acceptance(
        self, root: NativeRootKind, accepted: AcceptedNativeCall
    ) -> AcceptedTaskResult:
        context = SubmissionContext.model_validate(
            await self.history.io.run(self.history.read_content, accepted.submission.context)
        )
        backend = context.backend.model_copy(update={"task_id": accepted.task.task_id})
        if context.backend != self.backends[root]:
            raise ValueError("Acceptance came from a different native root")

        def confirm() -> TaskSnapshot:
            with self.history.serialized():
                current = self._current(context.handle)
                adopted = current.model_copy(
                    update={
                        "backend": backend,
                        "raw_status": accepted.creation.status,
                        "updated_at": now(),
                        "connection_freshness": "fresh",
                        "activity": current.activity
                        if current.activity == "executing"
                        else "queued",
                    }
                )
                self.history.record_snapshot(adopted, observation_id=uuid4().hex)
                return adopted

        adopted = await self.history.io.run(confirm)
        await self.history.io.run(self._settle, adopted, accepted)
        return accepted_result(
            {
                "handle": adopted.handle,
                "kind": adopted.kind,
                "accepted": True,
                "status": adopted.status,
                "activity": adopted.activity,
            }
        )

    async def _drive(self, accepted: AcceptedNativeCall) -> None:
        try:
            await accepted.task.result()  # the sole SDK polling/input driver
        except Exception as error:
            logger.exception(
                "Task boundary reason=native_observation_failed error_type=%s", type(error).__name__
            )
            handle, reason = accepted.submission.handle, str(error)
            await self._record_or_defer(
                "native-uncertain:" + handle, lambda: self._uncertain(handle, reason, lost=True)
            )

            async def reconnect() -> bool:
                if self._resources_closed:
                    return True
                current = await self.history.io.run(self._current, handle)
                if current.terminal_at is not None or current.connection_freshness == "fresh":
                    return True
                original_backend = current.backend
                if original_backend is None or original_backend.task_id != accepted.task.task_id:
                    return True
                driver = self._drivers.get(handle)
                if driver is not None and not driver.done():
                    return False
                root = next(
                    kind
                    for kind, backend in self.backends.items()
                    if backend.server == original_backend.server
                )
                async with self._reconnect_locks[root]:
                    client = self.clients[root]
                    if not client.is_connected():
                        await client.close()
                        await client.__aenter__()
                    await client.check_health()
                    call = accepted.reconnected(client)
                    self._calls[handle] = call
                    self._identities[(root, call.task.task_id)] = call
                    replacement = asyncio.create_task(self._drive(call))
                    self._drivers[handle] = replacement
                    replacement.add_done_callback(
                        lambda finished: self._schedule_retirement(handle)
                    )
                return False  # only an actual confirmed task reply clears lost visibility

            self.recovery.defer("native-reconnect:" + handle, reconnect)

    async def _observe(
        self, root: NativeRootKind, task_id: str, reply: ClientGetTaskResult
    ) -> None:
        accepted = self._identities.get((root, task_id))
        if accepted is None:
            raise ValueError("Unbound native task reply")
        await self._record_or_defer(
            "native-reply:" + accepted.submission.handle,
            lambda: self._record_reply(accepted, reply),
            handle=accepted.submission.handle,
        )

    def _record_reply(self, accepted: AcceptedNativeCall, reply: ClientGetTaskResult) -> None:
        with self.history.serialized():
            current = self._current(accepted.submission.handle)
            raw = None
            if reply.status in {"completed", "failed", "cancelled"}:
                raw = self.history.write_content(
                    current.owner.session_id, reply.model_dump(mode="json")
                )
            observed = current.model_copy(
                update={
                    "raw_status": reply.status,
                    "updated_at": now(),
                    "connection_freshness": "fresh",
                    "backend_observation": raw or current.backend_observation,
                    "progress": current.progress.model_copy(
                        update={"message": reply.status_message}
                    )
                    if reply.status_message is not None
                    else current.progress,
                }
            )
            attempts = self.history.native_attempts(current.handle)
            if (
                current.terminal_at is None
                and reply.status in {"working", "input_required"}
                and not any(attempt.cleanup_state == "unknown" for attempt in attempts)
            ):
                observed = observed.model_copy(
                    update={
                        "status": "input_required"
                        if reply.status == "input_required"
                        else "running"
                        if attempts
                        else "accepted",
                        "activity": "cancelling"
                        if current.cancellation_requested_at
                        else "waiting_for_input"
                        if reply.status == "input_required"
                        else "executing"
                        if attempts
                        else "queued",
                        "attention_episode": None,
                    }
                )
            self.history.record_snapshot(observed, observation_id=uuid4().hex)
            self._settle(observed, accepted)

    async def _cleanup(self, handle: str) -> None:
        def settle() -> None:
            self._settle(self._current(handle), self._calls.get(handle))

        await self._record_or_defer("native-settlement:" + handle, settle, handle=handle)

    _settle = native_task_settlement._settle

    cancel = native_task_settlement.cancel

    _deliver_cancel = native_task_settlement._deliver_cancel

    await_settlement = native_task_settlement.await_settlement

    async def _record_or_defer(
        self, identity: str, operation: Callable[[], None], *, handle: str | None = None
    ) -> None:
        async def record() -> None:
            await self.history.io.run(operation)
            if handle is not None:
                await self._retire_resources(handle)

        try:
            await record()
        except TaskAccessDenied:
            raise
        except Exception:
            if not self.history.requires_recovery:
                raise
            self.recovery.defer(identity, record)

    def _driver_retired(self, handle: str, _finished: asyncio.Task[None]) -> None:
        self._schedule_retirement(handle)

    def _schedule_retirement(self, handle: str) -> None:
        task = asyncio.create_task(self._retire_resources(handle))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _retire_resources(self, handle: str) -> None:
        """Release live SDK resources after confirmed settlement and observer exit."""
        current = await self.history.io.run(self.history.task_snapshot, handle)
        driver, rpc = self._drivers.get(handle), self._rpc.get(handle)
        provider_retired = self._closing and await self.history.io.run(
            native_task_settlement.provider_locally_retired, self, handle
        )
        if (
            current is None
            or current.terminal_at is None
            and not provider_retired
            or self.history.requires_recovery
            or current.backend_observation is None
            or current.raw_status not in {"completed", "failed", "cancelled"}
            or driver is not None
            and not driver.done()
            or rpc is not None
            and not rpc.done()
            or any(
                error.accepted.submission.handle == handle
                for client in self.clients.values()
                for error in client.unconfirmed_adoptions
            )
        ):
            return
        if (
            provider_retired
            and current.terminal_at is None
            and current.connection_freshness != "lost"
        ):
            await self._record_or_defer(
                "provider-shutdown:" + handle,
                partial(
                    self._uncertain,
                    handle,
                    "Local observation retired for shutdown; the provider job remains unconfirmed.",
                    lost=True,
                ),
                handle=handle,
            )
            return
        accepted = self._calls.get(handle)
        if accepted is not None:
            observation = current.backend_observation

            def verify() -> None:
                context = SubmissionContext.model_validate(
                    self.history.read_content(accepted.submission.context)
                )
                reply = ClientGetTaskResult.model_validate(self.history.read_content(observation))
                if (
                    current.submission != accepted.submission.context
                    or current.backend
                    != context.backend.model_copy(update={"task_id": accepted.task.task_id})
                    or reply.task_id != accepted.task.task_id
                    or context.output_schema != accepted.submission.output_schema
                ):
                    raise ValueError("Native custody differs from the SDK resource being retired")

            await self.history.io.run(verify)
            if self._calls.get(handle) is not accepted:
                return
        retired_call = self._calls.pop(handle, None)
        if retired_call is not None:
            for root in self.roots:
                key = (root, retired_call.task.task_id)
                if self._identities.get(key) is retired_call:
                    self._identities.pop(key)
        self._drivers.pop(handle, None)
        self._rpc.pop(handle, None)
        self._control_holds.discard(handle)
        self._custody.discard(handle)

    def _rejected(self, handle: str, reason: str) -> None:
        with self.history.serialized():
            current = self._current(handle)
            if current.terminal_at is not None:
                return
            result = self.history.write_content(
                current.owner.session_id, {"error": reason, "acceptance": "rejected"}
            )
            stamp = now()
            self.history.record_snapshot(
                current.model_copy(
                    update={
                        "status": "failed",
                        "raw_status": "rejected",
                        "activity": "",
                        "settlement": "never_started",
                        "terminal_at": stamp,
                        "updated_at": stamp,
                        "result": result,
                        "supported_actions": ("get_result",),
                    }
                ),
                observation_id=uuid4().hex,
            )

    def _uncertain(self, handle: str, reason: str, *, lost: bool = False) -> None:
        with self.history.serialized():
            current = self._current(handle)
            if current.terminal_at is not None:
                return
            if (
                current.status == "task_state_uncertain"
                and current.progress.message == reason
                and current.attention_episode is not None
                and (not lost or current.connection_freshness == "lost")
            ):
                return
            observed = current.model_copy(
                update={
                    "status": "task_state_uncertain",
                    "connection_freshness": "lost" if lost else current.connection_freshness,
                    "settlement": "unknown",
                    "updated_at": now(),
                    "attention_episode": current.attention_episode or "native-" + uuid4().hex,
                    "progress": TaskProgress(message=reason),
                }
            )
            self.history.record_snapshot(observed, observation_id=uuid4().hex)
            publish_task_notice(self.history, observed, episode=observed.attention_episode or "")

    def _current(self, handle: str) -> TaskSnapshot:
        current = self.history.task_snapshot(handle)
        if current is None:
            raise ValueError("Native handle is unavailable")
        return current
