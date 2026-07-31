"""SEP-2663 conformance against a REAL fastmcp-4 task-serving backend (#1115).

Everything else in ``test_mcp_tasks.py`` drives the poll loop against a scripted
session. This module drives CLIO's client against an actual ``fastmcp`` 4 server
with ``TasksExtension`` installed, over **Streamable HTTP** — the only transport on
which the ``Mcp-Name`` header exists at all — served by uvicorn in-process behind an
ASGI middleware that captures every request's headers.

The three scenarios are the conformance contract: task round-trip, an
``input_required`` round answered through the ONE HITL surface, and
reconnect-by-task-id after the originating client is dropped.

The server-side task backend is Docket, whose default URL is ``memory://`` — no
Redis is required for a single-process deployment.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from typing import Any

import mcp_types
import pytest
import uvicorn
from fastmcp import Context, FastMCP
from fastmcp_tasks import TasksExtension, call_tool_task

from clio_agent.tools.mcp_handlers import MCPClientCapabilities
from clio_agent.tools.mcp_runtime import MCPClientHandlers, make_mcp_client
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskRecord,
    set_task_record_store,
)
from clio_agent.tools.mcp_tasks import cancel_task, resume_task


def _elicit(message: str) -> Any:
    """One serialized ``elicitation/create`` ask a guard leg returns."""

    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(
            message=message,
            requested_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )
    )


def _reference_server() -> FastMCP:
    """A fastmcp-4 reference server with SEP-2663 tasks enabled."""

    server = FastMCP("tasks-reference")
    server.add_extension(TasksExtension())

    @server.tool(task=True)
    async def crunch(dataset: str) -> str:
        """Crunch a dataset in the background."""

        await asyncio.sleep(0.05)
        return f"crunched:{dataset}"

    @server.tool(task=True)
    async def guarded(ctx: Context) -> Any:
        """Ask for one input, then finish (the guard-pattern task tool)."""

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={"q1": _elicit("Pick a value")},
                requestState="s1",
                resultType="input_required",
            )
        answer = responses.get("q1")
        return f"answered:{getattr(answer, 'content', None)}"

    @server.tool(task=True)
    async def slow(seconds: float) -> str:
        """A long task the client abandons and later resumes by id."""

        await asyncio.sleep(seconds)
        return "slow-done"

    return server


class _HeaderCapture:
    """ASGI middleware recording ``(mcp-method, mcp-name)`` for every request."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.seen: list[tuple[str | None, str | None]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Record the inbound routing headers, then delegate."""

        if scope["type"] == "http":
            headers = {key.decode(): value.decode() for key, value in scope.get("headers", [])}
            self.seen.append((headers.get("mcp-method"), headers.get("mcp-name")))
        await self.app(scope, receive, send)

    def task_rpcs(self) -> list[tuple[str, str | None]]:
        """Only the ``tasks/*`` requests, as ``(method, Mcp-Name)``."""

        return [(m, n) for m, n in self.seen if m and m.startswith("tasks/")]


class _Backend:
    """A running reference server plus its header capture."""

    def __init__(self, url: str, capture: _HeaderCapture) -> None:
        self.url = url
        self.capture = capture


