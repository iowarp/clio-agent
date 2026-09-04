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
from typing import Annotated, Any, Literal

import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig, ResourceCSP, ResourcePermissions
from fastmcp.server.extensions import ServerExtension
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID, TaskConfig
from fastmcp_tasks.extension import TasksExtension
from pydantic import Field

__all__ = [
    "AGENT_AUDIENCE_META_KEY",
    "AGENT_GUARDED_INPUT_TOOL_NAME",
    "EXERCISER_NAMESPACE",
    "EXERCISER_PATH",
    "GUARDED_RESOURCE_URI",
    "HEADER_ANNOTATED_TOOL_NAME",
    "INVALID_HEADER_TOOL_NAME",
    "LIST_CHANGED_TOOL_NAME",
    "MODERN_PROTOCOL_VERSION",
    "SYNTHETIC_EXTENSION_ID",
    "TASKS_EXTENSION_ID",
    "UI_RESOURCE_URI",
    "URL_GUARDED_INPUT_IDN_URL",
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

#: #1285 C1-S5 item 1: a tool declaring an ``x-mcp-header``-ANNOTATED param, so
#: the headers avenue can actually exercise Mcp-Param-* mirroring (SEP-2578) --
#: no exerciser tool declared one before this slice (LEG_C2.md's gap 10).
HEADER_ANNOTATED_TOOL_NAME = "header_annotated_echo"

#: #1285 C1-S5 item 1: a tool whose ``x-mcp-header`` annotation is INVALID
#: (a ``number``-typed property -- SEP-2578 permits only string/integer/boolean).
#: The mcp SDK client MUST drop this tool from ``list_tools()`` results.
INVALID_HEADER_TOOL_NAME = "invalid_header_echo"

#: #1285 C1-S5 item 2: a tool that mutates the server's OWN tool registry at
#: runtime and fires ``notifications/tools/list_changed`` (LEG_C2.md's gap 7 --
#: the exerciser's tool set was fixed at build time before this slice).
LIST_CHANGED_TOOL_NAME = "mutate_and_notify_list_changed"

#: The url ``url_guarded_input`` elicits (C1-S4, #1284 mrtr-url avenue): a
#: plain https origin a live-verification run declares trusted via
#: ``CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS`` before it can ever mint a
#: question -- see ``check_url_trust`` (an undeclared trust list always
#: declines, so this constant and that config value must stay in lockstep).
URL_GUARDED_INPUT_URL = "https://mcp-clio.example.com/authorize"

#: The IDN (ACE-encoded, ``xn--``) counterpart ``url_guarded_input_idn``
#: elicits (Opus review addendum, C1-S4): the LIVE mrtr-url avenue can only
#: ever prove ``punycode_warning`` with a plain-ASCII host, since
#: ``URL_GUARDED_INPUT_URL`` above never trips it -- a live leg that never
#: exercises the ``warning=True`` branch cannot prove that branch works.
#: ``xn--nxasmq6b`` is the SAME real punycode-encoded (RFC 3492) IDN label
#: ``tests/test_gact/test_elicitation_hitl.py`` already uses -- one known-good
#: literal, not two independently-typed ones. Must stay in lockstep with the
#: live-verification leg's trusted-origins env var, same as the ASCII url.
URL_GUARDED_INPUT_IDN_URL = "https://xn--nxasmq6b.mcp-clio.example.com/authorize"

#: The MRTR-capable resource ``guarded_resource`` serves (mrtr-methods avenue).
GUARDED_RESOURCE_URI = "res://v2ex/guarded"

#: C1-S7 (#1309): the reverse-DNS ``_meta`` key clio's agent-driven-elicitation
#: convention reads to mark a question "for the agent, not the human" (mirrors
#: the existing ``x-clio-agent/*`` vendor-namespace convention this exerciser
#: already uses for ``SYNTHETIC_EXTENSION_ID`` / the ``ui_echo`` unknown-meta
#: probe). See ``clio_agent.gact.agent_elicitation``.
AGENT_AUDIENCE_META_KEY = "x-clio-agent/audience"

#: ``agent_guarded_input``'s tool name (leg_c2 avenue 12, "agent-elicitation").
AGENT_GUARDED_INPUT_TOOL_NAME = "agent_guarded_input"


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


def _one_url_elicit(message: str, url: str = URL_GUARDED_INPUT_URL) -> Any:
    """One serialized URL-mode ``ElicitRequest`` (C1-S4, #1284 mrtr-url avenue).

    The exerciser's url-mode arms -- every other MRTR guard here
    (``guarded_input``, ``plain_guarded_input``) is form-mode via
    :func:`_one_elicit`. This is what a REAL URL-mode elicitation over the
    wire looks like (LEG_C2.md's mrtr-url finding: no MCP tool anywhere in
    this repo emitted one before this). ``url`` defaults to the plain-ASCII
    origin; ``url_guarded_input_idn`` below passes the IDN counterpart so the
    LIVE avenue can prove ``punycode_warning=True`` too, not just the
    always-false ASCII case (Opus review addendum).
    """

    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestURLParams(message=message, url=url)
    )


