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
    _INPUT_SCHEMAS,
    JARVIS_DEFAULT_DOOR_NAMESPACE,
    JARVIS_RUN_HANDLE_OUTPUT_SCHEMA,
    JARVIS_TOOL_NAMES,
    JarvisJobError,
    JarvisJobs,
    JarvisRunHandle,
    _execution_projection,
    _raise_remote_call_failure,
    _structured_payload,
    _terminal_payload,
    resolve_jarvis_door_tool_name,
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


def _curated_tool_name(door_tool_name: str) -> str:
    """Normalize a dispatched door tool name back to its curated operation.

    ``_FakeJarvisRoute``'s own dispatch logic branches on the CURATED
    operation (create_pipeline/describe/...), independent of which door
    namespace the ``JarvisJobs`` surface under test is configured to dispatch
    through -- mirrors the real relay door's namespaced projection
    (``remote_jarvis_jarvis_create_pipeline``) without pinning one namespace
    into every behavior assertion in this file (the dedicated door-namespace
    tests below pin the exact wire name instead).
    """

    if door_tool_name in JARVIS_TOOL_NAMES:
        return door_tool_name
    prefix = f"{JARVIS_DEFAULT_DOOR_NAMESPACE}_"
    if door_tool_name.startswith(prefix) and door_tool_name[len(prefix) :] in JARVIS_TOOL_NAMES:
        return door_tool_name[len(prefix) :]
    raise AssertionError(f"unrecognized door tool name: {door_tool_name!r}")


class _FakeJarvisRoute:
    """In-memory relay route enforcing the curated JARVIS contract.

    Dispatched door tool names are normalized back to their curated operation
    (:func:`_curated_tool_name`) before any behavior branch runs, so this fake
    exercises whichever door namespace the ``JarvisJobs`` surface under test is
    configured with -- by default the registered-route namespace, the new
    default a fresh ``JarvisJobs()`` dispatches through.
    """

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
        curated = _curated_tool_name(tool)
        args = dict(arguments)
        pipeline_id = str(args.get("pipeline_id", ""))
        if curated == "jarvis_run" and "wait" in args:
            raise RelayTransportContractError(
                "jarvis_run does not accept internal wait",
                reason="jarvis_run_wait_not_allowed",
                details={"field": "wait"},
            )
        manifest = args.get("jarvis_input_manifest")
        if curated == "jarvis_run" and isinstance(manifest, Mapping):
            route = manifest.get("route")
            registered = route.get("pipeline_id") if isinstance(route, Mapping) else None
            if registered != pipeline_id:
                raise RelayTransportContractError(
                    "JARVIS input manifest does not match the registered pipeline",
                    reason="jarvis_input_manifest_pipeline_mismatch",
                    details={"pipeline_id": pipeline_id, "registered_pipeline_id": registered},
                )
        if curated not in {"jarvis_create_pipeline", "jarvis_describe"} and (
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
        dispatch = _Dispatch(identity, curated, args)
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


class _DeadStreamRelay:
    """A relay whose ``poll`` always dies -- relay#213's stale pooled stream."""

    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, _identity: RelayTaskIdentity) -> ClientGetTaskResult:
        self.polls += 1
        raise RelayTransportContractError(
            "relay task poll died on a stale pooled connection",
            reason="relay_stream_closed",
            details={},
        )