def _free_port() -> int:
    """An OS-assigned free localhost port."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def backend() -> Any:
    """Serve the reference server over Streamable HTTP for one test."""

    port = _free_port()
    capture = _HeaderCapture(_reference_server().http_app(path="/mcp"))
    server = uvicorn.Server(
        uvicorn.Config(capture, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=10)
        pytest.fail("the fastmcp-4 reference server did not start within 30s")
    try:
        yield _Backend(f"http://127.0.0.1:{port}/mcp", capture)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture(autouse=True)
def _isolated_store() -> Any:
    """Keep task records out of the process-wide registry."""

    set_task_record_store(InMemoryTaskRecordStore())
    yield
    set_task_record_store(None)


_PROMPTS: list[str] = []


async def _accept_handler(
    context: Any, message: str, response_type: Any, params: Any, request_context: Any
) -> Any:
    """A CLIO-shaped elicitation hook standing in for the HITL surface."""

    from fastmcp.client.elicitation import ElicitResult

    _PROMPTS.append(getattr(params, "message", ""))
    return ElicitResult(action="accept", content={"value": "chosen"})


def _client(url: str) -> Any:
    """An execution-path CLIO client (tasks declared, elicitation wired)."""

    return make_mcp_client(
        url,
        handlers=MCPClientHandlers(elicitation=_accept_handler),
        capabilities=MCPClientCapabilities(elicitation_form=True),
    )


async def test_conformance_task_round_trip(backend: Any) -> None:
    """A ``task=True`` tool completes transparently: caller sees the real result."""

    async with _client(backend.url) as client:
        result = await client.call_tool("crunch", {"dataset": "alpha"})

    assert result.content[0].text == "crunched:alpha"
    methods = [method for method, _ in backend.capture.task_rpcs()]
    assert methods, "a tasked call must poll tasks/get"
    assert set(methods) == {"tasks/get"}


async def test_conformance_input_required_round_reaches_the_hitl_surface(
    backend: Any,
) -> None:
    """An ``input_required`` leg is answered once, through the elicitation handler."""

    _PROMPTS.clear()
    async with _client(backend.url) as client:
        result = await client.call_tool("guarded", {})

    assert result.content[0].text == "answered:{'value': 'chosen'}"
    # Exactly one prompt for one key, across however many polls the drive took —
    # the dedup ledger's whole purpose, proven against a live server.
    assert _PROMPTS == ["Pick a value"]
    assert any(method == "tasks/update" for method, _ in backend.capture.task_rpcs())


async def test_conformance_reconnect_by_task_id_after_dropping_the_client(
    backend: Any,
) -> None:
    """The headline: persist the id, drop the client, resume on a fresh one."""

    from clio_agent.tools.mcp_task_records import task_record_store

    client = _client(backend.url)
    async with client:
        handle = await call_tool_task(client, "slow", {"seconds": 1.0})
        task_id = handle.task_id
        task_record_store().put(TaskRecord(task_id=task_id, tool="slow", status="working"))
    # The client (and its session, and its transport) is gone here.

    async with _client(backend.url) as fresh:
        final = await resume_task(fresh.session, task_id)

    assert final.status == "completed"
    assert final.result is not None
    assert final.result["content"][0]["text"] == "slow-done"
    assert task_record_store().get(task_id) is None


async def test_conformance_mcp_name_header_on_every_task_rpc(backend: Any) -> None:
    """SEP-2663 MUST, proven on the wire: every ``tasks/*`` POST carries ``Mcp-Name``."""

    _PROMPTS.clear()
    async with _client(backend.url) as client:
        await client.call_tool("guarded", {})

    task_rpcs = backend.capture.task_rpcs()
    assert {method for method, _ in task_rpcs} >= {"tasks/get", "tasks/update"}
    assert all(name for _, name in task_rpcs), f"Mcp-Name missing on {task_rpcs}"
    # One task id, mirrored identically onto every RPC of that task.
    assert len({name for _, name in task_rpcs}) == 1


async def test_conformance_cancel_is_ack_only(backend: Any) -> None:
    """``tasks/cancel`` settles a live task by request/ack, no cancelled notification."""

    client = _client(backend.url)
    async with client:
        handle = await call_tool_task(client, "slow", {"seconds": 5.0})
        await cancel_task(client.session, handle.task_id)
        settled = await handle.wait(timeout=15.0)

    assert settled.status == "cancelled"
    cancels = [method for method, _ in backend.capture.task_rpcs() if method == "tasks/cancel"]
    assert cancels == ["tasks/cancel"]
    # The cancel travelled as a REQUEST bearing Mcp-Name, never as a notification.
    assert all(name for method, name in backend.capture.task_rpcs() if method == "tasks/cancel")


# --------------------------------------------------------------------------- #
# Conformance backend (b): clio-relay 1.5.10                                  #
# --------------------------------------------------------------------------- #
#
# The relay is an out-of-repo product with its own install and auth, so these run
# only when a relay is already serving and its URL + token are exported. Recipe
# (verified for 1.5.10 — 1.5.9's wheel is broken, iowarp/clio-relay#147):
#
#   uv venv relayvenv && uv pip install --python relayvenv --prerelease=allow \
#       "clio-relay==1.5.10"
#   CLIO_RELAY_API_TOKEN=<token> relayvenv/Scripts/clio-relay init
#   CLIO_RELAY_API_TOKEN=<token> relayvenv/Scripts/clio-relay mcp-server \
#       --transport http --host 127.0.0.1 --port 18783 --path /mcp --profile all
#   CLIO_RELAY_MCP_URL=http://127.0.0.1:18783/mcp CLIO_RELAY_API_TOKEN=<token> \
#       uv run --no-sync pytest tests/test_tools/test_mcp_tasks_conformance.py -k relay
#
# `clio_relay.fastmcp_server.RelayTasksExtension` gates EVERY `tasks/*` request on
# the client having declared `io.modelcontextprotocol/tasks` for that request, and
# validates `Mcp-Name` against the body's `taskId`. Both CLIO gaps are therefore
# directly observable against the real relay wire.

_RELAY_URL = os.environ.get("CLIO_RELAY_MCP_URL", "")
_RELAY_TOKEN = os.environ.get("CLIO_RELAY_API_TOKEN", "")
_relay_required = pytest.mark.skipif(
    not (_RELAY_URL and _RELAY_TOKEN),
    reason="set CLIO_RELAY_MCP_URL + CLIO_RELAY_API_TOKEN to run the clio-relay backend",
)


def _relay_transport() -> Any:
    """An authenticated Streamable HTTP transport onto the running relay."""

    from fastmcp.client.transports import StreamableHttpTransport

    return StreamableHttpTransport(_RELAY_URL, headers={"Authorization": f"Bearer {_RELAY_TOKEN}"})


@_relay_required
async def test_relay_accepts_task_rpcs_because_clio_declares_the_extension() -> None:
    """CLIO's per-request declaration passes the relay's capability gate.

    The control below proves the gate is live: an undeclared client is refused with
    ``MISSING_REQUIRED_CLIENT_CAPABILITY``. CLIO's client instead reaches the relay's
    own task lookup ("Task not found"), which is only possible once the declaration
    arrived on that request.
    """

    from mcp.shared.exceptions import MCPError

    from clio_agent.tools.mcp_tasks import send_task_get

    client = make_mcp_client(
        _relay_transport(),
        handlers=MCPClientHandlers(elicitation=_accept_handler),
        capabilities=MCPClientCapabilities(elicitation_form=True),
    )
    async with client:
        assert client.session._negotiated_version == "2026-07-28"
        with pytest.raises(MCPError) as declared:
            await send_task_get(client.session, "no-such-task")
    assert "not found" in str(declared.value).lower()

    from fastmcp import Client

    class _NoExtensionClient(Client):
        """Control: folds no extension at all, so nothing is declared."""

        _auto_internal_extensions = False

    async with _NoExtensionClient(_relay_transport()) as bare:
        with pytest.raises(MCPError) as undeclared:
            await send_task_get(bare.session, "no-such-task")
    assert "did not declare that extension" in str(undeclared.value)


@_relay_required
async def test_relay_reads_the_mcp_name_header_clio_sends() -> None:
    """The relay validates ``Mcp-Name`` against ``taskId`` — so the header arrives.

    Deliberately corrupting the header the SDK stamped produces the relay's
    ``HEADER_MISMATCH`` refusal. CLIO's unmodified request never trips it, which is
    only true because the ``name_param`` declaration puts the real task id there.
    """

    from fastmcp_tasks.client_models import ClientGetTaskResult, GetTaskRequestParams
    from mcp.shared.exceptions import MCPError

    from clio_agent.tools.mcp_tasks import NamedGetTaskRequest

    client = make_mcp_client(
        _relay_transport(),
        handlers=MCPClientHandlers(elicitation=_accept_handler),
        capabilities=MCPClientCapabilities(elicitation_form=True),
    )
    async with client:
        stamp = client.session._stamp

        def _corrupt(data: Any, opts: Any) -> None:
            stamp(data, opts)
            opts.setdefault("headers", {})["mcp-name"] = "some-other-task"

        client.session._stamp = _corrupt
        try:
            with pytest.raises(MCPError) as mismatched:
                await client.session.send_request(
                    NamedGetTaskRequest(params=GetTaskRequestParams(task_id="task-a")),
                    ClientGetTaskResult,
                )
        finally:
            client.session._stamp = stamp

    assert "mcp-name header does not match" in str(mismatched.value).lower()
