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

from clio_agent.tools.jarvis_result_contract import (
    JarvisJobError,
)
from clio_agent.tools.jarvis_result_contract import (
    raise_inline_delivery_failure as _raise_inline_delivery_failure,
)
from clio_agent.tools.jarvis_result_contract import (
    raise_remote_call_failure as _raise_remote_call_failure,
)
from clio_agent.tools.jarvis_result_contract import (
    structured_payload as _structured_payload,
)
from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, TaskKey
from clio_agent.tools.relay_transport import (
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
# The correct-shape local relay door projects the six curated operations above
# under the OPERATOR-REGISTERED jarvis route, not the compact aliases this
# surface was originally built against: the compact names (``jarvis_create_pipeline``,
# ...) are ABSENT from that door's catalog, and only the registered route engages
# relay's input-staging contract (verified live: expected_registered_contract
# "clio-kit-jarvis-user-v3.6", staging proof passed). This is the door-side
# namespace prefix :func:`resolve_jarvis_door_tool_name` composes onto a curated
# name by default -- see
# ``clio_agent.tools.relay_factory.resolve_relay_jarvis_door_namespace`` for the
# config seam that resolves it (file -> ``CLIO_RELAY_JARVIS_DOOR_NAMESPACE`` -> this
# default).
JARVIS_DEFAULT_DOOR_NAMESPACE = "remote_jarvis"
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
    "JARVIS_DEFAULT_DOOR_NAMESPACE",
    "JARVIS_EXECUTION_OUTPUT_SCHEMA",
    "JARVIS_NAMESPACE",
    "JARVIS_RUN_HANDLE_OUTPUT_SCHEMA",
    "JARVIS_TOOL_NAMES",
    "JarvisJobError",
    "JarvisJobs",
    "JarvisRunHandle",
    "resolve_jarvis_door_tool_name",
]


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
# ``timeout_seconds`` is deliberately absent: an undescribed budget an agent had
# to guess at, where one real dispatch costs minutes and a durable job cannot
# finish sooner for being observed less. See :func:`_split_dispatch_budget`.
_CONTROL_PROPERTIES = {
    "idempotency_key": {"type": "string", "minLength": 1},
}
# The exact artifact-page filter JARVIS accepts, mirrored from the relay-advertised
# ``jarvis_get_execution`` input schema (captured live off clio-relay's tools/list,
# #1195). Declaring it closed is the root fix for a caller inventing a filter key:
# an opaque ``{"type": "object"}`` gave the model no contract, so a plausible-looking
# ``include_content`` reached JARVIS and came back as a remote pydantic
# ``extra_forbidden`` rejection. The page is a manifest of artifact RECORDS --
# identity, role, state, and location -- and carries no artifact content.
_ARTIFACT_PAGE_FILTER: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional filters for one bounded page of execution artifact records "
        "(identity, role, state, location). This page never carries artifact content."
    ),
    "properties": {
        "artifact_id": {
            "anyOf": [{"type": "string", "maxLength": 90}, {"type": "null"}],
            "default": None,
            "description": "Exact opaque JARVIS artifact ID filter.",
        },
        "package_id": {
            "anyOf": [{"type": "string", "maxLength": 256}, {"type": "null"}],
            "default": None,
            "description": "Exact JARVIS package alias filter.",
        },
        "role": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": [
                        "intermediate",
                        "output",
                        "log",
                        "checkpoint",
                        "provenance",
                        "validation",
                    ],
                },
                {"type": "null"},
            ],
            "default": None,
        },
        "state": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": ["producing", "available", "finalized", "incomplete", "failed"],
                },
                {"type": "null"},
            ],
            "default": None,
        },
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        "cursor": {
            "anyOf": [{"type": "string", "maxLength": 1024}, {"type": "null"}],
            "default": None,
            "description": "Opaque next-page cursor.",
        },
    },
    "additionalProperties": False,
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
            "artifacts": {"anyOf": [_ARTIFACT_PAGE_FILTER, {"type": "null"}], "default": None},
            **_CONTROL_PROPERTIES,
        },
        "required": ["cluster", "pipeline_id", "execution_id"],
        "additionalProperties": False,
    },
}