@pytest.mark.asyncio
async def test_drive_to_terminal_consumes_an_already_terminal_submit_without_polling() -> None:
    """FAILING-FIRST (relay#213 unreportability class): a ``wait_for_terminal``
    submit's own SEP-2663 create response can already report a terminal state
    -- relay runs the dispatch to completion server-side before minting the
    claim, so ``status`` is already known before any follow-up round trip.
    Reissuing a ``tasks/get`` to relearn that state is the redundant hop that
    dies on relay's stale pooled connection, converting an already-successful
    (or already-failed) execution into an opaque client-side transport error.
    When the submitted identity already carries a resolved terminal result,
    ``_drive_to_terminal`` must consume it directly and never call ``poll()``
    at all."""

    relay = _DeadStreamRelay()
    surface = JarvisJobs(lambda: None, poll_sleep=asyncio.sleep)  # type: ignore[arg-type]
    terminal = ClientGetTaskResult(
        taskId="jarvis-already-done",
        status="completed",
        createdAt="2026-08-10T23:00:00Z",
        lastUpdatedAt="2026-08-10T23:00:01Z",
        pollIntervalMs=RELAY_POLL_INTERVAL_MS,
        resultType="complete",
        result={
            "pipeline_id": "cu-eam-elastic-v2",
            "execution_id": "jarvis_already_done",
            "state": "completed",
            "terminal": True,
        },
        error=None,
    )
    identity = RelayTaskIdentity(
        key=TaskKey("fake-relay", "session-alice", "jarvis-already-done"),
        job_id="jarvis-already-done",
        mcp_name="jarvis-already-done",
        initial_result=terminal,
    )

    final = await surface._drive_to_terminal(relay, identity)  # type: ignore[arg-type]

    assert final is terminal
    assert relay.polls == 0


@pytest.mark.asyncio
async def test_drive_to_terminal_consumes_an_already_failed_submit_without_polling() -> None:
    """Sibling of the completed case: a create-time ``failed`` status is ALSO
    consumed directly -- a failure needs no additional content to report
    accurately, so this is exactly the #183 unreportability class the fix
    closes (a typed failure reason must reach the caller, not a transport
    crash on the redundant poll that would only relearn "failed")."""

    relay = _DeadStreamRelay()
    surface = JarvisJobs(lambda: None, poll_sleep=asyncio.sleep)  # type: ignore[arg-type]
    terminal = ClientGetTaskResult(
        taskId="jarvis-already-failed",
        status="failed",
        createdAt="2026-08-10T23:00:00Z",
        lastUpdatedAt="2026-08-10T23:00:01Z",
        pollIntervalMs=RELAY_POLL_INTERVAL_MS,
        statusMessage="Relay job is failed",
        resultType="complete",
        result=None,
        error={"message": "Relay job is failed"},
    )
    identity = RelayTaskIdentity(
        key=TaskKey("fake-relay", "session-alice", "jarvis-already-failed"),
        job_id="jarvis-already-failed",
        mcp_name="jarvis-already-failed",
        initial_result=terminal,
    )

    final = await surface._drive_to_terminal(relay, identity)  # type: ignore[arg-type]

    assert final is terminal
    assert relay.polls == 0
    with pytest.raises(JarvisJobError) as raised:
        _terminal_payload("jarvis_describe", final)
    assert raised.value.reason == "jarvis_dispatch_failed"
    assert raised.value.details["relay_error"] == {"message": "Relay job is failed"}


@pytest.mark.asyncio
async def test_drive_to_terminal_still_polls_a_genuinely_non_terminal_submit() -> None:
    """Sibling: a submit with no create-time terminal status (the ordinary,
    unchanged case -- no ``wait_for_terminal``, or one that was still
    ``working`` when its own wait budget elapsed) keeps polling exactly as
    before, until the fake relay itself reports a terminal state."""

    class WorkingThenDoneRelay:
        def __init__(self) -> None:
            self.polls = 0

        async def poll(self, identity: RelayTaskIdentity) -> ClientGetTaskResult:
            self.polls += 1
            status: Literal["working", "completed"] = (
                "completed" if self.polls >= 2 else "working"
            )
            return ClientGetTaskResult(
                taskId=identity.task_id,
                status=status,
                createdAt="2026-08-10T23:00:00Z",
                lastUpdatedAt="2026-08-10T23:00:01Z",
                pollIntervalMs=RELAY_POLL_INTERVAL_MS,
                resultType="complete",
                result={"pipeline_id": "p"} if status == "completed" else None,
                error=None,
            )

    relay = WorkingThenDoneRelay()
    surface = JarvisJobs(lambda: None, poll_sleep=_no_sleep)  # type: ignore[arg-type]
    identity = RelayTaskIdentity.from_key(
        TaskKey("fake-relay", "session-alice", "jarvis-still-working")
    )
    assert identity.initial_result is None

    final = await surface._drive_to_terminal(relay, identity)  # type: ignore[arg-type]

    assert final.status == "completed"
    assert relay.polls == 2


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


