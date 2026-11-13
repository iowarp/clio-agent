"""Native SDK task acceptance; CLIO adds custody and observes SDK-owned driving."""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Any, Literal

import mcp_types
from docket.dependencies import current_execution
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp_tasks.client import ToolTask
from fastmcp_tasks.client_models import ClientCreateTaskResult
from fastmcp_tasks.context import get_task_context, get_task_leg_number
from mcp.shared.exceptions import MCPError

from clio_agent import conf
from clio_agent.arc.task_types import CoreReference, argument_digest
from clio_agent.tools import call_origin
from clio_agent.tools.task_receipt import AcceptedTaskResult


class AcceptedCallResult(CallToolResult):
    """SDK-facing parsed receipt created only from CLIO's typed raw acceptance."""


@dataclass(frozen=True)
class TaskSubmission:
    """One previously confirmed invocation; scoped to the calling SDK coroutine."""

    handle: str
    tool_name: str
    context: CoreReference
    tool_version: str | None
    output_schema: dict[str, Any] | None
    argument_digest: str

    def __post_init__(self) -> None:
        # Catalog refresh must not alter an accepted call's output contract.
        object.__setattr__(self, "output_schema", copy.deepcopy(self.output_schema))


@dataclass(frozen=True)
class AcceptedNativeCall:
    """Actual SDK acceptance, retained independently of the requesting turn."""

    submission: TaskSubmission
    task: ToolTask
    creation: ClientCreateTaskResult

    async def request_cancel(self) -> None:
        """Bound cancellation RPC delivery, without imposing a cleanup/result deadline."""
        client = self.task._client  # pinned SDK identity; dependency coupling stays here
        if not isinstance(client, TaskAwareClient):
            raise ValueError("Cancellation requires the application-owned task client")
        async with asyncio.timeout(client.health_timeout_s):
            await self.task.cancel()

    def reconnected(self, client: Client) -> AcceptedNativeCall:
        """Bind observation to the fresh SDK session, preserving original acceptance."""
        return AcceptedNativeCall(
            self.submission,
            ToolTask(client, self.submission.tool_name, self.creation, raise_on_error=False),
            self.creation,
        )


@dataclass(frozen=True)
class NativeExecutionIdentity:
    """Pinned native-worker identity, isolated from workload admission policy."""

    task_id: str
    auth_scope: str
    leg: int
    execution_key: str


def native_execution_identity() -> NativeExecutionIdentity:
    """Read the native worker context without assuming request ContextVars survive."""
    task = get_task_context()
    if task is None:
        raise ValueError("Owned work requires native task execution")
    return NativeExecutionIdentity(
        task.task_id, task.task_scope or "", get_task_leg_number(), current_execution.get().key
    )


def restore_native_call(
    client: Client,
    submission: TaskSubmission,
    *,
    task_id: str,
    status: str,
    created_at: str,
    updated_at: str,
) -> AcceptedNativeCall:
    """Reattach SDK observation to a recorded identity without another tools/call."""
    creation = ClientCreateTaskResult.model_validate(
        {
            "task_id": task_id,
            "status": status
            if status in {"working", "input_required", "completed", "failed", "cancelled"}
            else "working",
            "created_at": created_at,
            "last_updated_at": updated_at,
            "result_type": "task",
        }
    )
    return AcceptedNativeCall(
        submission, ToolTask(client, submission.tool_name, creation, raise_on_error=False), creation
    )


@asynccontextmanager
async def own_native_runtime(server: Any) -> AsyncIterator[None]:
    """Keep the pinned, reference-counted native server alive across client reconnects."""
    async with server._lifespan_manager():
        yield


TaskAcceptor = Callable[[AcceptedNativeCall], Awaitable[AcceptedTaskResult]]


class TaskAdoptionError(RuntimeError):
    """The backend accepted work; this exception must never mean safe to resubmit."""

    def __init__(self, accepted: AcceptedNativeCall, cause: BaseException) -> None:
        super().__init__(
            f"Backend accepted {accepted.task.task_id}; adoption is unconfirmed: {cause}"
        )
        self.accepted = accepted


def submission_failure(
    error: Exception,
) -> Literal["rejected", "recording_unconfirmed", "uncertain"]:
    """Distinguish SDK-confirmed pre-dispatch rejection from lost/accepted custody."""
    if isinstance(error, TaskAdoptionError):
        return "recording_unconfirmed"
    if isinstance(error, MCPError) and error.code in {-32600, -32601, -32602}:
        return "rejected"
    return "uncertain"


