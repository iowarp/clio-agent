"""Pinned SDK upgrade contracts; these are not live-model task qualification."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any
from uuid import uuid4

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports.base import ClientTransport
from fastmcp.client.transports.config import MCPConfigTransport
from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp_tasks import TasksExtension
from fastmcp_tasks.client_models import (
    ClientCreateTaskResult,
    ClientGetTaskResult,
    GetTaskRequest,
    GetTaskRequestParams,
)
from mcp.client.session import ClientSession

from clio_agent.arc.task_types import CoreReference, argument_digest
from clio_agent.tools.mcp_handlers import MCPClientCapabilities
from clio_agent.tools.mcp_runtime import MCPClientHandlers, make_mcp_client
from clio_agent.tools.native_roots import build_native_roots, resolve_native_root
from clio_agent.tools.task_client import (
    AcceptedNativeCall,
    TaskAdoptionError,
    TaskAwareClient,
    TaskSubmission,
    restore_native_call,
)
from clio_agent.tools.task_receipt import (
    AcceptedTaskResult,
    TaskResultValidationError,
    accepted_result,
    validate_terminal,
)
from clio_agent.tools.task_transport import (
    NamedGetTaskRequest,
    TaskObservationTransport,
)


def submission(
    name: str, *, schema: dict[str, Any] | None = None, arguments: dict[str, Any] | None = None
) -> TaskSubmission:
    """Supply a test boundary reference; production Core custody has separate real-Core cases."""
    return TaskSubmission(
        handle="task_" + uuid4().hex,
        tool_name=name,
        context=CoreReference(session_id="test", scope="test", content_id="test", sha256="test"),
        tool_version=None,
        output_schema=schema,
        argument_digest=argument_digest(arguments or {}),
    )


def test_dependency_contract_is_pinned() -> None:
    """Any dependency upgrade must revisit the adapter's private and public contracts."""
    assert version("fastmcp") == "4.1.2"
    assert version("fastmcp-tasks") == "4.1.2"
    assert version("mcp") == "2.2.0"
    assert version("pydocket") == "0.26.2"


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Actual SDK/native task survives an unanswered cancellation RPC.
async def test_cancel_rpc_deadline_does_not_deadline_accepted_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FastMCP("cancel-health-contract", tasks=True)
    server.add_extension(
        TasksExtension(url="memory://", name="cancel-" + uuid4().hex, concurrency=1)
    )
    started, release, cancel_retired = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @server.tool(task=True)
    async def work() -> dict[str, str]:
        """Keep real accepted native execution active across a failed control RPC."""
        started.set()
        await release.wait()
        return {"result": "deliberately released"}

    accepted: list[AcceptedNativeCall] = []

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        accepted.append(call)
        return accepted_result({"accepted": True, "handle": call.submission.handle, "kind": "MCP"})

    client = TaskAwareClient(server, on_task_accepted=adopt)
    client.health_timeout_s = 0.02
    async with client:
        with client.submission(submission("work")):
            await client.call_tool("work", {})
        assert len(accepted) == 1
        call = accepted[0]
        observer = asyncio.create_task(call.task.result())
        await asyncio.wait_for(started.wait(), 5)
        session, requests = call.task._session, []
        original_request = session.send_request

        async def lost_cancel(request: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(request, "method", "") == "tasks/cancel":
                requests.append(request.params.task_id)
                try:
                    await asyncio.Event().wait()
                finally:
                    cancel_retired.set()
            return await original_request(request, *args, **kwargs)

        monkeypatch.setattr(session, "send_request", lost_cancel)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(call.request_cancel(), 2)
            assert requests == [call.task.task_id] and cancel_retired.is_set()
            assert not observer.done() and not release.is_set()
            client.health_timeout_s = 2
            await client.check_health()
            assert not observer.done()
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.shield(observer), 5)
        assert result.structured_content == {"result": "deliberately released"}