# --------------------------------------------------------------------------- #
# Door tool name resolution (P5 correct-shape door): the door this surface was
# built against exposed compact aliases (jarvis_create_pipeline, ...). The
# correct-shape local relay door projects only the OPERATOR-REGISTERED route
# (remote_jarvis_jarvis_create_pipeline, ...) -- the compact names are ABSENT
# from its catalog. These tests pin the resolved dispatch name reaching
# ``relay.submit`` directly, independent of ``_FakeJarvisRoute``'s own curated
# normalization above.
# --------------------------------------------------------------------------- #


class _CapturingRelay:
    """Records every door tool name a ``JarvisJobs`` dispatch actually submits.

    ``poll`` immediately reports a terminal, benign result (echoing the
    submitted ``pipeline_id``/``execution_id`` so ``get_execution``'s identity
    check passes) so the bounded dispatch path completes in one round trip --
    this fake exists to pin the WIRE NAME reaching relay, not to simulate
    JARVIS job semantics.
    """

    def __init__(self, *, reject: str | None = None) -> None:
        self.submitted: list[str] = []
        self.submitted_arguments: list[dict[str, Any]] = []
        self.submitted_timeouts: list[float | None] = []
        self._reject = reject

    async def __aenter__(self) -> "_CapturingRelay":
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
        del idempotency_key
        self.submitted.append(tool_name)
        self.submitted_arguments.append(dict(arguments or {}))
        self.submitted_timeouts.append(timeout_seconds)
        if self._reject is not None and tool_name == self._reject:
            raise RelayTransportContractError(
                f"relay tool {tool_name!r} was not present in the discovered catalog",
                reason="relay_tool_not_found",
                details={"tool": tool_name},
            )
        return RelayTaskIdentity.from_key(
            TaskKey("fake-relay", "session-alice", f"job-{len(self.submitted)}")
        )

    async def poll(self, task: RelayTaskIdentity) -> ClientGetTaskResult:
        arguments = self.submitted_arguments[-1]
        result = {
            "pipeline_id": arguments.get("pipeline_id"),
            "execution_id": arguments.get("execution_id"),
            "state": "completed",
            "terminal": True,
        }
        return ClientGetTaskResult(
            taskId=task.task_id,
            status="completed",
            createdAt="2026-08-06T00:00:00Z",
            lastUpdatedAt="2026-08-06T00:00:01Z",
            pollIntervalMs=RELAY_POLL_INTERVAL_MS,
            resultType="complete",
            result=result,
            error=None,
        )

    async def resume(
        self, key: TaskKey, *, timeout_seconds: float | None = None
    ) -> ClientGetTaskResult:
        raise AssertionError("resume is not exercised by these dispatch-name tests")


def test_resolve_jarvis_door_tool_name_prefixes_by_default() -> None:
    assert (
        resolve_jarvis_door_tool_name("jarvis_create_pipeline", "remote_jarvis")
        == "remote_jarvis_jarvis_create_pipeline"
    )


def test_resolve_jarvis_door_tool_name_empty_namespace_reproduces_the_compact_door() -> None:
    """The OLD compact door name is expressed ONLY through an empty namespace --
    never a second hardcoded literal anywhere in the module."""

    assert resolve_jarvis_door_tool_name("jarvis_create_pipeline", "") == "jarvis_create_pipeline"


def test_resolve_jarvis_door_tool_name_rejects_a_name_outside_the_curated_six() -> None:
    with pytest.raises(ValueError):
        resolve_jarvis_door_tool_name("jarvis_not_a_real_operation", "remote_jarvis")


