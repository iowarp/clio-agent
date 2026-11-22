"""Observe SDK tasks/get replies and route task names through supported options."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, ClassVar, TypeVar

from fastmcp.client.transports.base import ClientTransport, TransportOptions
from fastmcp.client.transports.config import MCPConfigTransport
from fastmcp_tasks.client_models import (
    CancelTaskRequest,
    ClientGetTaskResult,
    GetTaskRequest,
    UpdateTaskRequest,
)
from mcp.client.session import ClientSession
from pydantic import BaseModel, TypeAdapter

ReplyT = TypeVar("ReplyT", bound=BaseModel)
TaskReplyObserver = Callable[[ClientSession, str, ClientGetTaskResult], Awaitable[None]]
TaskUpdateGuard = Callable[[ClientSession, str, dict[str, Any]], Awaitable[None]]


class NamedGetTaskRequest(GetTaskRequest):
    """Request-local Mcp-Name routing, without patching the SDK's request class."""

    name_param: ClassVar[str | None] = "taskId"


class NamedUpdateTaskRequest(UpdateTaskRequest):
    """Preserve SDK update semantics while carrying its native task identity."""

    name_param: ClassVar[str | None] = "taskId"


class NamedCancelTaskRequest(CancelTaskRequest):
    """Preserve SDK cancellation semantics while carrying its native task identity."""

    name_param: ClassVar[str | None] = "taskId"


class ObservedTaskSession(ClientSession):
    """Delegate requests to the SDK; record the replies its existing driver obtains."""

    def __init__(
        self,
        *args: Any,
        task_observer: TaskReplyObserver,
        update_guard: TaskUpdateGuard,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._observe_task_reply = task_observer
        self._guard_task_update = update_guard

    async def send_request(
        self,
        request: Any,
        result_type: type[ReplyT] | TypeAdapter[ReplyT],
        request_read_timeout_seconds: float | None = None,
        metadata: Any = None,
        progress_callback: Any = None,
    ) -> ReplyT:
        task_id: str | None = None
        if isinstance(request, GetTaskRequest):
            task_id = request.params.task_id
            request = NamedGetTaskRequest.model_validate(request.model_dump())
        elif isinstance(request, UpdateTaskRequest):
            task_id = request.params.task_id
            await self._guard_task_update(self, task_id, request.params.input_responses)
            request = NamedUpdateTaskRequest.model_validate(request.model_dump())
        elif isinstance(request, CancelTaskRequest):
            request = NamedCancelTaskRequest.model_validate(request.model_dump())
        result = await super().send_request(
            request, result_type, request_read_timeout_seconds, metadata, progress_callback
        )
        if task_id is not None and isinstance(result, ClientGetTaskResult):
            await self._observe_task_reply(self, task_id, result)
        return result


#: The sessions task observation composes in front of: the plain SDK session and
#: fastmcp's own default (``TransportOptions().session_class``; fastmcp 4.2 made it
#: a ``ClientSession`` subclass that skips output-schema checks for version-pinned
#: calls). Any other session class carries behaviour this adapter has not reviewed.
_KNOWN_SESSION_BASES = frozenset({ClientSession, TransportOptions().session_class})


def _observed_session_class(existing: type[ClientSession]) -> type[ClientSession]:
    """Compose CLIO's cooperative capability override; refuse unknown session behavior."""
    if existing is ClientSession:
        return ObservedTaskSession
    if existing in _KNOWN_SESSION_BASES:
        return type("ObservedSdkTaskSession", (ObservedTaskSession, existing), {})
    from clio_agent.tools.mcp_runtime import _DeclaredCapabilityOverride

    bases = existing.__bases__
    if len(bases) != 2 or bases[0] is not _DeclaredCapabilityOverride:
        raise ValueError("Task observation cannot compose with this SDK session class")
    if bases[1] not in _KNOWN_SESSION_BASES:
        raise ValueError("Task observation cannot compose with this SDK session class")
    return type("ObservedDeclaredTaskSession", (ObservedTaskSession, existing), {})


class TaskObservationTransport(ClientTransport):
    """Wrap one fully configured server transport, retaining identity and lifecycle.

    Configure transport-specific TLS, authentication and server selection before
    wrapping. Multi-server configuration belongs to separate per-server clients;
    this wrapper cannot stand in for their transport-specific dispatch.
    """

    def __init__(
        self,
        original: ClientTransport,
        observer: TaskReplyObserver,
        update_guard: TaskUpdateGuard,
    ) -> None:
        if isinstance(original, TaskObservationTransport):
            raise ValueError("Task observation transport cannot be nested")
        if isinstance(original, MCPConfigTransport):
            raise ValueError("Tasks require one native client per server, not a multi-server proxy")
        self.original = original
        self.observer = observer
        self.update_guard = update_guard
        self.legacy_only = original.legacy_only

    @asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Any,
    ) -> AsyncIterator[ClientSession]:
        options = transport_options or TransportOptions()
        session_class = _observed_session_class(options.session_class)
        async with self.original.connect_session(
            transport_options=replace(options, session_class=session_class),
            **{
                **session_kwargs,
                "task_observer": self.observer,
                "update_guard": self.update_guard,
            },
        ) as session:
            yield session

    async def close(self) -> None:
        """Close the original transport once its application owner has settled work."""
        await self.original.close()

    def get_session_id(self) -> str | None:
        """Expose the original verified transport session, including legacy routes."""
        return self.original.get_session_id()

    def _set_auth(self, auth: Any) -> None:
        """Delegate SDK authentication configuration without changing its scope."""
        self.original._set_auth(auth)

    def __repr__(self) -> str:
        return f"TaskObservationTransport({self.original!r})"