# Plain human names for the six curated operations. Without a title the UI head
# renders the raw wire name (``jarvis_add_step``); the surrounding UI supplies
# arguments, so a title never carries parentheses.
_TITLES = {
    "jarvis_create_pipeline": "Create Pipeline",
    "jarvis_describe": "Describe",
    "jarvis_add_step": "Add Step",
    "jarvis_edit_step": "Edit Step",
    "jarvis_run": "Run Pipeline",
    "jarvis_get_execution": "Get Execution",
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
        "artifacts, services, and scheduler-native identity. Pass `artifacts` to "
        "add one bounded page of artifact records; that page lists each artifact's "
        "identity, role, state, and location, and never its content."
    ),
}


def _with_cluster_hint(description: str, cluster_hint: str | None) -> str:
    """Append one cluster-identity sentence when ``CLIO_RELAY_CLUSTER`` is configured.

    Every curated JARVIS tool declares a required ``cluster`` input (see
    ``_INPUT_SCHEMAS``), so the sentence tells the calling agent the exact
    value to pass -- composed once here from the resolved config value
    (``relay.cluster`` / ``CLIO_RELAY_CLUSTER``, see
    ``clio_agent.tools.relay_factory.resolve_relay_cluster``), never guessed
    or inferred from prose. Unset (``cluster_hint`` falsy) leaves the
    description byte-identical to ``_DESCRIPTIONS`` -- no placeholder text,
    per the no-silent-fallback rule.
    """

    if not cluster_hint:
        return description
    return (
        f"{description} This deployment's registered cluster is "
        f"{cluster_hint!r}; pass it as `cluster` verbatim."
    )


def resolve_jarvis_door_tool_name(curated_name: str, door_namespace: str) -> str:
    """Map one curated JARVIS operation to its configured relay door tool name.

    The curated (agent-facing) name is stable -- ``jarvis_create_pipeline`` and
    its five siblings never change. What changes is the wire name this surface
    dispatches to relay's MCP door under, which is why the mapping is driven by
    config rather than a hardcoded literal (see ``JARVIS_DEFAULT_DOOR_NAMESPACE``
    for the door-shape rationale). ``door_namespace`` is a simple prefix: the
    registered-route default ``"remote_jarvis"`` composes
    ``"remote_jarvis_jarvis_create_pipeline"``; an empty namespace reproduces the
    OLD compact door name (``"jarvis_create_pipeline"``) verbatim -- the only way
    the compact door is ever expressed, never a second hardcoded branch.
    """

    if curated_name not in JARVIS_TOOL_NAMES:
        raise ValueError(f"unsupported curated JARVIS tool: {curated_name!r}")
    if not door_namespace:
        return curated_name
    return f"{door_namespace}_{curated_name}"


class _ProjectedJarvisTool(Tool):
    """One curated operation exposed below the gateway's jarvis mount."""

    def __init__(self, name: str, owner: "JarvisJobs", *, cluster_hint: str | None = None) -> None:
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
            title=_TITLES[name],
            description=_with_cluster_hint(_DESCRIPTIONS[name], cluster_hint),
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


def _split_dispatch_budget(
    arguments: Mapping[str, Any], default_seconds: float
) -> tuple[dict[str, Any], float]:
    """Consume a programmatic caller's ``timeout_seconds``, keeping it off the wire.

    It bounds one dispatch and was never a door argument: the registered JARVIS
    route's schema is closed and spells the same bound ``wait_timeout_seconds``,
    so forwarding it verbatim failed every dispatch pre-flight with
    ``relay_arguments_invalid``. Agents no longer see it -- ``_CONTROL_PROPERTIES``.
    """

    payload = dict(arguments)
    if "timeout_seconds" not in payload:
        return payload, default_seconds
    raw = payload.pop("timeout_seconds")
    budget = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        budget = float(raw)
    if budget is None or budget <= 0:
        raise JarvisJobError(
            "timeout_seconds must be a positive number of seconds",
            reason="jarvis_timeout_seconds_invalid",
            details={"timeout_seconds": raw},
        )
    return payload, budget