@pytest.mark.asyncio
async def test_default_door_namespace_dispatches_registered_route_names_for_every_op() -> None:
    """FAILING-FIRST: before this change every curated dispatch submitted the
    bare curated name (e.g. "jarvis_create_pipeline") straight to relay --
    exactly the compact alias ABSENT from the correct-shape local relay door.
    With no override, every one of the six curated operations must now resolve
    through the registered-route door name (namespace prefix "remote_jarvis")."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep)

    await jobs.create_pipeline({"cluster": "ares", "pipeline_id": "p"})
    await jobs.describe({"cluster": "ares", "target": "packages"})
    await jobs.add_step({"cluster": "ares", "pipeline_id": "p", "package_name": "pkg"})
    await jobs.edit_step({"cluster": "ares", "pipeline_id": "p", "step_id": "s1"})
    await jobs.run({"cluster": "ares", "pipeline_id": "p"})
    await jobs.get_execution({"cluster": "ares", "pipeline_id": "p", "execution_id": "e1"})

    assert relay.submitted == [
        "remote_jarvis_jarvis_create_pipeline",
        "remote_jarvis_jarvis_describe",
        "remote_jarvis_jarvis_add_step",
        "remote_jarvis_jarvis_edit_step",
        "remote_jarvis_jarvis_run",
        "remote_jarvis_jarvis_get_execution",
    ]


@pytest.mark.asyncio
async def test_door_namespace_config_override_dispatches_the_old_compact_names() -> None:
    """The OLD compact door (the p5run2 evidence door used it) stays reachable
    purely through the ``door_namespace`` config value -- config-over-env-vars,
    never a second hardcoded branch."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep, door_namespace="")

    await jobs.create_pipeline({"cluster": "ares", "pipeline_id": "p"})
    await jobs.run({"cluster": "ares", "pipeline_id": "p"})

    assert relay.submitted == ["jarvis_create_pipeline", "jarvis_run"]


@pytest.mark.asyncio
async def test_door_tool_not_found_surfaces_curated_name_and_namespace_without_retry() -> None:
    """No silent fallback: a relay rejection of the configured door name must
    name the curated operation, the exact door tool that was dispatched, and
    the configured namespace -- and must never retry against the other
    namespace on its own (exactly one dispatch attempt reaches relay)."""

    relay = _CapturingRelay(reject="remote_jarvis_jarvis_run")
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep)

    with pytest.raises(JarvisJobError) as raised:
        await jobs.run({"cluster": "ares", "pipeline_id": "p"})

    assert raised.value.reason == "jarvis_door_tool_not_found"
    assert raised.value.details == {
        "reason": "jarvis_door_tool_not_found",
        "curated_tool": "jarvis_run",
        "door_tool": "remote_jarvis_jarvis_run",
        "door_namespace": "remote_jarvis",
    }
    assert relay.submitted == ["remote_jarvis_jarvis_run"]


def test_agent_facing_schemas_do_not_offer_a_dispatch_timeout_knob() -> None:
    """FAILING-FIRST: the curated schemas declared an undescribed
    ``timeout_seconds`` integer that an agent had to guess at, and every guess
    was wrong -- one real registered-route dispatch costs minutes, so a model
    picking 60 then 180 abandoned a describe at 94s and again at 212s and then
    tripped a repeated-failure circuit breaker. A relay dispatch is durable, so
    a shorter caller budget cannot make it faster; it only loses the result.
    The budget is deployment configuration, so the knob must not be on the
    agent-facing surface at all."""

    for name in JARVIS_TOOL_NAMES:
        properties = _INPUT_SCHEMAS[name]["properties"]
        assert "timeout_seconds" not in properties, name
        assert "idempotency_key" in properties, name
        assert _INPUT_SCHEMAS[name]["additionalProperties"] is False, name