def test_default_client_factory_constructs_the_native_sdk_transport() -> None:
    """The ordinary configured-client route must work without a class override."""
    server = FastMCP("default-client-contract", tasks=True)
    client = make_mcp_client(server)
    assert isinstance(client, Client)
    assert isinstance(client.transport, FastMCPTransport)
    assert client.transport.server is server


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Actual native SDK/server startup and one result observation.
async def test_adapted_native_client_preserves_declared_capabilities_and_task_observation() -> None:
    """The backend must see the configured modes after task observation is installed."""
    server = FastMCP("declared-capabilities-contract", tasks=True)
    server.add_extension(
        TasksExtension(url="memory://", name="capabilities-" + uuid4().hex, concurrency=1)
    )

    @server.tool(task=False)
    async def capabilities(ctx: Context) -> dict[str, Any]:
        """Report what the actual MCP connection advertised to this backend."""
        advertised = ctx.session.client_capabilities
        assert advertised is not None
        return advertised.model_dump(mode="json", by_alias=True, exclude_none=True)

    @server.tool(task=True)
    async def native_work() -> dict[str, int]:
        """Give the native SDK result driver an actual task to observe."""
        return {"value": 7}

    accepted: list[AcceptedNativeCall] = []
    observed: list[str] = []

    async def elicitation(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("No input request is expected")

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        accepted.append(call)
        return accepted_result({"accepted": True, "handle": call.submission.handle, "kind": "MCP"})

    async def observe(session: ClientSession, task_id: str, reply: ClientGetTaskResult) -> None:
        observed.append(task_id)

    async def guard(session: ClientSession, task_id: str, answers: dict[str, Any]) -> None:
        pytest.fail("No answer update is expected")

    configured = make_mcp_client(
        server,
        handlers=MCPClientHandlers(elicitation=elicitation),
        capabilities=MCPClientCapabilities(elicitation_form=True, elicitation_url=False),
    )
    transport = TaskObservationTransport(configured.transport, observe, guard)
    client = TaskAwareClient.from_configured(
        configured, transport, on_task_accepted=adopt, elicitation_handler=elicitation
    )
    async with client:
        with client.submission(submission("capabilities")):
            result = await client.call_tool("capabilities", {})
        assert result.structured_content is not None
        assert result.structured_content["elicitation"] == {"form": {}}
        with client.submission(submission("native_work")):
            receipt = await client.call_tool("native_work", {})
        assert receipt.structured_content is not None and len(accepted) == 1
        settled = await accepted[0].task.result()
        assert settled.structured_content == {"value": 7}
        assert observed and set(observed) == {accepted[0].task.task_id}


def test_configured_client_preserves_protocol_negotiation() -> None:
    async def refuse_acceptance(call: AcceptedNativeCall) -> AcceptedTaskResult:
        pytest.fail("This configuration contract must not submit work")

    configured = Client(FastMCP("configured"), mode="legacy", name="configured-client")
    adapted = TaskAwareClient.from_configured(
        configured,
        configured.transport,
        on_task_accepted=refuse_acceptance,
        elicitation_handler=None,
    )
    assert adapted.mode == configured.mode == "legacy"
    assert adapted.prior_discover is configured.prior_discover


def test_configured_sampling_is_rejected_before_submission() -> None:
    """The Tasks adapter cannot activate CLIO's prohibited MCP sampling surface."""

    async def refuse_acceptance(call: AcceptedNativeCall) -> AcceptedTaskResult:
        pytest.fail("Unsupported client options must be rejected before work is submitted")

    async def sampling(*args: Any, **kwargs: Any) -> str:
        pytest.fail("Configured sampling must never be invoked")

    configured = Client(FastMCP("unsupported-sampling"))
    configured.set_sampling_callback(sampling)
    with pytest.raises(ValueError, match="MCP sampling is unsupported"):
        TaskAwareClient.from_configured(
            configured,
            configured.transport,
            on_task_accepted=refuse_acceptance,
            elicitation_handler=None,
        )


def native_creation() -> ClientCreateTaskResult:
    """An acceptance-shaped value for adapter custody fault injection only."""
    return ClientCreateTaskResult.model_validate(
        {
            "resultType": "task",
            "taskId": "native-custody-test",
            "status": "working",
            "createdAt": "2026-10-13T00:00:00Z",
            "lastUpdatedAt": "2026-10-13T00:00:00Z",
        }
    )


def test_task_routing_does_not_mutate_sdk_request_classes() -> None:
    """Task name routing changes only the request passed to the native SDK."""
    original = GetTaskRequest(params=GetTaskRequestParams(task_id="with-hyphens-123"))
    routed = NamedGetTaskRequest.model_validate(original.model_dump())
    assert original.name_param is None
    assert routed.name_param == "taskId"
    assert routed.model_dump(by_alias=True) == original.model_dump(by_alias=True)


def test_native_roots_have_independent_queues_and_native_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root construction configures real Tasks extensions, without opening a lifespan."""
    from clio_agent import conf

    def settings(key: str, *, env: str, default: Any, cast: Any = None) -> Any:
        return default

    monkeypatch.setattr(conf, "resolve", settings)
    roots = build_native_roots()
    shell, storage = roots["shell"], roots["storage"]
    assert shell.server is not storage.server
    assert shell.settings.queue != storage.settings.queue
    assert shell.extension.docket_settings.concurrency == 10
    assert storage.extension.docket_settings.concurrency == 10
    assert resolve_native_root("shell").queue != shell.settings.queue


def test_native_persistent_and_explicit_queue_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent defaults stay stable; explicit queue names are not rewritten."""
    from clio_agent import conf

    configured: dict[str, Any] = {"gact.tasks.shell.url": "redis://localhost:6379/0"}

    def settings(key: str, *, env: str, default: Any, cast: Any = None) -> Any:
        return configured.get(key, default)

    monkeypatch.setattr(conf, "resolve", settings)
    assert resolve_native_root("shell").queue == "clio-shell"
    configured["gact.tasks.shell.queue"] = "human-chosen-queue"
    assert resolve_native_root("shell").queue == "human-chosen-queue"


@pytest.mark.asyncio
async def test_client_clone_cannot_inherit_a_submission_binding() -> None:
    """An SDK clone cannot silently reuse its caller's accepted operation identity."""

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        pytest.fail("No backend work should be submitted")

    client = TaskAwareClient(FastMCP("unused"), on_task_accepted=adopt)
    with client.submission(submission("work")):
        clone = client.new()
        with pytest.raises(ValueError, match="before tools/call"):
            await clone.call_tool_mcp("work", {})


@pytest.mark.asyncio
async def test_confirmed_adoption_survives_repeated_caller_cancellation() -> None:
    """Unit fault injection: acceptance custody persists without restarting a tool call."""
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = False

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        nonlocal cancelled
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return accepted_result({"accepted": True, "handle": call.submission.handle, "kind": "MCP"})

    client = TaskAwareClient(FastMCP("adoption-unit"), on_task_accepted=adopt)
    creation = ClientCreateTaskResult.model_validate(
        {
            "resultType": "task",
            "taskId": "native-with-hyphens",
            "status": "working",
            "createdAt": "2026-10-13T00:00:00Z",
            "lastUpdatedAt": "2026-10-13T00:00:00Z",
        }
    )
    with client.submission(submission("work")):
        caller = asyncio.create_task(client._resolve_claimed_result("work", creation, None))
    try:
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert not cancelled
        assert len(client._adoptions) == 1
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await client.close()
    assert not client._adoptions


def test_captured_schema_rejects_coercible_backend_output() -> None:
    """Raw output is checked against the accepted schema before SDK coercion."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }
    binding = submission("work", schema=schema)
    schema["properties"]["value"] = {"type": "string"}
    current = ClientGetTaskResult.model_validate(
        {
            "resultType": "complete",
            "taskId": "native",
            "status": "completed",
            "createdAt": "2026-10-13T00:00:00Z",
            "lastUpdatedAt": "2026-10-13T00:00:01Z",
            "result": {"content": [], "structuredContent": {"value": "7"}},
        }
    )
    with pytest.raises(TaskResultValidationError, match="declared schema"):
        validate_terminal(binding.output_schema, current)
    assert current.result is not None
    assert current.result["structuredContent"] == {"value": "7"}


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real FastMCP/Docket startup and deterministic held native execution.
async def test_sdk_acceptance_returns_before_work_and_drives_one_result() -> None:
    """Actual SDK execution is retained; the adapter never adds a result polling loop."""
    server = FastMCP("native-contract", tasks=True)
    server.add_extension(
        TasksExtension(url="memory://", name="contract-" + uuid4().hex, concurrency=1)
    )
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @server.tool(task=True)
    async def held_work() -> dict[str, int]:
        """Keep the native task active until this test releases its owned work."""
        entered.set()
        try:
            await release.wait()
            return {"value": 7}
        finally:
            settled.set()

    @server.tool(task=False)
    async def inline_work() -> dict[str, int]:
        """An independent normal SDK call while Docket's capacity is occupied."""
        return {"independent": 11}

    accepted: list[AcceptedNativeCall] = []
    observed: list[ClientGetTaskResult] = []

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        accepted.append(call)
        return accepted_result({"accepted": True, "handle": call.submission.handle, "kind": "MCP"})

    async def observe(session: ClientSession, task_id: str, reply: ClientGetTaskResult) -> None:
        assert session is accepted[0].task._session
        assert task_id == accepted[0].task.task_id
        observed.append(reply)

    async def guard_update(session: ClientSession, task_id: str, answers: dict[str, Any]) -> None:
        pytest.fail("This task does not request input")

    transport = TaskObservationTransport(FastMCPTransport(server), observe, guard_update)
    async with TaskAwareClient(transport, on_task_accepted=adopt) as client:
        try:
            binding = submission("held_work")
            with client.submission(binding):
                receipt = await client.call_tool("held_work", {})
            assert receipt.structured_content is not None
            assert receipt.structured_content["handle"] == binding.handle
            await entered.wait()
            assert not settled.is_set()
            with client.submission(submission("inline_work")):
                independent = await client.call_tool("inline_work", {})
            assert independent.structured_content == {"independent": 11}
            assert not settled.is_set()
            assert len(accepted) == 1
            rebound = restore_native_call(
                client,
                accepted[0].submission,
                task_id=accepted[0].task.task_id,
                status=accepted[0].creation.status,
                created_at=accepted[0].creation.created_at,
                updated_at=accepted[0].creation.last_updated_at,
            )
            assert rebound.task.task_id == accepted[0].task.task_id
            assert rebound.submission == accepted[0].submission
            assert not settled.is_set() and len(accepted) == 1
            release.set()
            result = await rebound.task.result()
            assert result.structured_content == {"value": 7}
            assert observed[-1].status == "completed"
            assert settled.is_set()
        finally:
            release.set()
            if accepted:
                await accepted[0].task.result()


@pytest.mark.asyncio
async def test_missing_submission_context_is_rejected_before_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cannot create uncertain backend work without first recording custody."""
    sent: list[str] = []

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        pytest.fail("No task should be accepted")

    async def sdk_call(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        sent.append(name)
        pytest.fail("No tools/call should be sent")

    from fastmcp.client.mixins.tools import ClientToolsMixin

    monkeypatch.setattr(ClientToolsMixin, "call_tool_mcp", sdk_call)
    client = TaskAwareClient(FastMCP("unused"), on_task_accepted=adopt)
    with pytest.raises(ValueError, match="before tools/call"):
        await client.call_tool_mcp("work", {})
    assert sent == []


@pytest.mark.asyncio
async def test_changed_submission_arguments_are_rejected_before_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual tools/call cannot differ from the arguments held in durable custody."""
    from fastmcp.client.mixins.tools import ClientToolsMixin

    async def forbidden_rpc(self: Any, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("Changed arguments must never be sent")

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        pytest.fail("No task should be accepted")

    monkeypatch.setattr(ClientToolsMixin, "call_tool_mcp", forbidden_rpc)
    client = TaskAwareClient(FastMCP("argument-custody"), on_task_accepted=adopt)
    with client.submission(submission("work", arguments={"value": 1})):
        with pytest.raises(ValueError, match="Arguments differ"):
            await client.call_tool_mcp("work", {"value": 2})
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_client_close_cannot_cancel_accepted_adoption() -> None:
    """Shutdown cancellation leaves recording owned by the application client."""
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = False

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        nonlocal cancelled
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return accepted_result({"accepted": True, "handle": call.submission.handle, "kind": "MCP"})

    client = TaskAwareClient(FastMCP("close-custody"), on_task_accepted=adopt)
    with client.submission(submission("work")):
        caller = asyncio.create_task(
            client._resolve_claimed_result("work", native_creation(), None)
        )
    closer: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        closer = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        closer.cancel()
        await asyncio.gather(closer, return_exceptions=True)
        assert not cancelled
        assert len(client._adoptions) == 1
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)
        await client.close()
    assert not cancelled
    assert not client._adoptions


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_departed_callers_recording_failure_stays_visible_and_can_reconcile(
    cancel_count: int,
) -> None:
    """Recording can retry under the same native identity without replaying tools/call."""
    entered, release = asyncio.Event(), asyncio.Event()
    healthy = False
    calls: list[AcceptedNativeCall] = []

    async def adopt(call: AcceptedNativeCall) -> AcceptedTaskResult:
        calls.append(call)
        entered.set()
        await release.wait()
        if not healthy:
            raise OSError("Core write unavailable")
        return accepted_result({"accepted": True, "handle": call.submission.handle, "kind": "MCP"})

    client = TaskAwareClient(FastMCP("failed-custody"), on_task_accepted=adopt)
    binding = submission("work")
    with client.submission(binding):
        caller = asyncio.create_task(
            client._resolve_claimed_result("work", native_creation(), None)
        )
    try:
        await entered.wait()
        for _ in range(cancel_count):
            caller.cancel()
            await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(caller, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        with pytest.raises(TaskAdoptionError, match="Core write unavailable"):
            await client.close()
        failures = client.unconfirmed_adoptions
        assert len(failures) == 1
        assert failures[0].accepted.task.task_id == "native-custody-test"
        healthy = True
        receipt = await client.retry_adoption(binding.handle)
        assert receipt.structured_content is not None
        assert receipt.structured_content["handle"] == binding.handle
        assert calls[0] is calls[1]
        assert not client.unconfirmed_adoptions
    finally:
        release.set()
        healthy = True
        await asyncio.gather(caller, return_exceptions=True)
        if client.unconfirmed_adoptions:
            await client.retry_adoption(binding.handle)
        await client.close()


def test_terminal_validation_honors_declared_draft_and_format_annotations() -> None:
    """Draft07 tuple arrays remain valid; format annotations do not become assertions."""
    schema: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": [{"type": "integer"}, {"type": "string"}]},
            "label": {"type": "string", "format": "email"},
        },
    }
    current = ClientGetTaskResult.model_validate(
        {
            "resultType": "complete",
            "taskId": "native",
            "status": "completed",
            "createdAt": "2026-10-13T00:00:00Z",
            "lastUpdatedAt": "2026-10-13T00:00:01Z",
            "result": {
                "content": [],
                "structuredContent": {"values": [7, "seven"], "label": "annotation"},
            },
        }
    )
    validate_terminal(schema, current)


