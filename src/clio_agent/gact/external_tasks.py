"""Application-owned external SDK clients; servers retain execution ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from uuid import uuid4

import mcp_types
from fastmcp import Client
from fastmcp_tasks.client import ToolTask
from fastmcp_tasks.client_models import ClientCreateTaskResult, ClientGetTaskResult

from clio_agent.arc.task_types import (
    BackendReference,
    SubmissionContext,
    TaskOwner,
    TaskProgress,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact import context, external_task_connections, external_task_inputs
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccessDenied, TaskCaller
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.tools.caller_scope import caller_meta
from clio_agent.tools.mcp_runtime import enter_connection
from clio_agent.tools.task_call_context import TASK_CALL, permission_snapshot, safe_description
from clio_agent.tools.task_client import (
    AcceptedNativeCall,
    TaskAwareClient,
    TaskSubmission,
    submission_failure,
)
from clio_agent.tools.task_receipt import accepted_result

logger = logging.getLogger(__name__)


@dataclass
class ExternalConnection:
    """Live transport/coroutine resources, with all observations held in Core."""

    backend: BackendReference
    client: TaskAwareClient | None = None
    calls: dict[str, AcceptedNativeCall] = field(default_factory=dict)
    drivers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inline: ContextVar[SubmissionContext | None] = field(
        default_factory=lambda: ContextVar("clio_inline_mcp", default=None)
    )
    isolated: bool = False
    cache_key: tuple[str, str, str] = ("", "", "")
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    invocation: SubmissionContext | None = None
    rpcs: set[asyncio.Task[Any]] = field(default_factory=set)
    closing: bool = False


class ExternalTasks:
    """Use SDK result driving once per task; never mirror an external scheduler."""

    def __init__(self, app: Any) -> None:
        self.app, self.history, self.recovery = app, app.state.task_history, app.state.task_recovery
        self.connections: dict[tuple[str, str, str], ExternalConnection] = {}
        self._opening: dict[tuple[str, str, str], asyncio.Task[ExternalConnection]] = {}
        self._retiring: set[asyncio.Task[None]] = set()
        self._closing = False

    connection = external_task_connections.connection
    _retire_connection = external_task_connections.retire_connection

    async def restore(self, handles: tuple[str, ...] | None = None) -> None:
        """Register recovery of original external identities without submitting work."""
        selected = (
            handles
            if handles is not None
            else await self.history.io.run(self.history.task_handles, kind="MCP")
        )
        for handle in selected:
            current = await self.history.io.run(self.history.task_snapshot, handle)
            if current is not None and current.terminal_at is not None:
                if (
                    current.backend is not None
                    and current.backend.owner == "external_mcp"
                    and current.backend_observation is not None
                    and current.settlement == "external_acknowledged"
                ):
                    await self._record_or_defer(
                        "external-publication:" + handle,
                        partial(external_task_inputs.finish_terminal, self, handle),
                    )
                continue
            if current is None or current.submission is None:
                continue
            submission = SubmissionContext.model_validate(
                await self.history.io.run(self.history.read_content, current.submission)
            )
            if submission.backend.owner != "external_mcp":
                continue
            if current.backend is None or not current.backend.task_id:
                await self._record_or_defer(
                    "external-recovery:" + handle,
                    lambda handle=handle: self._attention(
                        handle,
                        "Acceptance identity is unknown. Inspect effects before deliberately retrying; nothing was resubmitted.",
                    ),
                )
                continue
            if "stdio" in current.backend.transport.casefold():
                await self._record_or_defer(
                    "external-recovery:" + handle,
                    lambda handle=handle: self._attention(
                        handle,
                        "The original stdio runtime is unavailable. Restarting it would not reconnect the same work.",
                    ),
                )
                continue

            async def recover(
                handle: str = handle, submission: SubmissionContext = submission
            ) -> bool:
                if self._closing:
                    return True
                task = await self.history.io.run(self.history.task_snapshot, handle)
                if task is None or task.terminal_at is not None:
                    return True
                if self.app.state.agent is None:
                    return False
                try:
                    configured = await self.history.io.run(self._recovery_client, task)
                    host = await self.connection(
                        task.backend.server,
                        configured,
                        task.backend.auth_scope,
                        original=task.backend,
                    )
                    existing = host.calls.get(task.backend.task_id)
                    if existing is None:
                        creation = ClientCreateTaskResult.model_validate(
                            {
                                "task_id": task.backend.task_id,
                                "status": task.raw_status
                                if task.raw_status in {"working", "input_required"}
                                else "working",
                                "created_at": task.created_at,
                                "last_updated_at": task.updated_at,
                                "result_type": "task",
                            }
                        )
                        binding = TaskSubmission(
                            handle,
                            submission.tool,
                            task.submission,
                            submission.tool_version,
                            submission.output_schema,
                            submission.argument_digest,
                        )
                        assert host.client is not None
                        existing = AcceptedNativeCall(
                            binding,
                            ToolTask(host.client, submission.tool, creation, raise_on_error=False),
                            creation,
                        )
                        host.calls[task.backend.task_id] = existing
                    elif existing.submission.handle != handle:
                        raise ValueError(
                            "Original external task identity collides with another handle"
                        )
                    self._drive(host, existing)
                    return (
                        True  # the SDK observer now owns visibility, including reconnect failures
                    )
                except Exception as error:
                    logger.exception(
                        "Task boundary reason=external_reconnection_failed error_type=%s",
                        type(error).__name__,
                    )
                    reason = str(error)
                    await self._record_or_defer(
                        "external-recovery-attention:" + handle,
                        lambda: self._attention(handle, reason),
                    )
                    return False

            self.recovery.defer("external-recovery:" + handle, recover)

    def _recovery_client(self, task: TaskSnapshot) -> Client:
        """Resolve current trusted configuration and require its original auth scope."""
        from functools import partial

        from clio_agent.gact.agents.tool_executor_resolution import (
            resolve_active_base_agent_tool_executor,
        )
        from clio_agent.gact.mcp_connection_observability import emit_downgrade_events_for_executor

        assert task.backend is not None
        executor = resolve_active_base_agent_tool_executor(
            self.app.state.agent,
            app=self.app,
            session_id=task.owner.session_id,
            emit_downgrade=partial(
                emit_downgrade_events_for_executor, self.app, task.owner.session_id
            ),
        )
        inner = getattr(executor, "_async_executor", executor)
        specs = getattr(executor, "_clio_namespace_specs", {})
        spec = specs.get(task.backend.server)
        scope = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
        factory = getattr(inner, "_clio_namespace_direct_factories", {}).get(task.backend.server)
        if spec is None or scope != task.backend.auth_scope or factory is None:
            raise ValueError(
                "Original server/auth configuration is unavailable; work was not resubmitted"
            )
        client = factory()
        if type(client.transport).__name__ != task.backend.transport:
            raise ValueError("Original external task transport changed")
        return client

    async def call(
        self,
        server: str,
        configured: Client,
        name: str,
        arguments: dict[str, Any],
        *,
        auth_scope: str,
        progress_handler: Callable[[float, float | None, str | None], Awaitable[None]]
        | None = None,
    ) -> Any:
        """Confirm custody before one SDK tools/call; uncertain replies are never replayed."""
        if self._closing:
            raise ValueError("External task observation is closing")
        public = dict(arguments)
        description = public.pop("_clio_task_description", None) or safe_description(name, public)
        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError("Task description is invalid")
        host = await self.connection(server, configured, auth_scope)
        try:
            return await self._call(host, name, public, description, progress_handler)
        finally:
            self._schedule_retirement(host)

    async def _call(
        self,
        host: ExternalConnection,
        name: str,
        public: dict[str, Any],
        description: str,
        progress_handler: Callable[[float, float | None, str | None], Awaitable[None]] | None,
    ) -> Any:
        """Submit one call using its captured schema and trusted invocation identity."""
        assert host.client is not None
        tools = await host.client.list_tools()
        component = next((tool for tool in tools if tool.name == name), None)
        if component is None:
            raise ValueError("Tool is unavailable on the selected server")
        sid, invocation = context.active_session_id(), TASK_CALL.get().invocation_id or uuid4().hex
        assignment = await self.history.io.run(self.history.latest_assignment, sid)
        if assignment is None:
            raise ValueError("MCP calls require an owning assignment")
        session = self.app.state.sessions.get(sid)
        workspace = (
            self.app.state.workspaces.get(session.workspace_id) if session is not None else None
        )
        if session is None or workspace is None:
            raise ValueError("MCP execution session or workspace is unavailable")
        handle, reference, stamp = "task_" + uuid4().hex, uuid4().hex, now()
        submission = SubmissionContext(
            reference=reference,
            handle=handle,
            owner=TaskOwner(
                human_id=assignment.owner.human_id,
                session_id=sid,
                assignment_id=assignment.handle,
                parent_handle=assignment.handle,
            ),
            invocation_id=invocation,
            tool=name,
            argument_digest=argument_digest(public),
            arguments=public,
            output_schema=component.output_schema,
            tool_version=getattr(component, "version", None),
            workspace=workspace.root_path,
            permissions=permission_snapshot(self.app, sid),
            confinement={},
            attribution={"agent": context.active_react_scope()},
            execution_limits={},
            backend=host.backend,
        )

        def confirm() -> Any:
            with self.history.serialized():
                self.app.state.conversations.require_admission(assignment)
                content = self.history.record_submission(submission)
                self.history.record_snapshot(
                    TaskSnapshot(
                        handle=handle,
                        kind="MCP",
                        owner=submission.owner,
                        description=description,
                        description_missing=False,
                        label=name,
                        tool=name,
                        invocation_id=invocation,
                        created_at=stamp,
                        updated_at=stamp,
                        submission=content,
                        supported_actions=("observe", "wait", "cancel"),
                    ),
                    observation_id=uuid4().hex,
                )
                return content

        if self._closing:
            raise ValueError("External task observation is closing")
        content = await self.history.io.run(confirm)
        if host.isolated:
            if host.invocation is not None:
                raise ValueError("An invocation-bound transport cannot serve another submission")
            host.invocation = submission
        binding = TaskSubmission(
            handle,
            name,
            content,
            submission.tool_version,
            submission.output_schema,
            submission.argument_digest,
        )

        async def report_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            await self._record_or_defer(
                "external-progress:" + handle,
                partial(self._record_progress, handle, progress, total, message),
            )
            if progress_handler is not None and not self.history.requires_recovery:
                await progress_handler(progress, total, message)

        async def submit() -> Any:
            token = host.inline.set(submission)
            try:
                client = host.client
                if client is None:
                    raise ValueError("External task transport is unavailable before submission")
                with client.submission(binding):
                    return await client.call_tool(
                        name,
                        public,
                        raise_on_error=False,
                        progress_handler=report_progress,
                        meta=caller_meta(),  # the calling session, for per-branch state
                    )
            finally:
                host.inline.reset(token)

        rpc = asyncio.create_task(submit())
        host.rpcs.add(rpc)
        rpc.add_done_callback(lambda finished: self._rpc_retired(host, finished))
        stopped = False
        while True:
            try:
                reply = await asyncio.shield(rpc)
                break
            except asyncio.CancelledError:
                stopped = True
                if rpc.cancelled():
                    raise
            except Exception as error:
                logger.exception(
                    "Task boundary reason=external_submission_failed error_type=%s",
                    type(error).__name__,
                )
                reason = str(error)
                failure = submission_failure(error)
                if failure == "rejected":
                    await self._record_or_defer(
                        "external-rejected:" + handle,
                        partial(self._rejected, handle, reason),
                        retire=lambda: self._retire_connection(host),
                    )
                    return mcp_types.CallToolResult(
                        content=[
                            mcp_types.TextContent(
                                type="text",
                                text=json.dumps(
                                    {
                                        "handle": handle,
                                        "accepted": False,
                                        "acceptance": "rejected",
                                        "error": reason,
                                        "retry_safe": True,
                                    }
                                ),
                            )
                        ],
                        is_error=True,
                    )
                await self._record_or_defer(
                    "external-call:" + handle, partial(self._attention, handle, reason)
                )
                return accepted_result(
                    {
                        "handle": handle,
                        "kind": "MCP",
                        "accepted": True if failure == "recording_unconfirmed" else None,
                        "acceptance": failure,
                        "retry_safe": False,
                    }
                )
        accepted = (
            await self.history.io.run(self.history.task_snapshot, handle)
        ).backend is not None or any(
            call.submission.handle == handle for call in host.calls.values()
        )
        if not accepted:
            await self._record_or_defer(
                "external-inline:" + handle,
                partial(self._inline_result, handle, reply),
                retire=lambda: self._retire_connection(host),
            )
        if stopped:
            raise asyncio.CancelledError
        return reply

    def _adopt(self, host: ExternalConnection, accepted: AcceptedNativeCall) -> Any:
        with self.history.serialized():
            current = self.history.task_snapshot(accepted.submission.handle)
            observed = current.model_copy(
                update={
                    "backend": host.backend.model_copy(update={"task_id": accepted.task.task_id}),
                    "raw_status": accepted.creation.status,
                    "status": "running",
                    "activity": "executing",
                    "updated_at": now(),
                    "connection_freshness": "fresh",
                }
            )
            self.history.record_snapshot(observed, observation_id=uuid4().hex)
            return accepted_result(
                {
                    "handle": current.handle,
                    "kind": "MCP",
                    "accepted": True,
                    "status": observed.status,
                }
            )

    def _drive(self, host: ExternalConnection, call: AcceptedNativeCall) -> None:
        if call.task.task_id in host.drivers and not host.drivers[call.task.task_id].done():
            return
        current = self.history.task_snapshot(call.submission.handle)
        if current is not None and current.cancellation_requested_at is not None:

            async def deliver_cancel() -> None:
                if self._closing:
                    return
                task = await self.history.io.run(self.history.task_snapshot, call.submission.handle)
                if (
                    task is not None
                    and task.terminal_at is None
                    and task.raw_status not in {"completed", "failed", "cancelled"}
                ):
                    await self.cancel(task.handle)

            self.recovery.defer("external-cancel-delivery:" + current.handle, deliver_cancel)

        async def result() -> None:
            try:
                await call.task.result()  # SDK alone owns polling and input re-entry
            except Exception as error:
                logger.exception(
                    "Task boundary reason=external_observation_failed error_type=%s",
                    type(error).__name__,
                )
                reason = str(error)
                await self._record_or_defer(
                    "external-attention:" + call.submission.handle,
                    lambda: self._attention(call.submission.handle, reason),
                )

                async def reconnect() -> bool:
                    if self._closing:
                        return True  # confirmed Core visibility is retained for startup recovery
                    task = await self.history.io.run(
                        self.history.task_snapshot, call.submission.handle
                    )
                    if task is not None and (
                        task.terminal_at is not None or task.connection_freshness == "fresh"
                    ):
                        return True
                    driver = host.drivers.get(call.task.task_id)
                    if driver is not None and not driver.done():
                        return False
                    async with host.reconnect_lock:
                        assert host.client is not None
                        if not host.client.is_connected():
                            if "stdio" in host.backend.transport.casefold():
                                # The attention record remains durable. This identity
                                # cannot reconnect, so retrying cannot establish custody.
                                return True
                            await host.client.close()
                            await enter_connection(host.client)
                        await host.client.check_health()
                        rebound = call.reconnected(host.client)
                        host.calls[call.task.task_id] = rebound
                        self._drive(host, rebound)  # original identity/schema; never tools/call
                    return False  # only an actual persisted tasks/get reply clears lost visibility

                self.recovery.defer("external-reconnect:" + call.submission.handle, reconnect)

        driver = asyncio.create_task(result())
        host.drivers[call.task.task_id] = driver
        driver.add_done_callback(lambda finished: self._schedule_retirement(host, call))

    def _rpc_retired(self, host: ExternalConnection, rpc: asyncio.Task[Any]) -> None:
        host.rpcs.discard(rpc)
        self._schedule_retirement(host)

    def _schedule_retirement(
        self, host: ExternalConnection, call: AcceptedNativeCall | None = None
    ) -> None:
        task = asyncio.create_task(
            self._retire_resources(host, call)
            if call is not None
            else self._retire_connection(host)
        )
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _retire_resources(self, host: ExternalConnection, call: AcceptedNativeCall) -> None:
        current = await self.history.io.run(self.history.task_snapshot, call.submission.handle)
        driver = host.drivers.get(call.task.task_id)
        if (
            current is None
            or self.history.requires_recovery
            or current.backend_observation is None
            or current.raw_status not in {"completed", "failed", "cancelled"}
            or driver is not None
            and not driver.done()
            or host.client is not None
            and any(error.accepted is call for error in host.client.unconfirmed_adoptions)
        ):
            return
        raw = await self.history.io.run(self.history.read_content, current.backend_observation)
        reply = ClientGetTaskResult.model_validate(raw)
        if (
            current.submission != call.submission.context
            or current.backend != host.backend.model_copy(update={"task_id": call.task.task_id})
            or reply.task_id != call.task.task_id
        ):
            raise ValueError("External custody differs from the SDK resource being retired")
        if host.calls.get(call.task.task_id) is call:
            host.calls.pop(call.task.task_id)
            host.drivers.pop(call.task.task_id, None)
        await self._retire_connection(host)

    async def _observe(
        self, host: ExternalConnection, task_id: str, reply: ClientGetTaskResult
    ) -> None:
        accepted = host.calls.get(task_id)
        if accepted is None:
            raise ValueError("Task reply has no bound server/session/task identity")
        await self._record_or_defer(
            "external-reply:" + accepted.submission.handle,
            lambda: self._record_reply(host, accepted, reply),
            retire=lambda: self._retire_resources(host, accepted),
        )

    def _record_reply(
        self, host: ExternalConnection, accepted: AcceptedNativeCall, reply: ClientGetTaskResult
    ) -> None:
        return external_task_inputs._record_reply(self, host, accepted, reply)

    async def _input(self, host: ExternalConnection, params: Any, request: Any) -> Any:
        return await external_task_inputs._input(self, host, params, request)

    def _task(self, host: ExternalConnection, task_id: str) -> TaskSnapshot:
        call = host.calls.get(task_id)
        current = self.history.task_snapshot(call.submission.handle) if call is not None else None
        if current is None or current.backend != host.backend.model_copy(
            update={"task_id": task_id}
        ):
            raise ValueError("MCP task identity differs from its observer")
        return current

    def _details(self, task: TaskSnapshot) -> dict[str, Any]:
        return dict(self.history.read_content(task.details)) if task.details is not None else {}

    def _attention(
        self, handle: str, message: str, *, lost: bool = True, episode: str = ""
    ) -> None:
        with self.history.serialized():
            current = self.history.task_snapshot(handle)
            if current.terminal_at is not None:
                return
            if (
                current.status == "task_state_uncertain"
                and current.progress.message == message
                and (not lost or current.connection_freshness == "lost")
                and current.attention_episode
            ):
                return
            observed = current.model_copy(
                update={
                    "status": "task_state_uncertain",
                    "settlement": "unknown",
                    "connection_freshness": "lost" if lost else current.connection_freshness,
                    "updated_at": now(),
                    "progress": TaskProgress(message=message),
                    "attention_episode": current.attention_episode or episode or uuid4().hex,
                }
            )
            self.history.record_snapshot(observed, observation_id=uuid4().hex)
            publish_task_notice(self.history, observed, episode=observed.attention_episode)

    def _record_progress(
        self, handle: str, done: float, total: float | None, message: str | None
    ) -> None:
        """Persist this invocation's progress; progress alone cannot wake a model."""
        with self.history.serialized():
            current = self.history.task_snapshot(handle)
            if current is None or current.terminal_at is not None:
                return
            progress = current.progress.model_copy(
                update={"completed": done, "total": total, "message": message or ""}
            )
            progress = TaskProgress.model_validate(progress.model_dump())
            self.history.record_snapshot(
                current.model_copy(update={"progress": progress, "updated_at": now()}),
                observation_id=uuid4().hex,
            )

    def _inline_result(self, handle: str, reply: Any) -> None:
        with self.history.serialized():
            current = self.history.task_snapshot(handle)
            value = {
                "content": [part.model_dump(mode="json") for part in reply.content],
                "structuredContent": reply.structured_content,
                "isError": reply.is_error,
            }
            result = self.history.write_content(current.owner.session_id, value)
            updates = {
                "status": "failed" if reply.is_error else "completed",
                "raw_status": "inline",
                "activity": "",
                "terminal_at": now(),
                "updated_at": now(),
                "settlement": "external_acknowledged",
                "result": result,
                "backend_observation": result,
                "supported_actions": ("get_result",),
            }
            # An actual inline result settles this invocation, even if cancellation
            # was requested while it ran. It is not a remote cleanup receipt.
            observed = current.model_copy(update=updates)
            self.history.record_snapshot(observed, observation_id=uuid4().hex)
            if observed.attention_episode is not None:
                publish_task_notice(self.history, observed, episode=observed.attention_episode)
            # Inline output is already this invocation's observation; no task wake.

    def _rejected(self, handle: str, reason: str) -> None:
        with self.history.serialized():
            current = self.history.task_snapshot(handle)
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

    async def _record_or_defer(self, identity: str, write: Any, *, retire: Any = None) -> None:
        async def record() -> None:
            await self.history.io.run(write)
            if retire is not None:
                await retire()

        try:
            await record()
        except TaskAccessDenied:
            raise
        except Exception:
            if not self.history.requires_recovery:
                raise
            self.recovery.defer(identity, record)

    async def cancel(self, handle: str, *, caller: TaskCaller | None = None) -> dict[str, Any]:
        """Request cancellation through this owner's verified live SDK binding."""
        from clio_agent.gact.external_task_controls import cancel

        return await cancel(self, handle, caller=caller)

    async def close(self) -> None:
        """Retire observation without claiming surviving external jobs were cancelled."""
        await external_task_connections.close_connections(self)