@pytest.mark.asyncio
async def test_curated_timeout_seconds_is_consumed_and_never_reaches_the_door() -> None:
    """FAILING-FIRST: ``timeout_seconds`` is THIS surface's declared control knob
    (``_CONTROL_PROPERTIES``), not a relay door argument. The registered JARVIS
    route's discovered inputSchema is closed and has no such property -- it
    spells the same bound ``wait_timeout_seconds`` -- so forwarding the caller's
    value verbatim made EVERY curated dispatch die pre-flight with
    ``relay_arguments_invalid`` (observed live: three consecutive
    jarvis_describe/jarvis_create_pipeline rejections). Consuming it locally is
    what makes the declared knob mean what it says: the budget for this
    dispatch, on both the remote wait and this client's own read timeout."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep)

    await jobs.describe({"cluster": "ares", "target": "packages", "timeout_seconds": 60})

    sent = relay.submitted_arguments[0]
    assert "timeout_seconds" not in sent
    assert sent["wait_for_terminal"] is True
    assert sent["wait_timeout_seconds"] == 60.0
    assert relay.submitted_timeouts[0] == 60.0


@pytest.mark.asyncio
async def test_run_submit_gets_the_full_dispatch_budget_by_default() -> None:
    """FAILING-FIRST: the ``jarvis_run`` submit carried a separate flat 30s
    budget on the theory that it is a quick control call. It is not -- the
    ``tools/call`` travels the same SSH-relayed transport as every other
    operation, where one call costs ~200s, so the submit could never complete
    (observed live: two consecutive submits abandoned at ~51s, then a
    repeated-failure circuit breaker, on a pipeline that was created and
    configured correctly). It defaults to the dispatch budget now, and an
    explicit value still wins."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep, dispatch_timeout_seconds=480.0)
    await jobs.run({"cluster": "ares", "pipeline_id": "p"})
    assert relay.submitted_timeouts[0] == 480.0

    pinned = _CapturingRelay()
    explicit = JarvisJobs(
        lambda: pinned,
        poll_sleep=_no_sleep,
        dispatch_timeout_seconds=480.0,
        request_timeout_seconds=15.0,
    )
    await explicit.run({"cluster": "ares", "pipeline_id": "p"})
    assert pinned.submitted_timeouts[0] == 15.0


@pytest.mark.asyncio
async def test_curated_timeout_seconds_is_consumed_on_the_fire_and_forget_run() -> None:
    """``jarvis_run``'s submit takes the same knob down the same seam -- it must
    not leak into the door payload there either, and it must bound the submit."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep)

    await jobs.run({"cluster": "ares", "pipeline_id": "p", "timeout_seconds": 45})

    sent = relay.submitted_arguments[0]
    assert "timeout_seconds" not in sent
    assert "wait_timeout_seconds" not in sent
    assert relay.submitted_timeouts[0] == 45.0


@pytest.mark.asyncio
async def test_omitted_timeout_seconds_keeps_the_configured_dispatch_budget() -> None:
    """Omitting the knob is unchanged behaviour: the instance's own dispatch
    budget still bounds the call, and no caller value is invented."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep, dispatch_timeout_seconds=123.0)

    await jobs.describe({"cluster": "ares", "target": "packages"})

    assert relay.submitted_arguments[0]["wait_timeout_seconds"] == 123.0
    assert relay.submitted_timeouts[0] == 123.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -5, "soon", None])
async def test_unusable_timeout_seconds_is_a_typed_refusal_not_a_silent_default(
    bad: object,
) -> None:
    """A knob that cannot bound anything must be refused by name -- never
    quietly replaced with the default, which would hide the caller's mistake."""

    relay = _CapturingRelay()
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep)

    with pytest.raises(JarvisJobError) as raised:
        await jobs.describe({"cluster": "ares", "target": "packages", "timeout_seconds": bad})

    assert raised.value.reason == "jarvis_timeout_seconds_invalid"
    assert raised.value.details["timeout_seconds"] == bad
    assert relay.submitted == []


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


def _failed_remote_call_envelope(
    *,
    remote_message: str,
    tool: str = "jarvis_get_execution",
) -> dict[str, Any]:
    """Reproduce clio-relay's envelope for a DELIVERED but FAILED JARVIS call.

    Captured verbatim off the live p5run2 relay (#1195) by calling
    ``jarvis_get_execution`` with ``artifacts={"include_content": true}``
    against a real completed execution. The relay task itself reaches
    ``completed`` -- the dispatch was delivered -- while the job inside it is
    ``failed`` and the remote tool's own rejection sits in
    ``mcp_result.protocol_result``. ``structured_result`` is ``None``, which is
    what previously sent the unwrap down the both-shapes-miss path and produced
    an identity error about the wrong thing.
    """

    return {
        "job": {
            "job_id": "job_705ee07dd8894bcd8416324db328ddb8",
            "cluster": "ares-p5run2",
            "kind": "mcp_call",
            "state": "failed",
            "last_error": "exit code 1",
        },
        "transform": None,
        "relay_queue": {"state": "failed", "jobs_ahead": None, "position": None},
        "scheduler": [],
        "terminal": True,
        "observation": {"outcome": "terminal", "scheduler_action": "none"},
        "last_error": "exit code 1",
        "mcp_result_artifact": {
            "artifact_id": "artifact_b1cf082ecb79496da80321ffe96c0384",
            "kind": "mcp_result",
            "size_bytes": 18475,
        },
        "mcp_result": {
            "operation": "tools/call",
            "tool": tool,
            "returncode": 1,
            "timed_out": False,
            "protocol_error": "tools/call returned isError=true",
            "structured_result": None,
            "protocol_result": {
                "content": [{"text": remote_message, "type": "text"}],
                "isError": True,
            },
            "protocol_version": "2024-11-05",
            "server_info": {"name": "jarvis", "version": "3.4.4"},
            "result_validation": None,
        },
        "artifacts": [],
    }


