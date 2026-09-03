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
- a staller (progress + sleep) for the C1-S2 wait-surfacing and cancel legs;
- a synthetic, non-built-in :class:`~fastmcp.server.extensions.ServerExtension`
  (#1283, C1-S3) -- proves the generic extension-registry READ side
  (``tools/mcp_connection_era.py``'s ``record_server_extensions``) against an
  ARBITRARY identifier, not just the already-special-cased tasks/ui ids;
- a ui-serving tool + matching ``ui://`` resource (#1283, C1-S3 letter (d)) --
  the exerciser's MCP Apps admission arm, built with fastmcp's native
  ``fastmcp.apps`` support so the tool/resource ``_meta`` shape is
  spec-correct without hand-rolling it.

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
from fastmcp.apps import AppConfig, ResourceCSP, ResourcePermissions
from fastmcp.server.extensions import ServerExtension
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID, TaskConfig
from fastmcp_tasks.extension import TasksExtension

__all__ = [
    "EXERCISER_NAMESPACE",
    "EXERCISER_PATH",
    "GUARDED_RESOURCE_URI",
    "MODERN_PROTOCOL_VERSION",
    "SYNTHETIC_EXTENSION_ID",
    "TASKS_EXTENSION_ID",
    "UI_RESOURCE_URI",
    "URL_GUARDED_INPUT_URL",
    "build_exerciser_server",
]

EXERCISER_PATH = Path(__file__).resolve()

# The era the current v2 wire negotiates; pinned here so an upstream era bump
# fails conformance tests with a message about the ERA, not a bare string.
MODERN_PROTOCOL_VERSION = "2026-07-28"

# The declared-server namespace tests mount the exerciser under. Kept short and
# underscore-free: tool routing splits ``<namespace>_<tool>`` on the FIRST "_".
EXERCISER_NAMESPACE = "v2ex"

#: A deliberately non-built-in, non-tasks, non-ui identifier (#1283, C1-S3):
#: proves the generic extension-registry READ side records whatever a server
#: ACTUALLY declares, not a hardcoded shortlist of known ids.
SYNTHETIC_EXTENSION_ID = "x-clio-agent/exerciser-echo"

#: The MCP App resource ``ui_echo`` binds to; ``ui_panel`` (below) serves it.
UI_RESOURCE_URI = "ui://v2ex/panel"

#: The url ``url_guarded_input`` elicits (C1-S4, #1284 mrtr-url avenue): a
#: plain https origin a live-verification run declares trusted via
#: ``CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS`` before it can ever mint a
#: question -- see ``check_url_trust`` (an undeclared trust list always
#: declines, so this constant and that config value must stay in lockstep).
URL_GUARDED_INPUT_URL = "https://mcp-clio.example.com/authorize"

#: The MRTR-capable resource ``guarded_resource`` serves (mrtr-methods avenue).
GUARDED_RESOURCE_URI = "res://v2ex/guarded"


class SyntheticExtension(ServerExtension):
    """A synthetic, exerciser-only extension (#1283): identifier-only, no
    settings/methods/interceptor -- exists purely so the negotiated
    ``ServerCapabilities.extensions`` carries an id the client-side registry
    (:mod:`clio_agent.tools.mcp_extension_registry`) never special-cases,
    proving its READ side is generic end to end."""

    identifier = SYNTHETIC_EXTENSION_ID


def _one_elicit(message: str) -> Any:
    """One serialized form-mode ``ElicitRequest`` for the MRTR guard round."""

    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(
            message=message,
            requested_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )
    )


def _one_url_elicit(message: str) -> Any:
    """One serialized URL-mode ``ElicitRequest`` (C1-S4, #1284 mrtr-url avenue).

    The exerciser's ONLY url-mode arm -- every other MRTR guard here
    (``guarded_input``, ``plain_guarded_input``) is form-mode via
    :func:`_one_elicit`. This is what a REAL URL-mode elicitation over the
    wire looks like (LEG_C2.md's mrtr-url finding: no MCP tool anywhere in
    this repo emitted one before this).
    """

    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestURLParams(message=message, url=URL_GUARDED_INPUT_URL)
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
    server.add_extension(SyntheticExtension())

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
    async def url_guarded_input(ctx: Context) -> Any:
        """Ask for URL-mode consent, then finish (C1-S4, #1284 mrtr-url avenue).

        The exerciser's ONLY url-mode MRTR arm -- mirrors ``guarded_input``'s
        shape exactly, but its ``InputRequiredResult`` carries
        ``mcp_types.ElicitRequestURLParams`` (via :func:`_one_url_elicit`)
        instead of form params, so the avenue can assert the url-mode
        question payload (full URL + punycode-warning fields, build item 3).
        """

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={"q1": _one_url_elicit("Authorize CLIO")},
                requestState="round-1",
                resultType="input_required",
            )
        answer = responses.get("q1")
        return f"answered:{getattr(answer, 'content', None)}"

    @server.tool
    async def plain_url_guarded_input(ctx: Context) -> Any:
        """URL-mode consent via the PLAIN (non-task) SEP-2322 MRTR shape.

        Mirrors ``plain_guarded_input``'s reason for existing: unlike
        ``url_guarded_input`` (task=required, refused typed-fast through a
        naive proxy that never declares tasks -- the SAME defect mechanism
        ``task_echo`` proves), this needs no tasks extension at all, so it
        proves url-mode MRTR itself survives a ``create_proxy(ProxyClient(...))``
        front -- the proxy-MOUNT axis, not the protocol-era axis.
        """

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={"q1": _one_url_elicit("Authorize CLIO")},
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

    @server.tool(app=AppConfig(resource_uri=UI_RESOURCE_URI, visibility=["app", "model"]))
    async def ui_echo(payload: str) -> ToolResult:
        """Echo through a PLAIN tool bound to an MCP App resource (#1283).

        ``app=AppConfig(...)`` stamps ``_meta.ui.resourceUri`` onto this
        tool's DEFINITION -- ``gact/mcp_apps.py::_resource_uri`` reads exactly
        that key. Paired with ``ui_panel`` below, this is the exerciser's
        ui-serving arm: the conformance suite drives a real call through
        here, then feeds the real result into the (regression-locked,
        unmodified) Apps host's admission + serving logic (the observer
        invocation itself is by hand there, not through the auto-firing
        production hook -- see ``test_mcp_v2_conformance.py``'s Layer 6
        scope note).

        The RESULT (not just the definition) carries an extra, unrecognized
        ``_meta`` namespace (``x-clio-agent/unknown``) on purpose -- the
        exerciser's own proof that the Apps host's tolerate-unknown-metadata
        behavior still holds on THIS newly-declared wire, not just the
        pre-existing fake-executor fixture in ``tests/test_gact/
        test_mcp_apps.py``.
        """

        return ToolResult(
            content=[mcp_types.TextContent(type="text", text=f"ui:{payload}")],
            meta={"x-clio-agent/unknown": {"scratch": True}},
        )

    @server.resource(
        UI_RESOURCE_URI,
        app=AppConfig(
            csp=ResourceCSP(connect_domains=["http://127.0.0.1:*"], resource_domains=["blob:"]),
            permissions=ResourcePermissions(clipboard_write={}),
        ),
    )
    def ui_panel() -> str:
        """Serve the MCP App HTML shell ``ui_echo`` binds to (#1283).

        The ``ui://`` scheme resolves this resource's MIME type to
        ``text/html;profile=mcp-app`` automatically (fastmcp's
        ``resolve_ui_mime_type``) -- exactly what ``mcp_apps.py::
        _resource_payload`` requires.
        """

        return "<!doctype html><title>v2ex panel</title><body>v2ex</body>"

    @server.prompt
    async def guarded_prompt(ctx: Context) -> Any:
        """An MRTR-capable prompt (C1-S4, #1284 mrtr-methods avenue).

        `InputRequiredResult` is a RESULT TYPE, not a ``tools/call``-only
        feature (SEP-2322): fastmcp's ``FunctionPrompt`` wraps a raw return
        of one in ``InputRequiredPromptResult`` exactly like a tool's does,
        and the SDK's ``Client.get_prompt`` drives the SAME
        ``run_input_required_driver`` loop (``mcp/client/client.py``) --
        this is the exerciser's proof that MRTR genuinely fires on
        ``prompts/get``, not just ``tools/call``. Mirrors ``guarded_input``'s
        one-round shape.
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

    @server.resource(GUARDED_RESOURCE_URI)
    async def guarded_resource(ctx: Context) -> Any:
        """An MRTR-capable resource (C1-S4, #1284 mrtr-methods avenue).

        Same proof as ``guarded_prompt``, for ``resources/read``: fastmcp's
        ``convert_raw_to_resource_result`` wraps a raw ``InputRequiredResult``
        return in ``InputRequiredResourceResult``, and ``Client.read_resource``
        drives the SAME SDK driver. Mirrors ``guarded_input``'s one-round shape.
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

    return server


async def _forbidden_echo(payload: str) -> str:
    """Echo from a tool whose task mode is explicitly FORBIDDEN."""

    return f"forbidden:{payload}"


if __name__ == "__main__":
    build_exerciser_server().run()