class TaskAwareClient(Client):
    """Use normal SDK calls, intercepting only accepted Tasks and local receipts.

    The application owns this client, its acceptor and ToolTask.result observers.
    No polling, input-driving or cancellation loop is implemented here. Worker
    context crosses Docket through its explicit submission reference, never this
    coroutine-local request binding.
    """

    def __init__(self, *args: Any, on_task_accepted: TaskAcceptor, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.health_timeout_s = conf.resolve(
            "gact.tasks.health_timeout_s",
            env="CLIO_TASKS_HEALTH_TIMEOUT_S",
            default=10.0,
            cast=conf.as_float,
        )
        if not math.isfinite(self.health_timeout_s) or self.health_timeout_s <= 0:
            raise ValueError("Task transport health timeout must be finite and positive")
        self._accept_task = on_task_accepted
        self._submission: ContextVar[TaskSubmission | None] = ContextVar(
            "clio_sdk_submission", default=None
        )
        self._adoptions: dict[asyncio.Future[AcceptedTaskResult], AcceptedNativeCall] = {}
        self._unconfirmed_adoptions: dict[str, TaskAdoptionError] = {}

    async def check_health(self) -> None:
        """Bound catalog discovery as health; task result waits/cleanup remain unbounded."""
        async with asyncio.timeout(self.health_timeout_s):
            await self.list_tools()

    @classmethod
    def from_configured(
        cls,
        configured: Client,
        transport: Any,
        *,
        on_task_accepted: TaskAcceptor,
        elicitation_handler: Any,
    ) -> TaskAwareClient:
        """Preserve configured SDK behavior; keep pinned client internals in this adapter."""
        if any(
            key.startswith("sampling_") and value is not None
            for key, value in configured._session_kwargs.items()
        ):
            raise ValueError("Configured MCP sampling is unsupported by CLIO")
        client = cls(
            transport,
            on_task_accepted=on_task_accepted,
            name=configured.name,
            timeout=configured._session_kwargs.get("read_timeout_seconds"),
            init_timeout=configured._init_timeout,
            auto_initialize=configured.auto_initialize,
            client_info=configured._session_kwargs.get("client_info"),
            elicitation_handler=elicitation_handler,
            cache=configured._cache_arg,
            extensions=configured._extensions_arg,
            result_claims=configured._result_claims_arg,
            input_required_max_rounds=configured.input_required_max_rounds,
            mode=configured.mode,
            prior_discover=configured.prior_discover,
        )
        client._session_kwargs["list_roots_callback"] = configured._session_kwargs.get(
            "list_roots_callback"
        )
        client._session_kwargs["logging_callback"] = configured._session_kwargs.get(
            "logging_callback"
        )
        client._session_kwargs["log_level"] = configured._session_kwargs.get("log_level")
        client._session_kwargs["message_handler"] = configured._session_kwargs.get(
            "message_handler"
        )
        client._session_kwargs["read_timeout_seconds"] = configured._session_kwargs.get(
            "read_timeout_seconds"
        )
        client._session_kwargs["client_info"] = configured._session_kwargs.get("client_info")
        client._transport_options = copy.copy(configured._transport_options)
        return client

    @contextmanager
    def submission(self, context: TaskSubmission) -> Iterator[None]:
        """Bind a durable request before tools/call; this does not submit or replay it."""
        if not context.handle or not context.tool_name:
            raise ValueError("Durable task submission identity is missing")
        token = self._submission.set(context)
        try:
            yield
        finally:
            self._submission.reset(token)

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_handler: Any = None,
        timeout: timedelta | float | int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        """Reject a missing custody binding before sending a potentially tasked call."""
        context = self._submission.get()
        if context is None or context.tool_name != name:
            raise ValueError("Confirm and bind task submission context before tools/call")
        submitted_arguments = copy.deepcopy(arguments)
        if argument_digest(submitted_arguments) != context.argument_digest:
            raise ValueError("Arguments differ from confirmed submission custody")
        request_meta = call_origin.with_origin(copy.deepcopy(meta or {}))
        tool_meta = request_meta.get("fastmcp", {})
        if not isinstance(tool_meta, dict):
            raise ValueError("Malformed MCP tool-version metadata")
        requested_version = tool_meta.get("version")
        if requested_version is not None and requested_version != context.tool_version:
            raise ValueError("Tool version differs from the captured submission contract")
        if context.tool_version is not None:
            request_meta["fastmcp"] = {**tool_meta, "version": context.tool_version}
        return await super().call_tool_mcp(
            name, submitted_arguments, progress_handler, timeout, request_meta or None
        )

    async def _resolve_claimed_result(
        self,
        name: str,
        result: mcp_types.Result,
        read_timeout_seconds: float | None,
    ) -> mcp_types.CallToolResult:
        if not isinstance(result, ClientCreateTaskResult):
            return await super()._resolve_claimed_result(name, result, read_timeout_seconds)
        context = self._submission.get()
        if context is None or context.tool_name != name:
            raise RuntimeError(
                f"Backend task {result.task_id} was accepted without its durable submission context"
            )
        accepted = AcceptedNativeCall(
            context, ToolTask(self, name, result, raise_on_error=False), result
        )
        adoption = self._begin_adoption(accepted)
        try:
            return await asyncio.shield(adoption)
        except asyncio.CancelledError as cancellation:
            # Repeated caller cancellation may leave this wait. The application
            # client retains custody, including a later recording failure.
            try:
                await asyncio.shield(adoption)
            except TaskAdoptionError as error:
                cancellation.add_note(str(error))
            raise

    def _begin_adoption(self, accepted: AcceptedNativeCall) -> asyncio.Future[AcceptedTaskResult]:
        async def adopt() -> AcceptedTaskResult:
            try:
                receipt = await self._accept_task(accepted)
                if not isinstance(receipt, AcceptedTaskResult):
                    raise TypeError("Task acceptor must return an application-authored receipt")
                return receipt
            except Exception as error:
                raise TaskAdoptionError(accepted, error) from error

        adoption = asyncio.ensure_future(adopt())
        self._adoptions[adoption] = accepted
        adoption.add_done_callback(self._adoption_finished)
        return adoption

    def _adoption_finished(self, adoption: asyncio.Future[AcceptedTaskResult]) -> None:
        accepted = self._adoptions.pop(adoption)
        error = (
            TaskAdoptionError(accepted, asyncio.CancelledError())
            if adoption.cancelled()
            else adoption.exception()
        )
        if error is not None:
            if not isinstance(error, TaskAdoptionError):
                error = TaskAdoptionError(accepted, error)
            self._unconfirmed_adoptions[accepted.submission.handle] = error
        else:
            previous = self._unconfirmed_adoptions.get(accepted.submission.handle)
            if previous is not None and previous.accepted is accepted:
                self._unconfirmed_adoptions.pop(accepted.submission.handle)

    @property
    def unconfirmed_adoptions(self) -> tuple[TaskAdoptionError, ...]:
        """Retain actual accepted identities until owner-local recording is reconciled."""
        return tuple(self._unconfirmed_adoptions.values())

    async def retry_adoption(self, handle: str) -> AcceptedTaskResult:
        """Retry identity recording only; never repeat tools/call or task execution."""
        failure = self._unconfirmed_adoptions.get(handle)
        if failure is None:
            raise ValueError("No unconfirmed acceptance exists for this handle")
        accepted = failure.accepted
        if any(call.submission.handle == handle for call in self._adoptions.values()):
            raise ValueError("Acceptance recording is already pending")
        receipt = await asyncio.shield(self._begin_adoption(accepted))
        self._unconfirmed_adoptions.pop(handle, None)
        return receipt

    async def _parse_call_tool_result(
        self,
        name: str,
        result: mcp_types.CallToolResult,
        raise_on_error: bool = False,
    ) -> CallToolResult:
        if isinstance(result, AcceptedTaskResult):
            return AcceptedCallResult(
                content=result.content,
                structured_content=result.structured_content,
                data=result.structured_content,
                is_error=result.is_error,
                meta=result.meta,
            )
        return await super()._parse_call_tool_result(name, result, raise_on_error)

    async def close(self) -> None:
        """Retain custody until recording settles, without a hidden cutoff.

        A recording failure intentionally refuses closure: the application owner
        must reconcile the retained native identity before retiring its transport.
        Closing in a finally block would abandon accepted work during a Core outage.
        """
        await self._finish_adoptions()
        await super().close()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context exit also retains adoption before releasing the SDK session."""
        await self._finish_adoptions()
        await super().__aexit__(exc_type, exc_val, exc_tb)

    async def _finish_adoptions(self) -> None:
        while self._adoptions:
            pending = tuple(self._adoptions)
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        if self._unconfirmed_adoptions:
            raise next(iter(self._unconfirmed_adoptions.values()))

    def new(self) -> TaskAwareClient:
        """Preserve SDK configuration while giving a clone independent call custody."""
        clone = super().new()
        if not isinstance(clone, TaskAwareClient):
            raise TypeError("Pinned SDK Client.new no longer preserves the subclass")
        clone._submission = ContextVar("clio_sdk_submission", default=None)
        clone._adoptions = {}
        clone._unconfirmed_adoptions = {}
        return clone
