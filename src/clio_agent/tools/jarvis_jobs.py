"""Curated JARVIS/Spack application tools backed by durable relay jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from fastmcp_tasks.client_models import ClientGetTaskResult
from mcp.types import ToolAnnotations

from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, TaskKey
from clio_agent.tools.relay_transport import (
    RELAY_INLINE_LIMIT_CODE,
    RELAY_RESULT_DELIVERY_SCHEMA,
    RelayInlineResultTooLargeError,
    RelayTaskIdentity,
    RelayTransportContractError,
)

JARVIS_NAMESPACE = "jarvis"
JARVIS_TOOL_NAMES = (
    "jarvis_create_pipeline",
    "jarvis_describe",
    "jarvis_add_step",
    "jarvis_edit_step",
    "jarvis_run",
    "jarvis_get_execution",
)
JARVIS_RUN_HANDLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "job_id": {"type": "string"},
        "kind": {"type": "string", "const": "jarvis"},
        "state": {"type": "string", "const": "queued"},
        "terminal": {"type": "boolean", "const": False},
    },
    "required": ["task_id", "job_id", "kind", "state", "terminal"],
    "additionalProperties": False,
}
JARVIS_EXECUTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "const": "clio-agent.jarvis-execution.v1"},
        "pipeline_id": {"type": "string"},
        "execution_id": {"type": "string"},
        "state": {"type": "string"},
        "terminal": {"type": "boolean"},
        "progress": {},
        "artifacts": {},
        "services": {},
        "scheduler_native_id": {"type": ["string", "null"]},
        "scheduler_provider": {"type": ["string", "null"]},
    },
    "required": [
        "schema_version",
        "pipeline_id",
        "execution_id",
        "state",
        "terminal",
        "progress",
        "artifacts",
        "services",
        "scheduler_native_id",
        "scheduler_provider",
    ],
    "additionalProperties": False,
}

__all__ = [
    "JARVIS_EXECUTION_OUTPUT_SCHEMA",
    "JARVIS_NAMESPACE",
    "JARVIS_RUN_HANDLE_OUTPUT_SCHEMA",
    "JARVIS_TOOL_NAMES",
    "JarvisJobError",
    "JarvisJobs",
    "JarvisRunHandle",
]


class JarvisJobError(RelayTransportContractError):
    """A curated JARVIS dispatch or execution violated its durable contract."""


@dataclass(frozen=True)
class JarvisRunHandle:
    """Persisted identity for one admitted handle-first JARVIS execution."""

    identity: RelayTaskIdentity

    @property
    def task_id(self) -> str:
        """Return the relay task identity used for detach and reconnect."""
        return self.identity.task_id

    @property
    def job_id(self) -> str:
        """Return the relay job identity, equal to the task identity."""
        return self.identity.job_id

    def to_wire(self) -> dict[str, Any]:
        """Return the advertised handle-first tool result."""
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "kind": "jarvis",
            "state": "queued",
            "terminal": False,
        }


class JarvisRelayClient(Protocol):
    """Relay operations used by the curated JARVIS owner."""

    async def submit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RelayTaskIdentity: ...

    async def poll(self, task: RelayTaskIdentity) -> ClientGetTaskResult: ...

    async def resume(
        self, key: TaskKey, *, timeout_seconds: float | None = None
    ) -> ClientGetTaskResult: ...


JarvisClientFactory = Callable[[], AbstractAsyncContextManager[JarvisRelayClient]]

_CLUSTER = {"type": "string", "minLength": 1}
_IDENTITY = {"type": "string", "minLength": 1, "maxLength": 256}
_OPTIONAL_IDENTITY = {"anyOf": [_IDENTITY, {"type": "null"}], "default": None}
_CONTROL_PROPERTIES = {
    "idempotency_key": {"type": "string", "minLength": 1},
    "timeout_seconds": {"type": "integer", "minimum": 1},
}
_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "jarvis_create_pipeline": {
        "type": "object",
        "properties": {
            "cluster": _CLUSTER,
            "pipeline_id": _IDENTITY,
            "execution": {"type": ["object", "null"], "default": None},
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "pipeline_id"],
        "additionalProperties": False,
    },
    "jarvis_describe": {
        "type": "object",
        "properties": {
            "cluster": _CLUSTER,
            "target": {
                "type": "string",
                "enum": ["packages", "package_search", "package", "pipeline", "step"],
            },
            "package_name": _OPTIONAL_IDENTITY,
            "query": _OPTIONAL_IDENTITY,
            "page_size": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            "cursor": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 1024},
                    {"type": "null"},
                ],
                "default": None,
            },
            "pipeline_id": _OPTIONAL_IDENTITY,
            "step_id": _OPTIONAL_IDENTITY,
            "include_yaml": {"type": "boolean", "default": True},
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "target"],
        "additionalProperties": False,
    },
    "jarvis_add_step": {
        "type": "object",
        "properties": {
            "cluster": _CLUSTER,
            "pipeline_id": _IDENTITY,
            "package_name": _IDENTITY,
            "config": {"type": ["object", "null"], "default": None},
            "step_id": _OPTIONAL_IDENTITY,
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "pipeline_id", "package_name"],
        "additionalProperties": False,
    },
    "jarvis_edit_step": {
        "type": "object",
        "properties": {
            "cluster": _CLUSTER,
            "pipeline_id": _IDENTITY,
            "step_id": _IDENTITY,
            "operation": {"type": "string", "enum": ["edit", "remove"], "default": "edit"},
            "config": {"type": ["object", "null"], "default": None},
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "pipeline_id", "step_id"],
        "additionalProperties": False,
    },
    "jarvis_run": {
        "type": "object",
        "properties": {
            "cluster": _CLUSTER,
            "pipeline_id": _IDENTITY,
            "execution_id": _OPTIONAL_IDENTITY,
            "submit": {"type": "boolean", "default": True},
            "spack_specs": {
                "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "default": None,
            },
            "execution": {"type": ["object", "null"], "default": None},
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "pipeline_id"],
        "additionalProperties": False,
    },
    "jarvis_get_execution": {
        "type": "object",
        "properties": {
            "cluster": _CLUSTER,
            "pipeline_id": _IDENTITY,
            "execution_id": _IDENTITY,
            "include_progress": {"type": "boolean", "default": True},
            "include_service_runtimes": {"type": "boolean", "default": False},
            "artifacts": {"type": ["object", "null"], "default": None},
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "pipeline_id", "execution_id"],
        "additionalProperties": False,
    },
}

_DESCRIPTIONS = {
    "jarvis_create_pipeline": (
        "Use this when an application needs a new durable JARVIS pipeline before steps "
        "are configured. The call waits only for this bounded deployment operation."
    ),
    "jarvis_describe": (
        "Use this when an agent must inspect package settings, pipeline structure, or a "
        "step before making an exact JARVIS configuration decision."
    ),
    "jarvis_add_step": (
        "Use this when an agent has selected a described package and needs to add one "
        "validated application step to a durable pipeline."
    ),
    "jarvis_edit_step": (
        "Use this when an existing pipeline step must be changed or removed before an "
        "execution is admitted."
    ),
    "jarvis_run": (
        "Use this when a configured JARVIS/Spack pipeline should start durably. This is "
        "handle-first: persist task_id and query progress separately."
    ),
    "jarvis_get_execution": (
        "Use this when an agent needs the current execution lifecycle, progress, "
        "artifacts, services, and scheduler-native identity."
    ),
}


class _ProjectedJarvisTool(Tool):
    """One curated operation exposed below the gateway's jarvis mount."""

    def __init__(self, name: str, owner: "JarvisJobs") -> None:
        read_only = name in {"jarvis_describe", "jarvis_get_execution"}
        output_schema = (
            JARVIS_RUN_HANDLE_OUTPUT_SCHEMA
            if name == "jarvis_run"
            else JARVIS_EXECUTION_OUTPUT_SCHEMA
            if name == "jarvis_get_execution"
            else {"type": "object", "additionalProperties": True}
        )
        super().__init__(
            name=name.removeprefix("jarvis_"),
            description=_DESCRIPTIONS[name],
            parameters=deepcopy(_INPUT_SCHEMAS[name]),
            output_schema=deepcopy(output_schema),
            annotations=ToolAnnotations(
                read_only_hint=read_only,
                destructive_hint=False,
                idempotent_hint=read_only,
                open_world_hint=False,
            ),
        )
        self._relay_name = name
        self._owner = owner

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Invoke the owner while preserving typed errors and structured output."""

        result = await self._owner.invoke(self._relay_name, arguments)
        payload = result.to_wire() if isinstance(result, JarvisRunHandle) else result
        return ToolResult(structured_content=payload)


class JarvisJobs:
    """Six application-level JARVIS tools composed over RelayTransportClient."""

    def __init__(
        self,
        client_factory: JarvisClientFactory,
        *,
        poll_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dispatch_timeout_seconds: float = 600.0,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if dispatch_timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("JARVIS dispatch and request timeouts must be positive")
        self._client_factory = client_factory
        self._poll_sleep = poll_sleep
        self._dispatch_timeout_seconds = dispatch_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        server = FastMCP("clio-jarvis-jobs")
        for name in JARVIS_TOOL_NAMES:
            server.add_tool(_ProjectedJarvisTool(name, self))
        self._server = server

    @property
    def server(self) -> FastMCP:
        """Return the bare-name server mounted as namespace jarvis."""
        return self._server

    async def create_pipeline(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Create one pipeline and wait for the bounded deployment call."""
        return await self._bounded("jarvis_create_pipeline", arguments)

    async def describe(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Describe package or pipeline state through one bounded query."""
        return await self._bounded("jarvis_describe", arguments)

    async def add_step(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Add one validated package step and wait for deployment completion."""
        return await self._bounded("jarvis_add_step", arguments)

    async def edit_step(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Edit or remove one pipeline step and wait for the bounded result."""
        return await self._bounded("jarvis_edit_step", arguments)

    async def run(self, arguments: Mapping[str, Any]) -> JarvisRunHandle:
        """Submit jarvis_run and return its durable identity without workload waiting."""

        payload = dict(arguments)
        forbidden = {"wait", "wait_for_terminal", "wait_timeout_seconds", "poll_seconds"}
        smuggled = sorted(forbidden.intersection(payload))
        if smuggled:
            raise JarvisJobError(
                "jarvis_run does not accept internal wait; use jarvis_get_execution",
                reason="jarvis_run_wait_not_allowed",
                details={"fields": smuggled},
            )
        async with self._client_factory() as relay:
            identity = await relay.submit(
                "jarvis_run",
                payload,
                timeout_seconds=self._request_timeout_seconds,
            )
        return JarvisRunHandle(identity)

    async def get_execution(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Return the unified execution lifecycle and application outputs."""

        payload = await self._bounded("jarvis_get_execution", arguments)
        return _execution_projection(payload, arguments)

    async def resume_run(
        self,
        persisted: JarvisRunHandle | RelayTaskIdentity | TaskKey,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Resume a detached run dispatch from its persisted relay task identity."""

        if isinstance(persisted, JarvisRunHandle):
            key = persisted.identity.key
        elif isinstance(persisted, RelayTaskIdentity):
            key = persisted.key
        else:
            key = persisted
        async with self._client_factory() as relay:
            final = await relay.resume(
                key,
                timeout_seconds=timeout_seconds or self._dispatch_timeout_seconds,
            )
        return _terminal_payload("jarvis_run", final)

    async def invoke(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> JarvisRunHandle | dict[str, Any]:
        """Dispatch one of the six curated tool names."""

        if tool_name == "jarvis_create_pipeline":
            return await self.create_pipeline(arguments)
        if tool_name == "jarvis_describe":
            return await self.describe(arguments)
        if tool_name == "jarvis_add_step":
            return await self.add_step(arguments)
        if tool_name == "jarvis_edit_step":
            return await self.edit_step(arguments)
        if tool_name == "jarvis_run":
            return await self.run(arguments)
        if tool_name == "jarvis_get_execution":
            return await self.get_execution(arguments)
        raise ValueError(f"unsupported curated JARVIS tool: {tool_name!r}")

    async def _bounded(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Run one deploy/query dispatch to terminal with an injected poll clock."""

        payload = dict(arguments)
        payload["wait_for_terminal"] = True
        payload["wait_timeout_seconds"] = self._dispatch_timeout_seconds
        async with self._client_factory() as relay:
            identity = await relay.submit(
                tool_name,
                payload,
                timeout_seconds=self._request_timeout_seconds,
            )
            final = await self._drive_to_terminal(relay, identity)
        return _terminal_payload(tool_name, final)

    async def _drive_to_terminal(
        self,
        relay: JarvisRelayClient,
        identity: RelayTaskIdentity,
    ) -> ClientGetTaskResult:
        """Poll a bounded dispatch without replacing the process event-loop clock."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._dispatch_timeout_seconds
        while True:
            current = await relay.poll(identity)
            if current.status in TERMINAL_TASK_STATES:
                return current
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise JarvisJobError(
                    f"{identity.task_id} did not finish its bounded JARVIS dispatch",
                    reason="jarvis_dispatch_timeout",
                    details={
                        "task_id": identity.task_id,
                        "timeout_seconds": self._dispatch_timeout_seconds,
                    },
                )
            interval = float(current.poll_interval_ms or 1_000) / 1_000
            await self._poll_sleep(min(interval, remaining))


def _terminal_payload(tool_name: str, final: ClientGetTaskResult) -> dict[str, Any]:
    """Validate one terminal relay result and return its authoritative payload."""

    if final.status != "completed":
        raise JarvisJobError(
            f"{tool_name} relay dispatch ended in state {final.status!r}",
            reason="jarvis_dispatch_failed",
            details={
                "tool": tool_name,
                "task_id": final.task_id,
                "state": final.status,
                "relay_error": dict(final.error or {}),
            },
        )
    if final.result is None:
        raise JarvisJobError(
            f"{tool_name} relay dispatch completed without a result",
            reason="jarvis_dispatch_result_missing",
            details={"tool": tool_name, "task_id": final.task_id},
        )
    _raise_inline_delivery_failure(final.task_id, final.result)
    return _structured_payload(final.result)


def _structured_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap relay terminal evidence to the JARVIS tool's structured result."""

    candidates: list[Mapping[str, Any]] = [result]
    mcp_result = result.get("mcp_result")
    if isinstance(mcp_result, Mapping):
        candidates.insert(0, mcp_result)
    for candidate in candidates:
        for key in ("structured_result", "structuredContent", "structured_content"):
            structured = candidate.get(key)
            if isinstance(structured, Mapping):
                return dict(structured)
    return dict(result)


def _raise_inline_delivery_failure(task_id: str, value: Any) -> None:
    """Preserve relay's typed oversized-result failure through the owner layer."""

    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 100_000:
            raise JarvisJobError(
                "JARVIS dispatch result exceeded the validation node bound",
                reason="jarvis_dispatch_result_too_complex",
                details={"task_id": task_id, "max_nodes": 100_000},
            )
        if isinstance(current, Mapping):
            delivery = current.get("delivery")
            if (
                isinstance(delivery, Mapping)
                and delivery.get("schema_version") == RELAY_RESULT_DELIVERY_SCHEMA
                and delivery.get("status") == "failed"
                and delivery.get("code") == RELAY_INLINE_LIMIT_CODE
            ):
                raise RelayInlineResultTooLargeError(task_id, delivery)
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _execution_projection(
    payload: Mapping[str, Any],
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the registered execution query onto one stable application view."""

    handle = payload.get("execution_handle")
    record = payload.get("execution_record")
    progress = payload.get("progress")
    typed_handle = handle if isinstance(handle, Mapping) else {}
    typed_record = record if isinstance(record, Mapping) else {}
    typed_progress = progress if isinstance(progress, Mapping) else None

    pipeline_id = _first_text(payload, typed_handle, typed_record, field="pipeline_id")
    execution_id = _first_text(payload, typed_handle, typed_record, field="execution_id")
    expected_pipeline = requested.get("pipeline_id")
    expected_execution = requested.get("execution_id")
    if pipeline_id != expected_pipeline or execution_id != expected_execution:
        raise JarvisJobError(
            "jarvis_get_execution returned a different execution identity",
            reason="jarvis_execution_identity_mismatch",
            details={
                "expected_pipeline_id": expected_pipeline,
                "observed_pipeline_id": pipeline_id,
                "expected_execution_id": expected_execution,
                "observed_execution_id": execution_id,
            },
        )
    state = _first_text(typed_record, payload, typed_progress or {}, field="state")
    if state is None and typed_progress is not None:
        candidate = typed_progress.get("execution_state")
        state = candidate if isinstance(candidate, str) and candidate else None
    if state is None:
        raise JarvisJobError(
            "jarvis_get_execution omitted its lifecycle state",
            reason="jarvis_execution_state_missing",
            details={"pipeline_id": pipeline_id, "execution_id": execution_id},
        )
    terminal = typed_record.get("terminal")
    if not isinstance(terminal, bool) and typed_progress is not None:
        terminal = typed_progress.get("terminal")
    if not isinstance(terminal, bool):
        terminal = state in {"completed", "failed", "canceled"}

    return {
        "schema_version": "clio-agent.jarvis-execution.v1",
        "pipeline_id": pipeline_id,
        "execution_id": execution_id,
        "state": state,
        "terminal": terminal,
        "progress": progress,
        "artifacts": payload.get("artifact_page", payload.get("artifacts")),
        "services": payload.get("service_runtimes", payload.get("services")),
        "scheduler_native_id": _first_text(
            typed_handle, typed_record, payload, field="scheduler_native_id"
        ),
        "scheduler_provider": _first_text(
            typed_handle, typed_record, payload, field="scheduler_provider"
        ),
    }


def _first_text(*sources: Mapping[str, Any], field: str) -> str | None:
    """Return the first non-empty string field from ordered contract sources."""

    for source in sources:
        value = source.get(field)
        if isinstance(value, str) and value:
            return value
    return None