class JarvisJobs:
    """Six application-level JARVIS tools composed over RelayTransportClient."""

    def __init__(
        self,
        client_factory: JarvisClientFactory,
        *,
        poll_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dispatch_timeout_seconds: float = 600.0,
        request_timeout_seconds: float | None = None,
        cluster_hint: str | None = None,
        door_namespace: str = JARVIS_DEFAULT_DOOR_NAMESPACE,
    ) -> None:
        """Construct the six curated JARVIS tools over one relay client factory.

        Args:
            client_factory: Opens one owner-bound relay transport client.
            poll_sleep: Injected clock for the bounded-dispatch poll loop.
            dispatch_timeout_seconds: Budget for create/deploy/execution-query
                dispatches driven to terminal.
            request_timeout_seconds: Budget for the ``jarvis_run`` submit,
                defaulting to ``dispatch_timeout_seconds``. That submit is
                fire-and-forget in JOB terms only; its ``tools/call`` travels the
                same SSH-relayed transport as every other operation, so the old
                flat 30s made ``jarvis_run`` impossible on a remote deployment
                (live: two submits abandoned at ~51s where a call costs ~200s).
            cluster_hint: Resolved ``relay.cluster`` value stamped into every
                curated tool's description, or ``None`` when unset.
            door_namespace: Resolved ``relay.jarvis_door_namespace`` value this
                surface dispatches curated operations under (see
                :func:`resolve_jarvis_door_tool_name`). Defaults to the
                registered-route namespace; an empty string dispatches the old
                compact door names instead.
        """

        if request_timeout_seconds is None:
            request_timeout_seconds = dispatch_timeout_seconds
        if dispatch_timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("JARVIS dispatch and request timeouts must be positive")
        self._client_factory = client_factory
        self._poll_sleep = poll_sleep
        self._dispatch_timeout_seconds = dispatch_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._cluster_hint = cluster_hint
        self._door_namespace = door_namespace
        server = FastMCP("clio-jarvis-jobs")
        for name in JARVIS_TOOL_NAMES:
            server.add_tool(_ProjectedJarvisTool(name, self, cluster_hint=cluster_hint))
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

        payload, budget = _split_dispatch_budget(arguments, self._request_timeout_seconds)
        forbidden = {"wait", "wait_for_terminal", "wait_timeout_seconds", "poll_seconds"}
        smuggled = sorted(forbidden.intersection(payload))
        if smuggled:
            raise JarvisJobError(
                "jarvis_run does not accept internal wait; use jarvis_get_execution",
                reason="jarvis_run_wait_not_allowed",
                details={"fields": smuggled},
            )
        async with self._client_factory() as relay:
            identity = await self._submit_door_call(
                relay,
                "jarvis_run",
                payload,
                timeout_seconds=budget,
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

        payload, budget = _split_dispatch_budget(arguments, self._dispatch_timeout_seconds)
        payload["wait_for_terminal"] = True
        payload["wait_timeout_seconds"] = budget
        async with self._client_factory() as relay:
            # The create-task call carries ``wait_for_terminal=True`` and the
            # same budget, so the remote side is told it may take that long
            # before replying at all. The client's own read timeout must be at
            # least as generous or it abandons the server-side deadline it just
            # asked for (live: SSH-relayed dispatches routinely take 190-220s).
            identity = await self._submit_door_call(
                relay,
                tool_name,
                payload,
                timeout_seconds=budget,
            )
            final = await self._drive_to_terminal(relay, identity)
        return _terminal_payload(tool_name, final)

    async def _submit_door_call(
        self,
        relay: JarvisRelayClient,
        curated_name: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> RelayTaskIdentity:
        """Submit one dispatch against this instance's configured door tool name.

        No silent fallback: when relay rejects the resolved door name with its
        typed ``relay_tool_not_found`` reason, this re-raises carrying the
        curated operation, the exact door tool name that was dispatched, and the
        configured ``door_namespace`` -- it never retries the other namespace on
        its own (see :func:`resolve_jarvis_door_tool_name`).
        """

        door_name = self._door_tool_name(curated_name)
        try:
            return await relay.submit(door_name, payload, timeout_seconds=timeout_seconds)
        except RelayTransportContractError as exc:
            if exc.reason != "relay_tool_not_found":
                raise
            raise JarvisJobError(
                f"{curated_name} dispatched door tool {door_name!r}, which relay's "
                "catalog does not expose",
                reason="jarvis_door_tool_not_found",
                details={
                    "curated_tool": curated_name,
                    "door_tool": door_name,
                    "door_namespace": self._door_namespace,
                },
            ) from exc

    def _door_tool_name(self, curated_name: str) -> str:
        """Resolve one curated operation to this instance's configured door name."""

        return resolve_jarvis_door_tool_name(curated_name, self._door_namespace)

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
            if current.status == "input_required":
                raise JarvisJobError(
                    f"{identity.task_id} requires input that curated JARVIS tools cannot answer",
                    reason="jarvis_dispatch_input_required_unsupported",
                    details={"task_id": identity.task_id},
                )
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
    _raise_remote_call_failure(tool_name, final.task_id, final.result)
    return _structured_payload(final.result)


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
