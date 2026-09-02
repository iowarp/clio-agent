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
(c) the activity-driven ``call_timeout_s`` backstop (#1282 F11, landed with
    F3a): a REAL reset proof, not just "nothing fired" -- a tool slower than
    a short CONFIGURED backstop that keeps emitting progress (``plain_
    staller``) still completes, while the SAME-duration, genuinely SILENT
    tool (``silent_sleeper``, zero progress) hits the typed
    ``MCPCallTimeoutBackstopError`` at that same short window.
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


async def test_plain_guarded_input_mrtr_round_trips_through_a_proxy_front() -> None:
    """#1282 F9: the PROXY-route MRTR leg the spec named -- a DIFFERENT axis
    than the legacy-front test above (that one proves a task=required tool
    structurally CANNOT be satisfied pre-2026-07-28; this one proves the
    SDK's ``run_input_required_driver`` MRTR loop itself survives the
    declared-path's PROXY mount (``gateway._proxy_for_spec``, which strips
    the backend's tasks declaration -- ``test_mcp_v2_conformance.py``'s
    ``test_proxy_front_strips_the_backend_tasks_declaration`` -- but still
    negotiates the SAME modern era otherwise). ``plain_guarded_input`` needs
    no tasks extension at all, so it round-trips successfully through the
    proxy exactly as it does direct. Goes through ``build_gateway`` (a real
    stdio-spawned backend, the SAME production wiring
    ``test_v1_fixture_through_the_declared_path`` proves calls a tool
    successfully) rather than a bare in-memory ``create_proxy(ProxyClient(
    build_exerciser_server()))`` front -- that construction, verified live,
    hits an unrelated era-negotiation mismatch in the installed fastmcp/mcp
    version for ANY tool call (not just MRTR), which is a third-party proxy
    quirk outside this slice's scope, not a C1-S2 defect."""

    from clio_agent.tools.gateway import build_gateway

    namespace = "proxymrtr"
    spec = MCPServerSpec(
        name=namespace, transport="stdio", command=sys.executable, args=(str(EXERCISER_PATH),)
    )
    gw = build_gateway({namespace: spec})
    # _elicit_client_factory (this file's own helper) wraps _auto_accept the
    # way CLIO's make_mcp_client expects (4-arg elicitation_handler); a bare
    # fastmcp Client(gw, elicitation_handler=_auto_accept) dispatches MRTR's
    # server-initiated requests through a DIFFERENT arg count, unrelated to
    # this slice -- proven the same way the direct-route test above builds
    # its client.
    async with _elicit_client_factory(gw) as front:
        result = await front.call_tool(f"{namespace}_plain_guarded_input", {})
    text = result.content[0].text
    assert "answered:" in text
    assert "picked" in text


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


async def test_progress_resets_the_activity_backstop_silence_still_trips_it() -> None:
    """#1282 F11 (adversarial review, landed with F3a): a REAL activity-reset
    proof, not merely "nothing fired". Both tools run LONGER than a short
    CONFIGURED ``call_timeout_s`` (the executor's own ``timeout``, reused
    as-is -- F3a adds no new config knob): ``plain_staller`` keeps emitting
    progress every ~0.1s (well inside the 0.5s window) and MUST complete;
    ``silent_sleeper`` emits NOTHING and MUST hit the typed
    ``MCPCallTimeoutBackstopError`` at that same window -- proving the
    backstop is genuinely activity-driven, not merely disarmed."""

    from clio_agent.tools.mcp_wait_ladder import MCPCallTimeoutBackstopError

    executor = AsyncMCPToolExecutor(
        build_exerciser_server(), timeout=0.5, client_factory=lambda target: make_mcp_client(target)
    )
    async with executor:
        # Total runtime (1.2s) EXCEEDS the 0.5s backstop window; only frequent
        # progress (12 steps, ~0.1s apart) keeps it alive past that window.
        outcome = await executor.call_tool_result("plain_staller", {"seconds": 1.2, "steps": 12})
        assert outcome.model_text == "plain-stalled"

        with pytest.raises(MCPCallTimeoutBackstopError) as excinfo:
            await executor.call_tool_result("silent_sleeper", {"seconds": 1.2})
        assert excinfo.value.reason == "mcp_call_timeout_backstop"
