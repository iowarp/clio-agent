"""Relay transport acceptance: two doors behind the #1115 task surface (#1125)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as FastMCPToolError
from fastmcp.utilities.tasks import TaskConfig
from fastmcp_tasks import TasksExtension
from fastmcp_tasks.client_models import ClientCreateTaskResult

from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskRecord,
    set_task_record_store,
    task_record_store,
)
from clio_agent.tools.relay_transport import (
    RELAY_POLL_INTERVAL_MS,
    RelayInlineResultTooLargeError,
    RelayMcpNameMismatchError,
    RelayTaskIdentity,
    RelayTaskJobMismatchError,
    RelayTransportClient,
    RelayTransportContractError,
)


class _RelayCapture:
    """Wire evidence collected by the in-process fake relay."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.submitted_tokens: list[str | None] = []


def _fake_relay(capture: _RelayCapture) -> FastMCP:
    """A FastMCP 4 task server standing in for the relay's MCP door."""

    server = FastMCP("relay-transport-reference")
    server.add_extension(TasksExtension(minimum_check_interval=timedelta(seconds=1)))

    relay_task = TaskConfig(poll_interval=timedelta(seconds=1))

    @server.tool(task=relay_task)
    async def relay_run(delay: float, idempotency_key: str | None = None) -> dict[str, Any]:
        """Return a terminal result after leaving time to consume the SSE door."""

        capture.submitted_tokens.append(idempotency_key)
        await asyncio.sleep(delay)
        return {"outcome": "done", "idempotency_key": idempotency_key}

    @server.tool(task=relay_task)
    async def mismatched_job() -> dict[str, str]:
        """Return a forged relay job identity for the client-side rejection test."""

        return {"job_id": "job-other", "outcome": "must-not-pass"}

    @server.tool(task=relay_task)
    async def nested_scheduler_job() -> dict[str, Any]:
        """Return application scheduler ids below the delivery envelope."""

        return {
            "outcome": "done",
            "services": {"web": {"job_id": "slurm-22567"}},
            "progress": {"jobId": "pbs-913"},
        }

    @server.tool
    async def relay_inline_reject(cluster: str) -> dict[str, str]:
        """Reject synchronously so the client must retain the relay's real error."""

        raise FastMCPToolError(f"pipeline not found on cluster {cluster}")

    @server.tool(task=relay_task)
    async def oversized_result() -> dict[str, Any]:
        """Return relay's documented failed inline-delivery envelope."""

        return {
            "content_truncated": True,
            "result_available": False,
            "delivery": {
                "schema_version": "clio-relay.mcp-result-delivery.v1",
                "status": "failed",
                "code": "inline_result_limit_exceeded",
                "max_inline_bytes": 65_536,
                "private_evidence_preserved": True,
                "remote_side_effects_may_have_occurred": True,
                "message": "result exceeded the safe inline response limit",
            },
        }

    return server