def _one_agent_elicit(message: str, requested_schema: dict[str, Any]) -> Any:
    """One serialized form-mode ``ElicitRequest`` carrying the agent-audience
    ``_meta`` hint (C1-S7, #1309): the exerciser's proof that a server can mark
    a question "for the agent, not the human" via clio's declared convention.
    """

    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(
            message=message,
            requested_schema=requested_schema,
            # the wire alias, not the python attribute name -- required for
            # mypy's pydantic-model constructor stub (runtime accepts both).
            _meta={AGENT_AUDIENCE_META_KEY: "agent"},
        )
    )


def build_exerciser_server(
    *,
    cache_ttl: int | None = None,
    cache_scope: Literal["public", "private"] | None = None,
) -> FastMCP:
    """Build the v2 exerciser server (fresh instance per call).

    ``cache_ttl``/``cache_scope`` (#1285 C1-S5 item 3, both default ``None`` --
    every EXISTING caller's behavior is unchanged) forward verbatim to
    ``FastMCP(cache_ttl=..., cache_scope=...)``, which fastmcp applies
    UNIFORMLY to every SDK-cacheable result the server emits (tools/list,
    prompts/list, resources/list, resources/templates/list, resources/read,
    server/discover -- "no per-component surface and no aggregation", per
    fastmcp's own ``server/caching.py`` docstring). A per-tool cache hint does
    not exist in fastmcp; server-wide is the only knob the library offers.

    Returns:
        A ``FastMCP`` server with the SEP-2663 tasks extension and the C1-S0
        tool matrix. Task backend is fastmcp-tasks' in-process default
        (docket ``memory://``) -- zero external infrastructure.
    """

    server = FastMCP(EXERCISER_NAMESPACE, cache_ttl=cache_ttl, cache_scope=cache_scope)
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

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def agent_guarded_input(ctx: Context) -> Any:
        """Ask a question ONLY the SESSION'S AGENT can answer (C1-S7, #1309).

        Mirrors ``guarded_input``'s one-round MRTR shape exactly, except its
        ``ElicitRequest`` carries the ``x-clio-agent/audience: "agent"`` ``_meta``
        hint (:func:`_one_agent_elicit`) and its question asks for a value the
        server itself never told the client -- a nonce the TURN PROMPT planted
        earlier in the conversation. No human answering headlessly could ever
        produce the right value; only an agent holding this session's own
        transcript can. Proves the point leg_c2's avenue 12 ("agent-elicitation")
        exists to prove: the answer round-trips through the AGENT, not the human,
        and the server never sees a model, only a typed answer to its own
        declared schema.
        """

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={
                    "q1": _one_agent_elicit(
                        "What nonce did the user state earlier in this conversation?",
                        {
                            "type": "object",
                            "properties": {"nonce": {"type": "string"}},
                            "required": ["nonce"],
                        },
                    )
                },
                requestState="round-1",
                resultType="input_required",
            )
        answer = responses.get("q1")
        return f"agent-answered:{getattr(answer, 'content', None)}"

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

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def url_guarded_input_idn(ctx: Context) -> Any:
        """IDN counterpart of ``url_guarded_input`` (Opus review addendum, C1-S4).

        Identical shape, but elicits ``URL_GUARDED_INPUT_IDN_URL`` (an
        ``xn--`` ACE-encoded host) instead of the plain-ASCII origin --
        without this, the LIVE mrtr-url avenue can only ever observe
        ``punycode_warning=False`` and never actually exercises the
        ``warning=True`` branch it claims to prove (B5's homograph fix).
        """

        responses = ctx.input_responses
        if not responses:
            return mcp_types.InputRequiredResult(
                inputRequests={
                    "q1": _one_url_elicit("Authorize CLIO", url=URL_GUARDED_INPUT_IDN_URL)
                },
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

        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="color-scheme" content="light dark">
  <title>v2ex panel</title>
  <style>
    :root { font: 14px/1.5 system-ui, sans-serif; }
    body { margin: 0; color: CanvasText; background: Canvas; }
    main { display: grid; gap: 12px; padding: 24px; }
    h1, p { margin: 0; }
    h1 { font-size: 18px; }
    button {
      justify-self: start; border: 1px solid ButtonBorder; border-radius: 8px;
      padding: 8px 12px; color: ButtonText; background: ButtonFace; font: inherit;
    }
    button:focus-visible { outline: 2px solid Highlight; outline-offset: 2px; }
    button:disabled { opacity: .55; }
    [role=status] { min-height: 21px; color: GrayText; }
  </style>
</head>
<body>
  <main>
    <h1>V2EX interactive result</h1>
    <p id="payload">Waiting for tool input</p>
    <button id="continue" type="button" disabled>Continue with this result</button>
    <p id="delivery" role="status"></p>
  </main>
  <script>
  (() => {
    const target = window.parent;
    const send = (message) => target.postMessage(message, '*');
    const reportSize = () => requestAnimationFrame(() => send({
      jsonrpc:'2.0',
      method:'ui/notifications/size-changed',
      params:{height:Math.ceil(document.querySelector('main').getBoundingClientRect().bottom)}
    }));
    window.addEventListener('message', (event) => {
      const message = event.data;
      if (!message || message.jsonrpc !== '2.0') return;
      if (message.id === 'v2ex-init' && message.result) {
        send({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}});
        reportSize();
        return;
      }
      if (message.method === 'ui/notifications/tool-input') {
        const payload = message.params?.arguments?.payload;
        document.querySelector('#payload').textContent =
          typeof payload === 'string' ? `Result for ${payload}` : 'Result ready';
        document.querySelector('#continue').disabled = false;
        reportSize();
        return;
      }
      if (message.id === 'v2ex-message') {
        document.querySelector('#delivery').textContent =
          message.error ? message.error.message : 'Sent to the agent';
        reportSize();
      }
    });
    document.querySelector('#continue').addEventListener('click', () => {
      document.querySelector('#delivery').textContent = 'Sending';
      send({
        jsonrpc:'2.0',
        id:'v2ex-message',
        method:'ui/message',
        params:{
          role:'user',
          content:[{type:'text',text:'Continue from the V2EX interactive result'}]
        }
      });
    });
    send({
      jsonrpc:'2.0',
      id:'v2ex-init',
      method:'ui/initialize',
      params:{
        protocolVersion:'2026-01-26',
        appInfo:{name:'v2ex panel',version:'1.0.0'},
        appCapabilities:{availableDisplayModes:['inline']}
      }
    });
  })();
  </script>
