"""C1-S2 D4: MRTR per-path verification + the staller legs (#1282).

Three legs, each pinning something the D1-D3 machinery must not have broken
or left unobservable:

(a) the SDK's ``run_input_required_driver`` MRTR loop against the exerciser's
    ``guarded_input`` (a REQUIRED-task tool that also asks one MRTR round)
    through both a modern-negotiated direct connect and a legacy-negotiated
    front — the P1.3/P1.4 elicitation-callback machinery must survive S1's
    capability-keyed routing unchanged;
(b) the task-mode ``staller``: pin what IS observable during a task-mode wait
    post-D3 (the ``mcp_task.wait`` surfaced events via
    ``tools.mcp_task_records.set_task_wait_listener``, the attempt count, the
    poll cadence, the status transitions) and prove a live cancel interrupts
    it promptly (ack-only semantics, ``mcp_tasks.cancel_task``);
(c) the ``plain_staller`` progress-reset arm: a progress notification is the
    observable "alive" signal for a PLAIN (non-task) call — no wait-ladder
    escalation (the typed ``tools.mcp.call_timeout_s`` backstop,
    ``tools/mcp_wait_ladder.py``) fires while progress keeps arriving.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from fastmcp import Client

from clio_agent.tools.mcp_config import MCPServerSpec, transport_for
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_runtime import MCPClientCapabilities, MCPClientHandlers, make_mcp_client
from clio_agent.tools.mcp_task_extension import backend_identity
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    set_task_record_store,
    set_task_wait_listener,
)
from clio_agent.tools.mcp_tasks import (
    cancel_task,
    drive_task_to_terminal,
    session_elicitation_callback,
)

from .mcp_exerciser import EXERCISER_PATH, build_exerciser_server


@pytest.fixture(autouse=True)
def _isolated_task_store() -> Any:
    set_task_record_store(InMemoryTaskRecordStore())
    yield
    set_task_record_store(None)


async def _auto_accept(
    context: Any, message: Any, response_type: Any, params: Any, request_context: Any
) -> Any:
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="accept", content={"value": "picked"})


def _elicit_client_factory(target: Any, *, mode: str | None = None) -> Any:
    kwargs: dict[str, Any] = {
        "handlers": MCPClientHandlers(elicitation=_auto_accept),
        "capabilities": MCPClientCapabilities(elicitation_form=True),
    }
    if mode is not None:
        # A direct fastmcp Client, mirroring the C1-S0 probe's own legacy-front
        # pattern (make_mcp_client has no mode passthrough of its own -- era is
        # config-resolved, never a per-call override in production).
        from clio_agent.tools.mcp_task_extension import tasks_declaration

        declaration = tasks_declaration(Client, target)
        return Client(
            target,
            mode=mode,
            elicitation_handler=_auto_accept,
            extensions=list(declaration.extensions),
        )
    return make_mcp_client(target, **kwargs)


# --------------------------------------------------------------------------
# (a) MRTR: guarded_input through the direct route AND the legacy-mode front
# --------------------------------------------------------------------------


async def test_guarded_input_mrtr_round_trips_direct_modern() -> None:
    """The SDK's run_input_required_driver resolves guarded_input's ONE MRTR
    round through a modern-negotiated direct client (in-memory exerciser)."""

    executor = AsyncMCPToolExecutor(
        build_exerciser_server(),
        client_factory=lambda target: _elicit_client_factory(target),
    )
    async with executor:
        result = await executor.call_tool("guarded_input", {})
    assert "answered:" in result and "picked" in result


async def test_guarded_input_legacy_front_refuses_terminal_fast_not_hangs() -> None:
    """``guarded_input`` is a REQUIRED-task tool: extensions (the tasks id a
    task=required refusal needs) do not exist on the wire pre-2026-07-28 (the
    C1-S0 probe verdict -- ``capabilities.extensions`` is stripped by the
    legacy version sieve), so a legacy-NEGOTIATED front can never satisfy it,
    by protocol construction, not by a routing bug. What S1's routing must
    NOT regress is the SHAPE of that failure: a legacy front still gets the
    typed -32021 refusal terminal-fast (D1), never a silent hang -- proven
    here for the era C1-S0's own no-extension-client pin (line ~203 of
    test_mcp_v2_conformance.py) does not cover."""

    executor = AsyncMCPToolExecutor(
        build_exerciser_server(),
        client_factory=lambda target: _elicit_client_factory(target, mode="legacy"),
    )
    from clio_agent.errors import MCPMissingRequiredClientCapabilityError

    async with executor:
        with pytest.raises(MCPMissingRequiredClientCapabilityError):
            await executor.call_tool("guarded_input", {})


# --------------------------------------------------------------------------
# (b) task-mode staller: observable waits + live cancel
# --------------------------------------------------------------------------


async def test_staller_wait_events_surface_attempts_and_status() -> None:
    """Every non-terminal poll of a task-mode staller fires a typed
    ``mcp_task.wait`` -equivalent observation (attempt N, status, the
    server-advertised next-poll interval) through the generic wait-surfacing
    listener -- what IS observable during a task-mode wait, pinned."""

    seen: list[tuple[str, str, int, Any]] = []
    set_task_wait_listener(
        lambda key, status, attempt, next_poll_ms: seen.append(
            (key.task_id, status, attempt, next_poll_ms)
        )
    )
    try:
        client = make_mcp_client(build_exerciser_server(), server_id="staller-wait")
        async with client:
            result = await client.call_tool("staller", {"seconds": 0.3, "steps": 6})
        assert result.data == "stalled-through"
        assert seen, "no wait events observed for a task-mode drive"
        attempts = [row[2] for row in seen]
        assert attempts == sorted(attempts), "attempt numbers must be monotonic"
        assert attempts[0] == 1
        assert all(row[1] == "working" for row in seen), "only non-terminal polls surface"
    finally:
        set_task_wait_listener(None)


async def test_staller_live_cancel_interrupts_promptly() -> None:
    """A user cancel mid-stall settles the task ``cancelled`` promptly (ack-only
    semantics, mcp_tasks.cancel_task) instead of waiting out the tool's full
    ``steps`` -- the drive returns almost immediately after the ack."""

    from fastmcp_tasks.client import call_tool_task

    spec = MCPServerSpec(
        name="stallcancel",
        transport="stdio",
        command=sys.executable,
        args=(str(EXERCISER_PATH),),
    )
    client = make_mcp_client(transport_for(spec), server_id="stallcancel")
    async with client:
        assert client.session is not None
        # call_tool_task returns the handle as soon as the server ACCEPTS the
        # task, unlike client.call_tool -- which would auto-claim and drive it
        # to terminal transparently, racing this test's own cancel timing.
        handle = await call_tool_task(client, "staller", {"seconds": 30.0, "steps": 60})
        key = TaskKey(
            server_id=backend_identity(client.transport).server_id,
            session_id="test-session",
            task_id=handle.task_id,
        )

        async def _drive() -> Any:
            return await drive_task_to_terminal(
                client.session, key, session_elicitation_callback(client.session)
            )

        drive_task = asyncio.create_task(_drive())
        await asyncio.sleep(0.2)  # let the drive observe at least one "working" poll
        assert not drive_task.done(), "the staller must still be mid-flight when cancelled"
        ack = await cancel_task(client.session, key)
        assert ack is not None

        final = await asyncio.wait_for(drive_task, timeout=5.0)
        assert final.status == "cancelled"


# --------------------------------------------------------------------------
# (c) plain_staller progress-reset: no ladder escalation while progress flows
# --------------------------------------------------------------------------


async def test_plain_staller_progress_flow_never_trips_the_backstop() -> None:
    """A steadily progress-emitting plain call never trips the typed
    call_timeout_s backstop (tools/mcp_wait_ladder.MCPCallTimeoutBackstopError)
    -- progress is the observable "alive" signal a stall detector would key
    on; here there is no escalation to observe because none fires."""

    events: list[str] = []

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        events.append(f"{progress}/{total}")

    server = build_exerciser_server()
    executor = AsyncMCPToolExecutor(server, client_factory=lambda target: make_mcp_client(target))
    async with executor:
        client, on_server_name, _ns = await executor._route("plain_staller")  # noqa: SLF001
        outcome = await client.call_tool(
            on_server_name, {"seconds": 0.3, "steps": 6}, progress_handler=_on_progress
        )
    assert outcome.data == "plain-stalled"
    assert len(events) == 6, events


def test_call_timeout_backstop_reason_is_the_documented_constant() -> None:
    """The typed reason D3 surfaces on a genuine backstop firing (sanity pin
    that the constant this suite would grep for is spelled as documented)."""

    from clio_agent.tools.mcp_wait_ladder import MCP_CALL_TIMEOUT_BACKSTOP

    assert MCP_CALL_TIMEOUT_BACKSTOP == "mcp_call_timeout_backstop"