class _FakeRelayApp:
    """ASGI fake combining the MCP, timeline SSE, and artifact HTTP doors."""

    def __init__(self, mcp_app: Any, capture: _RelayCapture) -> None:
        self._mcp_app = mcp_app
        self._capture = capture

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Serve fake relay HTTP endpoints, delegating all other traffic to MCP."""

        if scope["type"] != "http":
            await self._mcp_app(scope, receive, send)
            return
        headers = {key.decode(): value.decode() for key, value in scope.get("headers", [])}
        path = str(scope.get("path", ""))
        self._capture.requests.append((path, headers))
        if path.endswith("/events/sse"):
            task_id = path.split("/")[-3]
            body = (
                "event: task_events\n"
                "data: "
                + json.dumps(
                    {
                        "task_id": task_id,
                        "events": [
                            {
                                "task_id": task_id,
                                "seq": 1,
                                "event_type": "progress",
                                "summary": "relay task is still running",
                            }
                        ],
                        "next_cursor": 2,
                    }
                )
                + "\n\n"
            ).encode()
            await self._respond(send, 200, body, b"text/event-stream")
            return
        if path.startswith("/artifacts/") and path.endswith("/content"):
            artifact_id = path.split("/")[-2]
            body = json.dumps(
                {
                    "artifact": {"artifact_id": artifact_id},
                    "encoding": "base64",
                    "data": "cmVsYXktYXJ0aWZhY3Q=",
                }
            ).encode()
            await self._respond(send, 200, body, b"application/json")
            return
        await self._mcp_app(scope, receive, send)

    @staticmethod
    async def _respond(send: Any, status: int, body: bytes, content_type: bytes) -> None:
        """Send one complete ASGI response."""

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", content_type)],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _Backend:
    """URLs and capture for one running fake relay."""

    def __init__(self, base_url: str, capture: _RelayCapture) -> None:
        self.base_url = base_url
        self.mcp_url = f"{base_url}/mcp"
        self.capture = capture


def _free_port() -> int:
    """Reserve an OS-assigned localhost port."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def relay_backend() -> Iterator[_Backend]:
    """Run the fake relay in-process over the real Streamable HTTP wire."""

    capture = _RelayCapture()
    port = _free_port()
    mcp_app = _fake_relay(capture).http_app(path="/mcp")
    server = uvicorn.Server(
        uvicorn.Config(
            _FakeRelayApp(mcp_app, capture),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=10)
        pytest.fail("the fake relay did not start within 30 seconds")
    try:
        yield _Backend(f"http://127.0.0.1:{port}", capture)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture(autouse=True)
def _isolated_task_store() -> Iterator[None]:
    """Give every acceptance test a fresh #1115 record store."""

    set_task_record_store(InMemoryTaskRecordStore())
    yield
    set_task_record_store(None)


def _client(backend: _Backend) -> RelayTransportClient:
    """Construct the owner-bound two-door client used by acceptance tests."""

    return RelayTransportClient(
        mcp_url=backend.mcp_url,
        http_base_url=backend.base_url,
        api_token="relay-secret",
        owner_session_id="session-alice",
        owner_session_generation_id="generation-1",
    )


def test_production_factory_resolves_both_doors_and_reports_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2: config-first transport construction has a typed absent state."""

    import clio_agent.tools.relay_transport as relay_module

    factory = getattr(relay_module, "relay_transport_from_env", None)
    assert callable(factory), "production relay factory is missing"

    for key in ("CLIO_RELAY_MCP_URL", "CLIO_RELAY_HTTP_URL", "CLIO_RELAY_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    unavailable = factory()
    assert unavailable.reason == "relay_not_configured"
    assert sorted(unavailable.details["missing"]) == ["api_token", "http_url", "mcp_url"]

    monkeypatch.setenv("CLIO_RELAY_MCP_URL", "http://127.0.0.1:18783/mcp")
    monkeypatch.setenv("CLIO_RELAY_HTTP_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("CLIO_RELAY_API_TOKEN", "relay-secret")
    configured = factory(session_id="session-alice")
    assert isinstance(configured, RelayTransportClient)
    assert configured._mcp_url == "http://127.0.0.1:18783/mcp"
    assert configured._http_base_url == "http://127.0.0.1:8765"
    assert configured._session_id == "session-alice"


async def _first(stream: AsyncIterator[dict[str, Any]]) -> dict[str, Any]:
    """Read one event from an async event stream."""

    async for event in stream:
        return event
    raise AssertionError("relay SSE stream returned no events")


async def test_agent_message_persists_then_sends_exact_tasks_update_payload() -> None:
    """The #145 agent input answer uses #1115's ledger and named update sender."""

    from tests.test_tools.test_mcp_tasks import ScriptedSession

    store = task_record_store()
    key = TaskKey(
        server_id="relay-message-test",
        session_id="session-alice",
        task_id="task-agent-message",
    )
    store.put(TaskRecord(key=key, tool="relay_submit_remote_agent", status="input_required"))
    session = ScriptedSession([])
    relay = RelayTransportClient(
        mcp_url="http://relay.invalid/mcp",
        http_base_url="http://relay.invalid",
        api_token="relay-secret",
        session_id="session-alice",
        store=store,
    )
    relay._mcp_client = SimpleNamespace(session=session)
    identity = RelayTaskIdentity.from_key(key)

    await relay.message(identity, "Use the new boundary condition.")

    expected = {
        "agent_message": {
            "action": "accept",
            "content": {"message": "Use the new boundary condition."},
        }
    }
    assert session.methods() == ["tasks/update"]
    assert session.updates == [expected]
    persisted = store.get(key)
    assert persisted is not None
    assert len(persisted.input_answers) == 1
    assert persisted.input_answers[0].payload == expected["agent_message"]
    assert persisted.input_answers[0].delivered is True


async def test_agent_message_lost_ack_retries_identical_persisted_answer() -> None:
    """A lost update acknowledgement never re-captures or changes the answer."""

    from tests.test_tools.test_mcp_tasks import ScriptedSession

    store = task_record_store()
    key = TaskKey("relay-message-test", "session-alice", "task-agent-retry")
    store.put(TaskRecord(key=key, tool="relay_submit_remote_agent", status="input_required"))
    relay = RelayTransportClient(
        mcp_url="http://relay.invalid/mcp",
        http_base_url="http://relay.invalid",
        api_token="relay-secret",
        session_id="session-alice",
        store=store,
    )
    first = ScriptedSession([], update_failures=1)
    relay._mcp_client = SimpleNamespace(session=first)
    identity = RelayTaskIdentity.from_key(key)
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await relay.message(identity, "same bytes")
    captured = store.get(key)
    assert captured is not None
    assert captured.input_answers[0].delivered is False

    retry = ScriptedSession([])
    relay._mcp_client = SimpleNamespace(session=retry)
    await relay.message(identity, "same bytes")

    assert first.updates == retry.updates
    delivered = store.get(key)
    assert delivered is not None
    assert delivered.input_answers[0].delivered is True


async def test_fake_relay_submit_poll_terminal_round_trip_and_headers(
    relay_backend: _Backend,
) -> None:
    """Submit, poll, and finish through MCP while owner identity is on submission."""

    async with _client(relay_backend) as relay:
        task = await relay.submit("relay_run", {"delay": 0.05}, idempotency_key="client-create-1")
        assert task.task_id == task.job_id == task.mcp_name
        assert task_record_store().get(task.key) is not None

        current = await relay.poll(task)
        while current.status not in {"completed", "failed", "cancelled"}:
            current = await relay.poll(task)

    assert current.status == "completed"
    assert relay_backend.capture.submitted_tokens == ["client-create-1"]
    submission_headers = next(
        headers
        for path, headers in relay_backend.capture.requests
        if path == "/mcp" and headers.get("mcp-method") == "tools/call"
    )
    assert submission_headers["authorization"] == "Bearer relay-secret"
    assert submission_headers["x-clio-relay-owner-session-id"] == "session-alice"
    assert submission_headers["x-clio-relay-session-generation-id"] == "generation-1"
    task_headers = [
        headers
        for path, headers in relay_backend.capture.requests
        if path == "/mcp" and headers.get("mcp-method") == "tasks/get"
    ]
    assert task_headers
    assert all(headers["mcp-name"] == task.task_id for headers in task_headers)
    assert task.poll_interval_ms == RELAY_POLL_INTERVAL_MS


async def test_timeline_streams_while_running_and_artifact_fetch_returns_bytes(
    relay_backend: _Backend,
) -> None:
    """The HTTP door streams task events and fetches out-of-band artifact bytes."""

    async with _client(relay_backend) as relay:
        task = await relay.submit("relay_run", {"delay": 1.0})
        event = await _first(relay.stream_events(task))
        assert (await relay.poll(task)).status == "working"
        content = await relay.fetch_artifact("artifact-1")
        await relay.cancel(task)
        cancel_record = task_record_store().get(task.key)
        assert cancel_record is not None and cancel_record.cancel_requested is True
        cancelled = await relay.poll(task)

    assert event["task_id"] == task.task_id
    assert event["event_type"] == "progress"
    assert content == b"relay-artifact"
    assert cancelled.status == "cancelled"
    assert task_record_store().get(task.key) is None


async def test_oversize_inline_delivery_raises_typed_contract_error(
    relay_backend: _Backend,
) -> None:
    """A relay delivery failure is typed and never presented as truncated success."""

    async with _client(relay_backend) as relay:
        task = await relay.submit("oversized_result", {})
        with pytest.raises(RelayInlineResultTooLargeError) as raised:
            await relay.wait(task, timeout_seconds=15)

    assert raised.value.delivery["schema_version"] == "clio-relay.mcp-result-delivery.v1"
    assert raised.value.delivery["code"] == "inline_result_limit_exceeded"
    assert raised.value.details["task_id"] == task.task_id


async def test_task_job_mismatch_is_rejected_client_side(relay_backend: _Backend) -> None:
    """A terminal relay result cannot redirect a task onto another job identity."""

    async with _client(relay_backend) as relay:
        task = await relay.submit("mismatched_job", {})
        with pytest.raises(RelayTaskJobMismatchError) as raised:
            await relay.wait(task, timeout_seconds=15)

    assert raised.value.details["task_id"] == task.task_id
    assert raised.value.details["job_id"] == "job-other"


async def test_nested_application_job_ids_do_not_override_relay_task_identity(
    relay_backend: _Backend,
) -> None:
    """Finding 1: scheduler-native ids below the top envelope remain application data."""

    async with _client(relay_backend) as relay:
        task = await relay.submit("nested_scheduler_job", {})
        final = await relay.wait(task, timeout_seconds=15)

    assert final.status == "completed"
    assert final.result["structuredContent"]["services"]["web"]["job_id"] == "slurm-22567"
    assert final.result["structuredContent"]["progress"]["jobId"] == "pbs-913"


async def test_submit_rejects_missing_and_unknown_arguments_before_submission(
    relay_backend: _Backend,
) -> None:
    """Finding 6: discovered inputSchema failures are typed before tools/call."""

    async with _client(relay_backend) as relay:
        before = len(
            [
                1
                for path, headers in relay_backend.capture.requests
                if path == "/mcp" and headers.get("mcp-method") == "tools/call"
            ]
        )
        with pytest.raises(RelayTransportContractError) as raised:
            await relay.submit("relay_run", {"unexpected": True})
        after = len(
            [
                1
                for path, headers in relay_backend.capture.requests
                if path == "/mcp" and headers.get("mcp-method") == "tools/call"
            ]
        )

    assert raised.value.reason == "relay_arguments_invalid"
    assert raised.value.details["missing_keys"] == ["delay"]
    assert raised.value.details["unknown_keys"] == ["unexpected"]
    assert after == before
    # FAILING-FIRST: the typed ``details`` never reach an agent -- the tool layer
    # renders only the exception's own text, so a live expert saw a bare "do not
    # match its discovered inputSchema" and had no way to learn WHICH key was
    # wrong. It burned three consecutive remote dispatches guessing. The message
    # itself must name the offending keys.
    message = str(raised.value)
    assert "missing ['delay']" in message
    assert "unknown ['unexpected']" in message


async def test_submit_preserves_inline_relay_error_as_typed_reason(
    relay_backend: _Backend,
) -> None:
    """Finding 6: a non-admitted inline rejection retains the relay error text."""

    async with _client(relay_backend) as relay:
        with pytest.raises(RelayTransportContractError) as raised:
            await relay.submit("relay_inline_reject", {"cluster": "local"})

    assert raised.value.reason == "relay_call_rejected_inline"
    assert "pipeline not found on cluster local" in raised.value.details["relay_error"]


async def test_mcp_name_mismatch_is_rejected_before_an_rpc(relay_backend: _Backend) -> None:
    """A forged Mcp-Name is refused locally rather than sent to the relay."""

    async with _client(relay_backend) as relay:
        task = await relay.submit("relay_run", {"delay": 1.0})
        forged = RelayTaskIdentity(
            key=task.key,
            job_id=task.job_id,
            mcp_name="another-task",
            poll_interval_ms=task.poll_interval_ms,
        )
        before = len(relay_backend.capture.requests)
        with pytest.raises(RelayMcpNameMismatchError):
            await relay.poll(forged)
        assert len(relay_backend.capture.requests) == before
        await relay.cancel(task)


async def test_reconnect_uses_persisted_record_after_originating_client_drops(
    relay_backend: _Backend,
) -> None:
    """A fresh client resumes the exact durable #1115 key to terminal."""

    first = _client(relay_backend)
    async with first:
        task = await first.submit("relay_run", {"delay": 1.0})
        persisted = task_record_store().get(task.key)
        assert persisted is not None
        assert persisted.backend["url"] == relay_backend.mcp_url

    async with _client(relay_backend) as rebuilt:
        final = await rebuilt.resume(task.key, timeout_seconds=15)

    assert final.status == "completed"
    assert task_record_store().get(task.key) is None


async def test_cancel_merges_the_post_ack_record_instead_of_replaying_stale_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 10: cancel preserves a concurrent driver's status and lease fields."""

    store = task_record_store()
    key = TaskKey("relay-cancel-test", "session-alice", "task-cancel-race")
    original = TaskRecord(key=key, tool="relay_run", status="created")
    store.put(original)
    relay = RelayTransportClient(
        mcp_url="http://relay.invalid/mcp",
        http_base_url="http://relay.invalid",
        api_token="relay-secret",
        session_id="session-alice",
        store=store,
    )
    relay._mcp_client = SimpleNamespace(session=object())

    async def cancel_with_concurrent_driver(
        _session: Any, task_key: TaskKey, **_kwargs: Any
    ) -> Any:
        store.drop(task_key)
        store.put(
            replace(
                original,
                status="working",
                lease_owner="driver-1",
                lease_expires_at=time.time() + 30,
            )
        )
        return {"acknowledged": True}

    monkeypatch.setattr(
        "clio_agent.tools.relay_transport.cancel_task", cancel_with_concurrent_driver
    )

    await relay.cancel(RelayTaskIdentity.from_key(key))

    retained = store.get(key)
    assert retained is not None
    assert retained.status == "working"
    assert retained.lease_owner == "driver-1"
    assert retained.cancel_requested is True


async def test_submit_returns_a_plain_identity_even_when_create_reports_terminal(
    relay_backend: _Backend,
) -> None:
    """FAILING-FIRST regression for D1 (f3da6efd): relay materializes the
    SEP-2663 task claim only AFTER a ``wait_for_terminal`` dispatch already
    ran to completion (``clio_relay.fastmcp_server`` ``intercept_tool_call``:
    ``status=... if task.state in TERMINAL_STATES else "working"``), so
    ``status`` on the create response can legitimately already be terminal --
    mirrored here via monkeypatching the parsed create result, since the
    generic fastmcp_tasks reference server this fixture wraps always claims
    ``working``. ``ClientCreateTaskResult`` (``fastmcp_tasks/client_models.py``)
    has no ``result``/``error`` field on the wire at all -- only
    ``ClientGetTaskResult``, produced exclusively by ``tasks/get``, carries
    one. So ``submit()`` must never attach a synthesized result to the
    returned identity, terminal-at-birth or not: the identity it returns is
    always the same plain, from-key shape, and a caller resolves the real
    payload through exactly one follow-up ``relay.poll()`` -- proven below
    against the real in-process task server, not a hand-built fake."""

    async with _client(relay_backend) as relay:
        client = relay._require_mcp_client()
        real_call_tool = client.session.call_tool

        async def terminal_call_tool(*args: Any, **kwargs: Any) -> Any:
            raw = await real_call_tool(*args, **kwargs)
            if isinstance(raw, ClientCreateTaskResult):
                return raw.model_copy(update={"status": "completed"})
            return raw

        client.session.call_tool = terminal_call_tool  # type: ignore[method-assign]
        identity = await relay.submit("relay_run", {"delay": 0})

        assert not hasattr(identity, "initial_result")
        assert identity == RelayTaskIdentity.from_key(identity.key)

        # The one round trip D1 dropped: the real payload only exists behind
        # a ``tasks/get``, and it must come back populated, not None.
        fetched = await relay.poll(identity)

    assert fetched.status == "completed"
    assert fetched.result is not None
    assert fetched.result["isError"] is False
    assert fetched.result["structuredContent"] == {"outcome": "done", "idempotency_key": None}