</body>
</html>"""

    @server.tool
    async def header_annotated_echo(
        trace_id: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Trace-Id"})],
        payload: str,
    ) -> str:
        """Echo ``payload``; ``trace_id`` carries an ``x-mcp-header`` annotation
        (SEP-2578) -- the mcp SDK client mirrors it into a ``Mcp-Param-Trace-Id``
        request header on every ``tools/call`` (#1285 C1-S5 item 1)."""

        return f"trace={trace_id}:{payload}"

    @server.tool
    async def invalid_header_echo(
        amount: Annotated[float, Field(json_schema_extra={"x-mcp-header": "Amount"})],
    ) -> str:
        """Deliberately INVALID ``x-mcp-header`` (a ``number``-typed property --
        SEP-2578 forbids float→str mirroring). Never reachable: a modern-era
        client MUST drop this tool from ``tools/list`` (#1285 C1-S5 item 1)."""

        return f"amount:{amount}"

    @server.tool
    async def list_changed_target() -> str:
        """A tool whose VISIBILITY ``mutate_and_notify_list_changed`` toggles
        (#1285 C1-S5 item 2) -- the target the ``tools/list_changed`` avenue
        watches for."""

        return "target"

    @server.tool
    async def mutate_and_notify_list_changed(ctx: Context) -> str:
        """Hide ``list_changed_target`` and fire ``notifications/tools/list_changed``
        (#1285 C1-S5 item 2). ``ctx.disable_components`` is fastmcp's own session-
        visibility mutation, which sends the notification as a side effect --
        this tool exists purely to trigger it from a real tool call so a live
        ``subscriptions/listen`` watcher observes an ACTUAL registry mutation,
        not a synthetic notification with nothing behind it."""

        await ctx.disable_components(names={"list_changed_target"})
        return "list_changed_target hidden; notifications/tools/list_changed sent"

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