def test_terminal_validation_refuses_unknown_schema_dialects() -> None:
    """An unsupported contract cannot silently fall back to a different JSON Schema draft."""
    current = ClientGetTaskResult.model_validate(
        {
            "resultType": "complete",
            "taskId": "native",
            "status": "completed",
            "createdAt": "2026-10-13T00:00:00Z",
            "lastUpdatedAt": "2026-10-13T00:00:01Z",
            "result": {"content": [], "structuredContent": {"value": 7}},
        }
    )
    with pytest.raises(TaskResultValidationError, match="unsupported dialect"):
        validate_terminal({"$schema": "https://invalid.example/unimplemented-schema"}, current)


def test_task_wrapper_rejects_multi_server_proxy_before_connection() -> None:
    """A task-capable server must retain its own SDK connection and backend identity."""

    async def observe(session: ClientSession, task_id: str, reply: ClientGetTaskResult) -> None:
        pytest.fail("No connection should be opened")

    async def guard(session: ClientSession, task_id: str, answers: dict[str, Any]) -> None:
        pytest.fail("No call should be sent")

    proxy = MCPConfigTransport({"mcpServers": {"example": {"url": "http://127.0.0.1:1/mcp"}}})
    with pytest.raises(ValueError, match="one native client per server"):
        TaskObservationTransport(proxy, observe, guard)


@pytest.mark.asyncio
async def test_task_wrapper_delegates_transport_identity_auth_and_close() -> None:
    """Adapter contract double exists only in tests; no session is connected."""

    class Transport(ClientTransport):
        auth: Any = None
        closed = False

        @asynccontextmanager
        async def connect_session(self, **kwargs: Any) -> AsyncIterator[ClientSession]:
            pytest.fail("No session should be connected")
            yield  # pragma: no cover

        def get_session_id(self) -> str:
            return "original-legacy-session"

        def _set_auth(self, auth: Any) -> None:
            self.auth = auth

        async def close(self) -> None:
            self.closed = True

    async def observe(session: ClientSession, task_id: str, reply: ClientGetTaskResult) -> None:
        pytest.fail("No request should be sent")

    async def guard(session: ClientSession, task_id: str, answers: dict[str, Any]) -> None:
        pytest.fail("No request should be sent")

    original = Transport()
    wrapped = TaskObservationTransport(original, observe, guard)
    assert wrapped.get_session_id() == "original-legacy-session"
    wrapped._set_auth("test-credential")
    assert original.auth == "test-credential"
    await wrapped.close()
    assert original.closed