_REMOTE_REJECTION = (
    "1 validation error for call[jarvis_get_execution_tool]\n"
    "artifacts.include_content\n"
    "  Extra inputs are not permitted [type=extra_forbidden, input_value=True, "
    "input_type=bool]"
)


def test_failed_remote_call_raises_its_own_reason_not_identity_mismatch() -> None:
    """FAILING-FIRST (#1195): the live artifacts-parameterized shape.

    A relay task status of ``completed`` only proves the dispatch was
    delivered. When the JARVIS call inside it failed, this layer must fail with
    the remote tool's own reason -- carrying the message that names the exact
    bad field -- instead of unwrapping an envelope that has no
    ``structured_result`` and blaming execution identity.
    """

    wire = _tasks_get_structured_content_wrapper(
        _failed_remote_call_envelope(remote_message=_REMOTE_REJECTION)
    )

    with pytest.raises(JarvisJobError) as raised:
        _raise_remote_call_failure("jarvis_get_execution", "job_705ee0", wire)

    assert raised.value.reason == "jarvis_remote_call_failed"
    details = raised.value.details
    assert details["job_state"] == "failed"
    assert details["returncode"] == 1
    assert details["protocol_error"] == "tools/call returned isError=true"
    # The remote message reaches the caller verbatim; it is the only text that
    # says which field JARVIS rejected.
    assert "artifacts.include_content" in details["remote_message"]
    assert "extra_forbidden" in details["remote_message"]


def test_failed_remote_call_is_detected_in_every_envelope_carrier() -> None:
    """The same failure must be caught wherever the envelope sits.

    Three carriers reach ``_terminal_payload``: the result IS the envelope; the
    envelope is nested under a structured-result key (a replayed ``tasks/get``
    reply, the #1171 shape); or it is nested under the result's ``mcp_result``
    hop. Shape detection, not position, decides.
    """

    envelope = _failed_remote_call_envelope(remote_message=_REMOTE_REJECTION)
    carriers = {
        "direct_envelope": envelope,
        "replayed_wrapper": _tasks_get_structured_content_wrapper(envelope),
        "snake_case_wrapper": {"structured_result": envelope},
        "mcp_result_hop": {"mcp_result": {"structured_content": envelope}},
    }
    for label, wire in carriers.items():
        with pytest.raises(JarvisJobError) as raised:
            _raise_remote_call_failure("jarvis_get_execution", "job_test", wire)
        assert raised.value.reason == "jarvis_remote_call_failed", label


def test_successful_remote_call_is_never_flagged_as_failed() -> None:
    """A healthy dispatch must pass through untouched.

    Live success evidence carries ``protocol_error: null``, ``returncode: 0``
    and omits ``protocol_result`` altogether, so no successful envelope can
    match the failure evidence.
    """

    healthy = _relay_job_envelope(
        pipeline_id="smoke-hostname-p1",
        execution_id="jarvis_01f33476ad965a1abca1146189464282",
        scheduler_native_id=None,
    )
    for wire in (healthy, _tasks_get_structured_content_wrapper(healthy)):
        _raise_remote_call_failure("jarvis_get_execution", "job_test", wire)
        payload = _structured_payload(wire)
        assert payload["pipeline_id"] == "smoke-hostname-p1"


