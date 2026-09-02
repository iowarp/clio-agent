"""The MCP v2 exerciser: a synthetic server that FORCEFULLY uses v2 semantics.

The dual-era conformance bed for the #1274 unification campaign (C1-S0,
iowarp/clio-agent#1280). Every campaign slice extends this server with the
surface it delivers, and ``test_mcp_v2_conformance.py`` runs the unified client
against it. It deliberately exercises what real v2 servers (e.g. the clio-kit
web MCP, whose ``fetch`` is ``task=required``) demand from a client:

- ``task=required`` tools that answer -32021 (with ``requiredCapabilities``)
  to any client that did not declare the tasks extension;
- the three ``taskSupport`` arms (required / optional / forbidden-explicit) --
  the forbidden arm sets ``Tool.execution`` EXPLICITLY because fastmcp omits
  the block for ``mode="forbidden"``, making it wire-identical to untagged;
- an MRTR guard tool (one ``input_required`` round, SEP-2577 shape);
- a staller (progress + sleep) for the C1-S2 wait-surfacing and cancel legs.

Import-only helper (``python_files = test_*.py`` keeps it uncollected), also
runnable as a stdio server (``python mcp_exerciser.py``) so the DECLARED path
-- ``MCPServerSpec`` -> ``build_gateway`` -> executor -- can mount it exactly
the way a user-declared server mounts. It imports fastmcp only, never
clio_agent, so the spawned subprocess needs no repo path.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.tools.base import Tool
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID, TaskConfig
from fastmcp_tasks.extension import TasksExtension

__all__ = [
    "EXERCISER_NAMESPACE",
    "EXERCISER_PATH",
    "MODERN_PROTOCOL_VERSION",
    "TASKS_EXTENSION_ID",
    "build_exerciser_server",
]

EXERCISER_PATH = Path(__file__).resolve()

# The era the current v2 wire negotiates; pinned here so an upstream era bump
# fails conformance tests with a message about the ERA, not a bare string.
MODERN_PROTOCOL_VERSION = "2026-07-28"

# The declared-server namespace tests mount the exerciser under. Kept short and
# underscore-free: tool routing splits ``<namespace>_<tool>`` on the FIRST "_".
EXERCISER_NAMESPACE = "v2ex"


def _one_elicit(message: str) -> Any:
    """One serialized form-mode ``ElicitRequest`` for the MRTR guard round."""

    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(
            message=message,
            requested_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )
    )


def build_exerciser_server() -> FastMCP:
    """Build the v2 exerciser server (fresh instance per call).

    Returns:
        A ``FastMCP`` server with the SEP-2663 tasks extension and the C1-S0
        tool matrix. Task backend is fastmcp-tasks' in-process default
        (docket ``memory://``) -- zero external infrastructure.
    """

    server = FastMCP(EXERCISER_NAMESPACE)
    server.add_extension(TasksExtension())

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def task_echo(payload: str) -> str:
        """Echo through a REQUIRED task: the clio-kit web ``fetch`` analog."""

        return f"echo:{payload}"

    @server.tool(task=TaskConfig(mode="optional", poll_interval=timedelta(milliseconds=50)))
    async def task_optional_echo(payload: str) -> str:
        """Echo through an OPTIONAL task: callable both plain and as a task."""

        return f"optional:{payload}"

    @server.tool
    async def plain_echo(payload: str) -> str:
        """A plain tool on a task-capable server: must keep working pre/post S1."""

        return f"plain:{payload}"

    # fastmcp emits NO execution block for mode="forbidden" (wire-identical to
    # untagged), and the @tool decorator registers a COPY, so the explicit
    # SEP-1686 forbidden arm must be built as a Tool object and registered
    # as-is for the legacy-era listing to carry all three taskSupport values.
    forbidden = Tool.from_function(
        _forbidden_echo, name="forbidden_echo", task=TaskConfig(mode="forbidden")
    )
    forbidden.execution = mcp_types.ToolExecution(task_support="forbidden")
    server.add_tool(forbidden)

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def guarded_input(ctx: Context) -> Any:
        """Ask for one input, then finish (the MRTR guard-pattern task tool)."""

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={"q1": _one_elicit("Pick a value")},
                requestState="round-1",
                resultType="input_required",
            )
        answer = responses.get("q1")
        return f"answered:{getattr(answer, 'content', None)}"

    @server.tool
    async def plain_guarded_input(ctx: Context) -> Any:
        """Ask for one input via the PLAIN (non-task) SEP-2322 MRTR shape (#1282 F9).

        Unlike ``guarded_input`` (task=required, so a client without the tasks
        extension can never reach it at all -- proven terminal-fast in
        ``test_guarded_input_legacy_front_refuses_terminal_fast_not_hangs``,
        a PROTOCOL-STRUCTURAL axis), this tool needs NO extension: the SDK's
        own ``run_input_required_driver`` handles a raw ``InputRequiredResult``
        return regardless of tasks capability. Proves MRTR itself survives a
        ``create_proxy(ProxyClient(...))`` front (which strips the tasks
        declaration but negotiates the SAME modern era otherwise) -- a
        DIFFERENT axis: the proxy mount, not the protocol era.
        """

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={"q1": _one_elicit("Pick a value")},
                requestState="round-1",
                resultType="input_required",
            )
        answer = responses.get("q1")
        return f"answered:{getattr(answer, 'content', None)}"

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def staller(ctx: Context, seconds: float = 0.5, steps: int = 5) -> str:
        """Run slowly as a REQUIRED task, cancellable between steps.

        Its ``report_progress`` calls ride the TASK channel, which a plain
        ``call_tool`` progress handler never sees (proven by the C1-S0 review's
        A/B) -- what IS observable during a task-mode wait (status transitions,
        poll cadence) is C1-S2's job to pin. For client-visible progress
        notifications, use ``plain_staller``.
        """

        for step in range(max(1, steps)):
            await asyncio.sleep(max(0.0, seconds) / max(1, steps))
            await ctx.report_progress(progress=step + 1, total=max(1, steps))
        return "stalled-through"

    @server.tool
    async def plain_staller(ctx: Context, seconds: float = 0.5, steps: int = 5) -> str:
        """Run slowly on the PLAIN path, one progress notification per step.

        The provable "progress resets the clock" / "visible waiting" arm:
        a ``call_tool(..., progress_handler=...)`` receives every step.
        """

        for step in range(max(1, steps)):
            await asyncio.sleep(max(0.0, seconds) / max(1, steps))
            await ctx.report_progress(progress=step + 1, total=max(1, steps))
        return "plain-stalled"

    @server.tool(annotations={"readOnlyHint": True})
    async def silent_sleeper(seconds: float = 0.5) -> str:
        """Sleep on the PLAIN path with ZERO progress notifications (#1282 F11).

        The contrasting arm to ``plain_staller``: genuinely silent (no
        instrument at all, not even one), so the C1-S2 D3 activity-driven
        ``call_timeout_s`` backstop (``tools/mcp_wait_ladder.
        run_with_activity_backstop``) has nothing to reset on and must still
        fire for a call that outlasts its window. ``readOnlyHint`` is
        genuinely true (it has no side effects) -- declared so a timeout on
        it surfaces the typed backstop directly rather than the executor's
        (correct, unrelated) uncertain-mutating-timeout guard for a
        NON-retry-safe tool.
        """

        await asyncio.sleep(max(0.0, seconds))
        return "slept-silently"

    return server


async def _forbidden_echo(payload: str) -> str:
    """Echo from a tool whose task mode is explicitly FORBIDDEN."""

    return f"forbidden:{payload}"


if __name__ == "__main__":
    build_exerciser_server().run()
