"""P2.13 acceptance for durable JARVIS/Spack application jobs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastmcp import Client
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.tools.gateway import build_gateway
from clio_agent.tools.jarvis_jobs import (
    _DESCRIPTIONS,
    JARVIS_RUN_HANDLE_OUTPUT_SCHEMA,
    JarvisJobError,
    JarvisJobs,
    JarvisRunHandle,
    _execution_projection,
    _structured_payload,
)
from clio_agent.tools.mcp_task_records import TaskKey
from clio_agent.tools.relay_transport import (
    RELAY_POLL_INTERVAL_MS,
    RelayInlineResultTooLargeError,
    RelayTaskIdentity,
    RelayTransportContractError,
)


@dataclass(frozen=True)
class _Recipe:
    pipeline_id: str
    steps: tuple[tuple[str, dict[str, Any]], ...]


PARAVIEW_RECIPE = _Recipe(
    "gray-scott-paraview",
    (
        ("benchmark_apps.gray_scott_morphology", {"regime": "feed"}),
        ("builtin.adios2_gray_scott", {"engine": "BP5"}),
        ("builtin.paraview", {"profile": "batch", "frames": 10}),
    ),
)
LAMMPS_RECIPE = _Recipe(
    "lammps-copper-elastic",
    (
        ("benchmark_apps.lammps_copper", {"cells": [4, 4, 4]}),
        ("builtin.lammps", {"mpi_ranks": 4}),
    ),
)


@dataclass
class _Execution:
    pipeline_id: str
    execution_id: str
    ticks: int = 0
    admitted: bool = False
    terminal: bool = False


@dataclass
class _Dispatch:
    identity: RelayTaskIdentity
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    stream_closed: asyncio.Event = field(default_factory=asyncio.Event)
    stream_task: asyncio.Task[None] | None = None


class _FakeJarvisRoute:
    """In-memory relay route enforcing the compact registered JARVIS contract."""

    def __init__(self) -> None:
        self.pipelines: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.executions: dict[str, _Execution] = {}
        self.dispatches: dict[str, _Dispatch] = {}
        self.resume_keys: list[TaskKey] = []
        self.client_ids: list[int] = []
        self._next_job = 0
        self._next_client = 0

    def client(self) -> "_FakeRelayClient":
        self._next_client += 1
        self.client_ids.append(self._next_client)
        return _FakeRelayClient(self, self._next_client)

    async def close(self) -> None:
        """Close every fake stream and prove its task was drained."""

        for dispatch in self.dispatches.values():
            dispatch.terminal.set()
        for dispatch in self.dispatches.values():
            if dispatch.stream_task is not None:
                await dispatch.stream_task
            assert dispatch.stream_closed.is_set()
            assert dispatch.stream_task is not None and dispatch.stream_task.done()

    async def submit(self, tool: str, arguments: Mapping[str, Any]) -> RelayTaskIdentity:
        args = dict(arguments)
        pipeline_id = str(args.get("pipeline_id", ""))
        if tool == "jarvis_run" and "wait" in args:
            raise RelayTransportContractError(
                "jarvis_run does not accept internal wait",
                reason="jarvis_run_wait_not_allowed",
                details={"field": "wait"},
            )
        manifest = args.get("jarvis_input_manifest")
        if tool == "jarvis_run" and isinstance(manifest, Mapping):
            route = manifest.get("route")
            registered = route.get("pipeline_id") if isinstance(route, Mapping) else None
            if registered != pipeline_id:
                raise RelayTransportContractError(
                    "JARVIS input manifest does not match the registered pipeline",
                    reason="jarvis_input_manifest_pipeline_mismatch",
                    details={"pipeline_id": pipeline_id, "registered_pipeline_id": registered},
                )
        if tool not in {"jarvis_create_pipeline", "jarvis_describe"} and (
            pipeline_id not in self.pipelines
        ):
            raise RelayTransportContractError(
                f"unknown pipeline: {pipeline_id}",
                reason="jarvis_pipeline_unknown",
                details={"pipeline_id": pipeline_id},
            )
        self._next_job += 1
        task_id = f"jarvis-job-{self._next_job}"
        identity = RelayTaskIdentity.from_key(TaskKey("fake-relay", "session-alice", task_id))
        dispatch = _Dispatch(identity, tool, args)
        dispatch.stream_task = asyncio.create_task(self._stream(dispatch))
        self.dispatches[task_id] = dispatch
        self._prepare(dispatch)
        return identity

    async def poll(self, identity: RelayTaskIdentity) -> ClientGetTaskResult:
        dispatch = self._dispatch(identity.key)
        status: Literal["working", "completed"] = (
            "completed" if dispatch.terminal.is_set() else "working"
        )
        return _task_result(dispatch, status=status)

    async def resume(self, key: TaskKey) -> ClientGetTaskResult:
        self.resume_keys.append(key)
        dispatch = self._dispatch(key)
        dispatch.terminal.set()
        return _task_result(dispatch, status="completed")

    def _dispatch(self, key: TaskKey) -> _Dispatch:
        try:
            return self.dispatches[key.task_id]
        except KeyError as exc:
            raise RelayTransportContractError(
                f"relay task {key.task_id!r} has no persisted record",
                reason="relay_task_record_missing",
                details=key.to_wire(),
            ) from exc

    def _prepare(self, dispatch: _Dispatch) -> None:
        tool, args = dispatch.tool, dispatch.arguments
        pipeline_id = str(args.get("pipeline_id", ""))
        if tool == "jarvis_create_pipeline":
            if pipeline_id == "fail-deploy":
                dispatch.error = {"code": "jarvis_deploy_failed", "message": "Spack failed"}
            else:
                self.pipelines[pipeline_id] = []
                dispatch.result = {"pipeline_id": pipeline_id, "created": True}
            dispatch.terminal.set()
        elif tool == "jarvis_describe":
            if args.get("target") == "oversized":
                dispatch.result = {
                    "delivery": {
                        "schema_version": "clio-relay.mcp-result-delivery.v1",
                        "status": "failed",
                        "code": "inline_result_limit_exceeded",
                        "message": "result exceeded the inline limit",
                    }
                }
            else:
                dispatch.result = {"result": {"pipeline_id": pipeline_id}}
            dispatch.terminal.set()
        elif tool == "jarvis_add_step":
            package = str(args["package_name"])
            self.pipelines[pipeline_id].append((package, dict(args.get("config") or {})))
            dispatch.result = {
                "pipeline_id": pipeline_id,
                "step_id": f"step-{len(self.pipelines[pipeline_id])}",
            }
            dispatch.terminal.set()
        elif tool == "jarvis_edit_step":
            dispatch.result = {"pipeline_id": pipeline_id, "edited": True}
            dispatch.terminal.set()
        elif tool == "jarvis_run":
            execution_id = str(args.get("execution_id") or f"execution-{len(self.executions) + 1}")
            self.executions[execution_id] = _Execution(pipeline_id, execution_id)
            dispatch.result = {
                "pipeline_id": pipeline_id,
                "execution_id": execution_id,
                "status": "submitted",
            }
        elif tool == "jarvis_get_execution":
            execution_id = str(args["execution_id"])
            execution = self.executions.get(execution_id)
            if execution is None:
                dispatch.error = {
                    "code": "jarvis_execution_unknown",
                    "execution_id": execution_id,
                }
            else:
                execution.ticks += 1
                execution.admitted = execution.ticks >= 2
                execution.terminal = execution.ticks >= 3
                dispatch.result = _execution_result(execution)
            dispatch.terminal.set()

    @staticmethod
    async def _stream(dispatch: _Dispatch) -> None:
        try:
            await dispatch.terminal.wait()
        finally:
            dispatch.stream_closed.set()


class _FakeRelayClient:
    def __init__(self, route: _FakeJarvisRoute, client_id: int) -> None:
        self.route = route
        self.client_id = client_id

    async def __aenter__(self) -> "_FakeRelayClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def submit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RelayTaskIdentity:
        del idempotency_key, timeout_seconds
        return await self.route.submit(tool_name, arguments or {})

    async def poll(self, task: RelayTaskIdentity) -> ClientGetTaskResult:
        return await self.route.poll(task)

    async def resume(
        self, key: TaskKey, *, timeout_seconds: float | None = None
    ) -> ClientGetTaskResult:
        del timeout_seconds
        return await self.route.resume(key)


def _task_result(
    dispatch: _Dispatch,
    *,
    status: Literal["working", "completed"],
) -> ClientGetTaskResult:
    return ClientGetTaskResult(
        taskId=dispatch.identity.task_id,
        status="failed" if dispatch.error is not None else status,
        createdAt="2026-08-01T00:00:00Z",
        lastUpdatedAt="2026-08-01T00:00:01Z",
        pollIntervalMs=RELAY_POLL_INTERVAL_MS,
        resultType="complete",
        result=dispatch.result,
        error=dispatch.error,
    )


def _execution_result(execution: _Execution) -> dict[str, Any]:
    state = "completed" if execution.terminal else "running"
    native_id = "22567" if execution.admitted else None
    return {
        "schema_version": "clio-kit.jarvis-execution.v2",
        "pipeline_id": execution.pipeline_id,
        "execution_id": execution.execution_id,
        "execution_handle": {
            "scheduler_provider": "slurm",
            "scheduler_native_id": native_id,
        },
        "execution_record": {"state": state, "terminal": execution.terminal},
        "progress": {
            "execution_state": state,
            "terminal": execution.terminal,
            "packages": [
                {
                    "package_id": "builtin.paraview",
                    "event_count": execution.ticks,
                    "latest": {"sequence": execution.ticks, "state": state},
                }
            ],
        },
        "artifact_page": {
            "artifacts": [{"artifact_id": "artifact-final-frame", "role": "rendered-comparison"}]
        },
        "service_runtimes": {
            "service_runtimes": [
                {"service_instance_id": "pvserver-1", "endpoint": "https://pv.invalid"}
            ]
        },
    }


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def fake_route() -> AsyncIterator[_FakeJarvisRoute]:
    route = _FakeJarvisRoute()
    try:
        yield route
    finally:
        await route.close()


def _surface(
    route: _FakeJarvisRoute,
    poll_sleep: Callable[[float], Awaitable[None]] = _no_sleep,
) -> JarvisJobs:
    return JarvisJobs(route.client, poll_sleep=poll_sleep)


async def _deploy(surface: JarvisJobs, recipe: _Recipe) -> None:
    await surface.create_pipeline({"cluster": "ares", "pipeline_id": recipe.pipeline_id})
    for package_name, config in recipe.steps:
        await surface.add_step(
            {
                "cluster": "ares",
                "pipeline_id": recipe.pipeline_id,
                "package_name": package_name,
                "config": config,
            }
        )


@pytest.mark.asyncio
async def test_jarvis_run_is_handle_first_and_progress_accrues(
    fake_route: _FakeJarvisRoute,
) -> None:
    """FAILING-FIRST: dispatch returns while the JARVIS workload is still running."""

    surface = _surface(fake_route)
    await _deploy(surface, PARAVIEW_RECIPE)
    handle = await surface.run(
        {
            "cluster": "ares",
            "pipeline_id": PARAVIEW_RECIPE.pipeline_id,
            "execution_id": "paraview-execution",
            "spack_specs": ["paraview@5.13.3", "adios2@2.10"],
        }
    )

    assert handle.to_wire() == {
        "task_id": handle.task_id,
        "job_id": handle.task_id,
        "kind": "jarvis",
        "state": "queued",
        "terminal": False,
    }
    assert fake_route.executions["paraview-execution"].terminal is False
    query = {
        "cluster": "ares",
        "pipeline_id": PARAVIEW_RECIPE.pipeline_id,
        "execution_id": "paraview-execution",
        "include_progress": True,
        "include_service_runtimes": True,
    }
    first = await surface.get_execution(query)
    second = await surface.get_execution(query)

    assert first["progress"]["packages"][0]["event_count"] == 1
    assert second["progress"]["packages"][0]["event_count"] == 2
    assert first["scheduler_native_id"] is None
    assert second["scheduler_native_id"] == "22567"
    assert second["scheduler_provider"] == "slurm"
    assert second["artifacts"]["artifacts"][0]["artifact_id"] == "artifact-final-frame"
    assert second["services"]["service_runtimes"][0]["service_instance_id"] == "pvserver-1"
    assert fake_route.executions["paraview-execution"].terminal is False


@pytest.mark.asyncio
async def test_detached_client_resumes_dispatch_then_observes_continuing_execution(
    fake_route: _FakeJarvisRoute,
) -> None:
    surface_a = _surface(fake_route)
    await _deploy(surface_a, LAMMPS_RECIPE)
    handle = await surface_a.run(
        {
            "cluster": "ares",
            "pipeline_id": LAMMPS_RECIPE.pipeline_id,
            "execution_id": "lammps-execution",
            "spack_specs": ["lammps@20260704"],
        }
    )

    surface_b = _surface(fake_route)
    dispatch = await surface_b.resume_run(handle.identity)
    observation = await surface_b.get_execution(
        {
            "cluster": "ares",
            "pipeline_id": LAMMPS_RECIPE.pipeline_id,
            "execution_id": dispatch["execution_id"],
        }
    )

    assert fake_route.resume_keys == [handle.identity.key]
    assert len(set(fake_route.client_ids)) == len(fake_route.client_ids)
    assert dispatch["execution_id"] == "lammps-execution"
    assert observation["state"] == "running"
    assert observation["terminal"] is False


@pytest.mark.asyncio
async def test_curated_surface_has_six_agent_story_tools_and_handle_schema(
    fake_route: _FakeJarvisRoute,
) -> None:
    surface = _surface(fake_route)
    gateway = build_gateway({}, jarvis_jobs=surface)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    names = {name for name in listed if name.startswith("jarvis_")}
    assert names == {
        "jarvis_create_pipeline",
        "jarvis_describe",
        "jarvis_add_step",
        "jarvis_edit_step",
        "jarvis_run",
        "jarvis_get_execution",
    }
    run = listed["jarvis_run"]
    assert run.output_schema == JARVIS_RUN_HANDLE_OUTPUT_SCHEMA
    assert "wait" not in run.input_schema["properties"]
    assert all("Use this when" in listed[name].description for name in names)


@pytest.mark.asyncio
async def test_cluster_hint_stamps_all_six_descriptions(fake_route: _FakeJarvisRoute) -> None:
    """FAILING-FIRST (#1171 cluster-discovery gap): CLIO_RELAY_CLUSTER's resolved
    value reaches every curated JARVIS tool's description verbatim, composed once
    at construction from the config value -- never via prose/keyword inference on
    a model's own output."""

    surface = JarvisJobs(fake_route.client, poll_sleep=_no_sleep, cluster_hint="ares-p5run2")
    gateway = build_gateway({}, jarvis_jobs=surface)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    names = {name for name in listed if name.startswith("jarvis_")}
    assert names == {
        "jarvis_create_pipeline",
        "jarvis_describe",
        "jarvis_add_step",
        "jarvis_edit_step",
        "jarvis_run",
        "jarvis_get_execution",
    }
    for name in names:
        description = listed[name].description
        assert description.startswith(_DESCRIPTIONS[name])
        assert "This deployment's registered cluster is 'ares-p5run2'" in description
        assert "pass it as `cluster` verbatim" in description


@pytest.mark.asyncio
async def test_cluster_hint_unset_leaves_descriptions_byte_identical(
    fake_route: _FakeJarvisRoute,
) -> None:
    """Unset CLIO_RELAY_CLUSTER (cluster_hint=None, the default) -> no placeholder,
    no cluster sentence -- descriptions stay byte-identical to the static catalog
    (no-silent-fallback: nothing else about the description changes)."""

    surface = _surface(fake_route)
    gateway = build_gateway({}, jarvis_jobs=surface)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    names = {name for name in listed if name.startswith("jarvis_")}
    for name in names:
        assert listed[name].description == _DESCRIPTIONS[name]
        assert "registered cluster" not in listed[name].description


@pytest.mark.asyncio
async def test_wrong_inputs_and_terminal_failures_remain_typed(
    fake_route: _FakeJarvisRoute,
) -> None:
    surface = _surface(fake_route)

    with pytest.raises(RelayTransportContractError) as unknown_pipeline:
        await surface.run({"cluster": "ares", "pipeline_id": "missing"})
    assert unknown_pipeline.value.reason == "jarvis_pipeline_unknown"

    with pytest.raises(JarvisJobError) as smuggled_wait:
        await surface.run({"cluster": "ares", "pipeline_id": "missing", "wait": True})
    assert smuggled_wait.value.reason == "jarvis_run_wait_not_allowed"

    await surface.create_pipeline({"cluster": "ares", "pipeline_id": "query-errors"})
    with pytest.raises(RelayTransportContractError) as manifest_mismatch:
        await surface.run(
            {
                "cluster": "ares",
                "pipeline_id": "query-errors",
                "jarvis_input_manifest": {"route": {"pipeline_id": "other-pipeline"}},
            }
        )
    assert manifest_mismatch.value.reason == "jarvis_input_manifest_pipeline_mismatch"

    with pytest.raises(JarvisJobError) as unknown_execution:
        await surface.get_execution(
            {
                "cluster": "ares",
                "pipeline_id": "query-errors",
                "execution_id": "missing-execution",
            }
        )
    assert unknown_execution.value.reason == "jarvis_dispatch_failed"
    assert unknown_execution.value.details["relay_error"]["code"] == "jarvis_execution_unknown"

    with pytest.raises(JarvisJobError) as failed_deploy:
        await surface.create_pipeline({"cluster": "ares", "pipeline_id": "fail-deploy"})
    assert failed_deploy.value.reason == "jarvis_dispatch_failed"
    assert failed_deploy.value.details["relay_error"]["code"] == "jarvis_deploy_failed"


@pytest.mark.asyncio
async def test_bounded_dispatch_reports_input_required_without_waiting_for_timeout() -> None:
    """Finding 11: an unsupported input round is typed on first observation."""

    class InputRequiredRelay:
        def __init__(self) -> None:
            self.polls = 0

        async def poll(self, _identity: RelayTaskIdentity) -> Any:
            self.polls += 1
            return SimpleNamespace(
                status="input_required",
                poll_interval_ms=RELAY_POLL_INTERVAL_MS,
                input_required={"schema": {"type": "object"}},
            )

    relay = InputRequiredRelay()
    surface = JarvisJobs(
        lambda: None,  # type: ignore[arg-type]
        poll_sleep=asyncio.sleep,
        dispatch_timeout_seconds=0.01,
    )
    identity = RelayTaskIdentity.from_key(
        TaskKey("fake-relay", "session-alice", "jarvis-input-required")
    )

    with pytest.raises(JarvisJobError) as raised:
        await surface._drive_to_terminal(relay, identity)  # type: ignore[arg-type]

    assert raised.value.reason == "jarvis_dispatch_input_required_unsupported"
    assert relay.polls == 1


@pytest.mark.asyncio
async def test_oversize_and_unknown_resume_keep_relay_error_types(
    fake_route: _FakeJarvisRoute,
) -> None:
    surface = _surface(fake_route)
    with pytest.raises(RelayInlineResultTooLargeError):
        await surface.describe({"cluster": "ares", "target": "oversized"})

    missing_identity = RelayTaskIdentity.from_key(
        TaskKey("fake-relay", "session-alice", "unknown-task")
    )
    missing = JarvisRunHandle(identity=missing_identity)
    with pytest.raises(RelayTransportContractError) as unknown_task:
        await surface.resume_run(missing)
    assert unknown_task.value.reason == "relay_task_record_missing"


def _relay_job_envelope(
    *,
    pipeline_id: str,
    execution_id: str,
    scheduler_native_id: str,
) -> dict[str, Any]:
    """Build a clio-relay job envelope shaped exactly like the live #1171 wire capture.

    Top-level keys mirror the exact production shape observed on the live
    wire (verified against a captured ``jarvis_get_execution`` relay job
    record): ``artifacts``, ``job``, ``last_error``, ``mcp_result``,
    ``mcp_result_artifact``, ``observation``, ``relay_queue``, ``scheduler``,
    ``terminal``, ``transform``. The JARVIS tool's own structured output does
    NOT live at this level -- it sits one hop deeper, at
    ``envelope["mcp_result"]["structured_result"]``.
    """

    return {
        "mcp_result_artifact": {
            "artifact_id": "artifact_test",
            "job_id": "job_test",
            "kind": "mcp_result",
            "size_bytes": 1234,
            "sha256": "deadbeef",
            "created_at": "2026-08-05T13:56:17.080847Z",
        },
        "terminal": True,
        "last_error": None,
        "mcp_result": {
            "operation": "tools/call",
            "tool": "jarvis_get_execution",
            "returncode": 0,
            "timed_out": False,
            "protocol_error": None,
            "structured_result": {
                "schema_version": "clio-kit.jarvis-execution.v2",
                "pipeline_id": pipeline_id,
                "execution_id": execution_id,
                "execution_handle": {
                    "pipeline_id": pipeline_id,
                    "execution_id": execution_id,
                    "scheduler_native_id": scheduler_native_id,
                    "scheduler_provider": "slurm",
                    "schema_version": "jarvis.execution.handle.v1",
                },
                "execution_record": {"state": "completed", "terminal": True},
                "progress": {"execution_state": "completed", "terminal": True},
                "artifact_page": None,
                "service_runtimes": None,
            },
            "protocol_version": "2024-11-05",
            "server_info": {"name": "jarvis", "version": "3.4.5"},
            "result_validation": None,
        },
        "transform": None,
        "relay_queue": {"state": "succeeded", "jobs_ahead": None, "position": None},
        "scheduler": [],
        "observation": {
            "outcome": "terminal",
            "scheduler_action": "none",
            "relay_action": "none",
        },
        "job": {"job_id": "job_test", "state": "succeeded", "kind": "mcp_call"},
        "artifacts": [],
    }


def _tasks_get_structured_content_wrapper(envelope: dict[str, Any]) -> dict[str, Any]:
    """Wrap a job envelope the way a resumed/replayed ``tasks/get`` reply does.

    ``structuredContent`` here names clio-relay's own durable job record, not
    the JARVIS tool's structured output -- the key collision (#1171) that let
    ``_structured_payload`` match on the generic key name and return the
    envelope untouched instead of descending one more hop.
    """

    return {
        "content": [{"type": "text", "text": json.dumps(envelope)}],
        "structuredContent": envelope,
        "isError": False,
        "resultType": "complete",
    }


def test_structured_payload_unwraps_relay_job_envelope_from_structured_content() -> None:
    """FAILING-FIRST (#1171): live wire evidence showed ``jarvis_get_execution``
    always raising ``jarvis_execution_identity_mismatch`` with
    ``pipeline_id: None`` / ``execution_id: None``. Root cause: a resumed
    ``tasks/get`` reply wraps the relay job envelope under
    ``structuredContent`` -- the exact key JARVIS's own direct result also
    uses -- so the unwrap must detect the envelope shape (``mcp_result`` plus
    a ``job``/``relay_queue`` sibling) and descend one further hop into its
    ``mcp_result.structured_result`` instead of returning clio-relay's
    bookkeeping untouched.
    """

    envelope = _relay_job_envelope(
        pipeline_id="cu-eam-elastic-v2",
        execution_id="jarvis_7999c467cfb94ac4826b73c78f38a709",
        scheduler_native_id="22827",
    )
    wire = _tasks_get_structured_content_wrapper(envelope)

    payload = _structured_payload(wire)

    assert payload["pipeline_id"] == "cu-eam-elastic-v2"
    assert payload["execution_id"] == "jarvis_7999c467cfb94ac4826b73c78f38a709"
    assert payload["execution_handle"]["scheduler_native_id"] == "22827"
    # The raw envelope's own relay bookkeeping must never leak through.
    assert "job" not in payload
    assert "relay_queue" not in payload
    assert "mcp_result" not in payload


def test_execution_projection_resolves_identity_through_relay_job_envelope() -> None:
    """The full get_execution projection succeeds once the envelope is unwrapped."""

    envelope = _relay_job_envelope(
        pipeline_id="cu-eam-elastic-v2",
        execution_id="jarvis_7999c467cfb94ac4826b73c78f38a709",
        scheduler_native_id="22827",
    )
    wire = _tasks_get_structured_content_wrapper(envelope)
    requested = {
        "pipeline_id": "cu-eam-elastic-v2",
        "execution_id": "jarvis_7999c467cfb94ac4826b73c78f38a709",
    }

    projected = _execution_projection(_structured_payload(wire), requested)

    assert projected["pipeline_id"] == "cu-eam-elastic-v2"
    assert projected["execution_id"] == "jarvis_7999c467cfb94ac4826b73c78f38a709"
    assert projected["scheduler_native_id"] == "22827"
    assert projected["scheduler_provider"] == "slurm"


def test_structured_payload_direct_payload_regression() -> None:
    """Direct (already-unwrapped) payloads must keep working unchanged.

    Covers both direct shapes the curated tools rely on: a flat structured
    result with no wrapping at all (create/describe/add_step/edit_step,
    and jarvis_get_execution's own already-projected fields), and a relay
    job envelope handed straight as ``result`` with no ``structuredContent``
    wrapper -- resolved via the existing ``mcp_result`` first-candidate path,
    which this fix must not disturb.
    """

    direct = {
        "schema_version": "clio-kit.jarvis-execution.v2",
        "pipeline_id": "direct-pipeline",
        "execution_id": "direct-execution",
        "execution_handle": {"scheduler_native_id": "9001", "scheduler_provider": "slurm"},
        "execution_record": {"state": "running", "terminal": False},
        "progress": {"execution_state": "running", "terminal": False},
    }
    assert _structured_payload(direct) == direct

    envelope = _relay_job_envelope(
        pipeline_id="direct-pipeline",
        execution_id="direct-execution",
        scheduler_native_id="9002",
    )
    payload = _structured_payload(envelope)
    assert payload["pipeline_id"] == "direct-pipeline"
    assert payload["execution_id"] == "direct-execution"
    assert payload["execution_handle"]["scheduler_native_id"] == "9002"


def test_structured_payload_both_shapes_miss_keeps_typed_identity_error() -> None:
    """A malformed envelope with no reachable structured_result anywhere must
    not be silently returned as if it carried the identity -- no double-try
    that masks a malformed payload. The caller's typed
    ``jarvis_execution_identity_mismatch`` error must still fire.
    """

    malformed_envelope = {
        # Bears the envelope shape (mcp_result + job/relay_queue siblings)
        # but mcp_result itself carries none of the accepted structured keys.
        "mcp_result": {"operation": "tools/call", "tool": "jarvis_get_execution"},
        "job": {"job_id": "job_test", "state": "succeeded"},
        "relay_queue": {"state": "succeeded"},
        "terminal": True,
        "last_error": None,
    }
    wire = _tasks_get_structured_content_wrapper(malformed_envelope)

    payload = _structured_payload(wire)
    # Neither shape resolved an identity, so the untouched top-level input is
    # returned rather than a guessed/synthesized structured result.
    assert payload == wire
    assert payload.get("pipeline_id") is None

    with pytest.raises(JarvisJobError) as raised:
        _execution_projection(
            payload,
            {"pipeline_id": "cu-eam-elastic-v2", "execution_id": "jarvis_exec"},
        )
    assert raised.value.reason == "jarvis_execution_identity_mismatch"
    assert raised.value.details["observed_pipeline_id"] is None
    assert raised.value.details["observed_execution_id"] is None