def test_malformed_envelope_without_failure_evidence_keeps_identity_error() -> None:
    """No failure evidence must not become a fabricated failure.

    The #1171 both-shapes-miss case has no ``protocol_error``, no non-zero
    ``returncode`` and no ``protocol_result``: it is a malformed payload, not a
    remote rejection, so the typed identity error still owns it.
    """

    malformed = {
        "mcp_result": {"operation": "tools/call", "tool": "jarvis_get_execution"},
        "job": {"job_id": "job_test", "state": "succeeded"},
        "relay_queue": {"state": "succeeded"},
        "terminal": True,
    }
    wire = _tasks_get_structured_content_wrapper(malformed)

    _raise_remote_call_failure("jarvis_get_execution", "job_test", wire)

    with pytest.raises(JarvisJobError) as raised:
        _execution_projection(
            _structured_payload(wire),
            {"pipeline_id": "p", "execution_id": "e"},
        )
    assert raised.value.reason == "jarvis_execution_identity_mismatch"


def test_artifacts_filter_schema_is_closed_and_content_free() -> None:
    """FAILING-FIRST (#1195): the schema that let a caller invent a filter key.

    ``artifacts`` was declared as an opaque object, so nothing told a caller
    which filters exist and an invented ``include_content`` only failed after a
    live remote dispatch. The declared filter now mirrors the relay-advertised
    contract exactly and is closed, and no key promises content.
    """

    artifacts = _INPUT_SCHEMAS["jarvis_get_execution"]["properties"]["artifacts"]
    filter_schema = artifacts["anyOf"][0]

    assert filter_schema["additionalProperties"] is False
    assert set(filter_schema["properties"]) == {
        "artifact_id",
        "package_id",
        "role",
        "state",
        "page_size",
        "cursor",
    }
    assert "include_content" not in filter_schema["properties"]
    assert filter_schema["properties"]["page_size"]["maximum"] == 100
    assert set(filter_schema["properties"]["state"]["anyOf"][0]["enum"]) == {
        "producing",
        "available",
        "finalized",
        "incomplete",
        "failed",
    }


class _FailedRemoteCallRelay:
    """Relay client whose dispatch is DELIVERED (task completed) but failed inside."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope
        self.submitted: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FailedRemoteCallRelay":
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
        self.submitted.append((tool_name, dict(arguments or {})))
        return RelayTaskIdentity.from_key(TaskKey("fake-relay", "session-alice", "job_705ee0"))

    async def poll(self, task: RelayTaskIdentity) -> ClientGetTaskResult:
        return ClientGetTaskResult(
            taskId=task.task_id,
            status="completed",
            createdAt="2026-08-06T19:34:57Z",
            lastUpdatedAt="2026-08-06T19:34:57Z",
            pollIntervalMs=RELAY_POLL_INTERVAL_MS,
            resultType="complete",
            result=_tasks_get_structured_content_wrapper(self._envelope) | {"isError": True},
            error=None,
        )

    async def resume(
        self, key: TaskKey, *, timeout_seconds: float | None = None
    ) -> ClientGetTaskResult:
        raise AssertionError("resume is not part of this regression")


@pytest.mark.asyncio
async def test_get_execution_reports_the_remote_reason_the_caller_must_act_on() -> None:
    """FAILING-FIRST (#1195), at the surface the agent actually calls.

    Before the fix this exact wire produced
    ``jarvis_execution_identity_mismatch`` with both observed identities
    ``None`` -- an error naming a problem that did not exist, while the one
    piece of actionable information (JARVIS rejected the ``artifacts`` filter
    key) was discarded. The caller must instead receive the remote reason.
    """

    relay = _FailedRemoteCallRelay(_failed_remote_call_envelope(remote_message=_REMOTE_REJECTION))
    jobs = JarvisJobs(lambda: relay, poll_sleep=_no_sleep)

    with pytest.raises(JarvisJobError) as raised:
        await jobs.get_execution(
            {
                "cluster": "ares-p5run2",
                "pipeline_id": "smoke-hostname-p1",
                "execution_id": "jarvis_01f33476ad965a1abca1146189464282",
                "artifacts": {"artifact_id": "art_F1viBUWePupgP06PTyAQLpmV"},
            }
        )

    assert raised.value.reason == "jarvis_remote_call_failed"
    assert raised.value.reason != "jarvis_execution_identity_mismatch"
    assert "artifacts.include_content" in raised.value.details["remote_message"]
