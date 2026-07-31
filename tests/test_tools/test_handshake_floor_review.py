"""P1.1 handshake-floor — review fix-round conformance (#1111 Codex review).

Covers the four accepted findings:

1. The PRIMARY (no-handler) gateway proxy branch stamps CLIO identity downstream
   (it previously leaked FastMCP's default ``mcp/0.1.0``).
2. Client capability DECLARATION is decoupled from handler activation: a typed
   declaration controls the advertised ``clientCapabilities`` regardless of
   whether a live handler is wired — pinned across three envelope states
   (no-capability / declared form-only / wired-handler).
4. The per-request ``_meta`` (clientInfo + clientCapabilities) is stamped across
   representative request families INCLUDING ``server/discover`` (which the SDK
   builds via a separate ``send_discover`` path).

Plus the verify-fix round: the handler proxy branch preserves ProxyClient
forwarding (structurally AND behaviorally — hook overrides its own domain while
sampling forwards to the front and structured results relay), and an explicit
empty capability declaration is authoritative PER DOMAIN IT MODELS (elicitation)
while leaving unmodeled domains (sampling/roots) truthfully base-derived.

Finding 3 (the ``/v1/mcp/handshake`` endpoint surfacing the discovered fields via
the ``mcp_rows`` owner helper) is pinned in ``tests/test_gact/test_mcp_handshake.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import psutil
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware

from clio_agent import __version__
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_handlers import MCPClientCapabilities
from clio_agent.tools.mcp_runtime import MCPClientHandlers, make_mcp_client

CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"

# stdio backend that reports the clientInfo/capabilities its OWN session received,
# reachable through the real gateway proxy so the downstream leg is asserted.
ID_STUB = '''
from fastmcp import Context, FastMCP

mcp = FastMCP("id-backend")

@mcp.tool
async def whoami(ctx: Context) -> dict:
    meta = getattr(ctx.request_context, "meta", None)
    d = dict(meta) if meta else {}
    return {
        "client_info": d.get("io.modelcontextprotocol/clientInfo"),
        "capabilities": d.get("io.modelcontextprotocol/clientCapabilities"),
    }

mcp.run()
'''


def _reap(needle: str) -> None:
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if needle in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            continue


class _MetaRecorder(Middleware):
    """Server middleware recording the reserved ``_meta`` per request method."""

    def __init__(self) -> None:
        self.by_method: dict[str, dict[str, Any]] = {}

    async def on_request(self, context: Any, call_next: Any) -> Any:
        method = getattr(context, "method", None)
        try:
            meta = get_context().request_context.meta
        except RuntimeError:
            meta = None
        if method is not None:
            self.by_method[method] = dict(meta) if meta else {}
        return await call_next(context)


def _capture_server(recorder: _MetaRecorder) -> FastMCP:
    server = FastMCP("floor-review")
    server.add_middleware(recorder)

    @server.tool
    def echo(text: str) -> str:
        return text

    @server.prompt
    def greet() -> str:
        return "hi"

    return server


# --------------------------------------------------------------------------- #
# Finding 4: _meta across request families, INCLUDING server/discover.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_meta_stamped_across_request_families_including_discover() -> None:
    """clientInfo + clientCapabilities ride tools/call, tools/list, prompts/list, discover."""
    recorder = _MetaRecorder()
    async with make_mcp_client(_capture_server(recorder)) as client:
        await client.list_tools()
        await client.call_tool("echo", {"text": "x"})
        await client.list_prompts()

    families = {"server/discover", "tools/list", "tools/call", "prompts/list"}
    assert families <= set(recorder.by_method), (
        f"missing families: {families - set(recorder.by_method)}"
    )
    for method in families:
        meta = recorder.by_method[method]
        assert CLIENT_INFO_KEY in meta, f"{method} missing clientInfo"
        assert CLIENT_CAPS_KEY in meta, f"{method} missing clientCapabilities"
        assert meta[CLIENT_INFO_KEY]["name"] == "clio-agent", f"{method} wrong identity"


# --------------------------------------------------------------------------- #
# Finding 2: capability declaration decoupled from handler activation.
# --------------------------------------------------------------------------- #


async def _advertised_caps(
    *,
    capabilities: MCPClientCapabilities | None = None,
    handlers: MCPClientHandlers | None = None,
) -> dict[str, Any]:
    """Return the clientCapabilities a factory-built client advertises to a server."""
    recorder = _MetaRecorder()
    async with make_mcp_client(
        _capture_server(recorder), capabilities=capabilities, handlers=handlers
    ) as client:
        await client.call_tool("echo", {"text": "x"})
    return recorder.by_method["tools/call"][CLIENT_CAPS_KEY]


def _capability_domains(caps: dict[str, Any]) -> dict[str, Any]:
    """The capability DOMAINS advertised, without the extension declarations.

    #1115 added the SEP-2663 tasks extension to every execution-path client, so
    ``clientCapabilities`` now always carries an ``extensions`` key. That key is a
    registry of declared extensions, not a capability domain the
    :class:`MCPClientCapabilities` declaration models, so the per-domain assertions
    below read the envelope with it split off — and assert on it separately in
    :func:`test_capability_envelope_always_declares_the_tasks_extension`.
    """

    return {key: value for key, value in caps.items() if key != "extensions"}


@pytest.mark.asyncio
async def test_capability_envelope_no_declaration_is_empty() -> None:
    """No declaration, no handler -> no capability DOMAIN advertised."""
    assert _capability_domains(await _advertised_caps()) == {}


@pytest.mark.asyncio
async def test_capability_envelope_always_declares_the_tasks_extension() -> None:
    """#1115: every execution-path client declares io.modelcontextprotocol/tasks.

    The extension declaration is orthogonal to the elicitation DOMAIN declaration:
    it rides the same ``clientCapabilities`` envelope but is what tells a
    task-serving backend it may run a call as a background task.
    """
    caps = await _advertised_caps()
    assert caps["extensions"] == {"io.modelcontextprotocol/tasks": {}}


@pytest.mark.asyncio
async def test_capability_envelope_declared_form_only_without_handler() -> None:
    """A form-only DECLARATION advertises form WITHOUT url and WITHOUT a live handler."""
    caps = await _advertised_caps(capabilities=MCPClientCapabilities(elicitation_form=True))
    assert _capability_domains(caps) == {"elicitation": {"form": {}}}


@pytest.mark.asyncio
async def test_capability_envelope_wired_handler_advertises_both_modes() -> None:
    """Wiring an elicitation handler (no declaration) advertises the SDK's both-modes.

    This is exactly why the decoupled declaration exists: a bare wired handler
    over-advertises ``url``; the declaration (previous test) constrains it.
    """

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    caps = await _advertised_caps(handlers=MCPClientHandlers(elicitation=elicit))
    assert caps.get("elicitation") == {"form": {}, "url": {}}


@pytest.mark.asyncio
async def test_capability_declaration_overrides_wired_handler_to_form_only() -> None:
    """Declaration is authoritative: form-only declared + handler wired -> form only."""
    async def elicit(context: Any, *a: Any) -> Any:
        return None

    caps = await _advertised_caps(
        capabilities=MCPClientCapabilities(elicitation_form=True),
        handlers=MCPClientHandlers(elicitation=elicit),
    )
    assert caps.get("elicitation") == {"form": {}}


@pytest.mark.asyncio
async def test_empty_declaration_pins_elicitation_absent_on_direct_client() -> None:
    """Empty declaration pins the elicitation DOMAIN absent on a direct client.

    A declaration is authoritative only for the domain it models (elicitation). On
    a plain client nothing else is wired, so with an elicitation handler wired the
    empty declaration removes elicitation and the COMPLETE advertised envelope is
    genuinely ``{}`` — asserted by exact equality, not mere elicitation absence.
    """

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    caps = await _advertised_caps(
        capabilities=MCPClientCapabilities(),  # explicit empty -> authoritative for elicitation
        handlers=MCPClientHandlers(elicitation=elicit),
    )
    # The whole envelope's DOMAINS, exactly — nothing else was wired here. (The
    # #1115 tasks extension declaration is not a domain; see _capability_domains.)
    assert _capability_domains(caps) == {}


@pytest.mark.asyncio
async def test_empty_declaration_pins_elicitation_absent_but_keeps_forwarding() -> None:
    """Empty declaration through a ProxyClient ``.new()`` clone pins elicitation
    absent while sampling/roots forwarding REMAINS advertised.

    This is the ruled per-domain contract, not an accepted leak: the declaration
    models only elicitation, so it removes the SDK's over-advertised elicitation
    form+url; sampling and roots stay because a proxy backend genuinely forwards
    those server-initiated requests to the front — a truthful advertisement, and
    clearing it would sever push-forwarding.
    """
    from fastmcp.server.providers.proxy import ProxyClient

    recorder = _MetaRecorder()
    base = make_mcp_client(
        _capture_server(recorder),
        capabilities=MCPClientCapabilities(),  # explicit empty
        client_cls=ProxyClient,
    )
    clone = base.new()  # the per-request clone the proxy would build
    clone.mode = "2026-07-28"  # a modern front (the gateway mirrors this per request)
    async with clone as client:
        await client.call_tool("echo", {"text": "x"})

    caps = recorder.by_method["tools/call"][CLIENT_CAPS_KEY]
    assert "elicitation" not in caps  # over-advertised form+url removed
    assert "sampling" in caps  # proxy forwarding preserved (truthful, unmodeled domain)
    assert "roots" in caps


# --------------------------------------------------------------------------- #
# Finding 1: the PRIMARY (no-handler) gateway proxy branch stamps identity.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_handler_gateway_backend_receives_clio_identity(tmp_path: Path) -> None:
    """Through the real gateway proxy, a downstream declared server sees clio-agent."""
    from clio_agent.tools.gateway import build_gateway

    script = tmp_path / "id_mcp.py"
    script.write_text(ID_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="idb", transport="stdio", command=sys.executable, args=(str(script),)
    )
    gw = build_gateway({"idb": spec})  # PRIMARY no-handler production path
    try:
        async with Client(gw) as client:
            result = await client.call_tool("idb_whoami", {})
    finally:
        _reap("id_mcp.py")

    assert result.data["client_info"]["name"] == "clio-agent"
    assert result.data["client_info"]["version"] == __version__


# --------------------------------------------------------------------------- #
# Remnant 1: the HANDLER-populated proxy branch preserves ProxyClient forwarding.
# --------------------------------------------------------------------------- #


def test_handler_proxy_branch_preserves_proxyclient_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler+declaration proxy backend is a ProxyClient with forwarding intact.

    The per-request clone must (a) be a ``ProxyClient`` whose session class descends
    from ``_ForwardingClientSession`` (composed with the capability override), (b)
    forward the caller's authorization header to HTTP backends, and (c) keep
    FastMCP's sampling / roots / log push-forwarding handlers — while CLIO's own
    elicitation dispatcher replaces only the elicitation handler. A plain-``Client``
    handler branch (the remnant) would fail every one of these.
    """
    from fastmcp.server.providers.proxy import ProxyClient, _ForwardingClientSession

    from clio_agent.tools import gateway

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    # An in-memory server object as the transport target so no subprocess spawns.
    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: FastMCP("fwd-stub"))
    proxy = gateway._proxy_for_spec(
        MCPServerSpec(name="ext", transport="stdio", command="x"),
        handlers=MCPClientHandlers(elicitation=elicit),
        capabilities=MCPClientCapabilities(elicitation_form=True),
    )
    clone = proxy.client_factory()

    # (a) ProxyClient with a forwarding session class (subclassed for capabilities).
    assert isinstance(clone, ProxyClient)
    assert issubclass(clone._transport_options.session_class, _ForwardingClientSession)
    # (b) caller authorization is forwarded to HTTP backends.
    assert clone._transport_options.forward_incoming_headers is True
    # (c) forwarding push-handlers preserved (restored per request); CLIO's hook
    # replaced ONLY elicitation. `_proxy_restoring_handler_keys` names the handlers
    # ProxyClient defaulted to forwarding — CLIO's elicitation is absent from it.
    restoring = clone._proxy_restoring_handler_keys
    assert {"sampling_handler", "roots", "log_handler"} <= restoring
    assert "elicitation_handler" not in restoring
    # The forwarding callbacks are actually installed on the session.
    for key in ("sampling_callback", "list_roots_callback", "logging_callback"):
        assert clone._session_kwargs.get(key) is not None


@pytest.mark.asyncio
async def test_handler_gateway_behavior_hook_override_and_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BEHAVIORAL proof through a handler-populated gateway over an in-memory backend.

    Complements the structural test above by exercising real traffic across the
    proxy (fastmcp in-memory transport, no subprocess):

    * elicit leg — a backend tool calls ``ctx.elicit``; CLIO's wired elicitation
      hook services it (hook observed firing; the backend receives the response),
      proving the hook OVERRIDES only its own domain.
    * sample leg — a backend tool requests sampling; the request forwards through
      the proxy to the FRONT client's sampling handler, proving unhandled
      push-forwarding survives on the handler-populated path.
    * result-relay leg — a tool returning structured content arrives intact through
      the proxy (no mid-proxy output-schema rejection).

    ERA NOTE (API evidence): server-initiated elicitation/sampling exist only on
    the legacy/handshake era. On a 2026-07-28 connection fastmcp raises
    ``ToolError("elicitation via server-initiated requests is unavailable on
    2026-07-28 connections")`` and sampling is deprecated (SEP-2577), because the
    revision statelessified server->client requests. So the two forwarding legs are
    driven through a LEGACY front (the gateway mirrors the era to a legacy backend),
    which is exactly the era whose push-forwarding this branch must preserve. No leg
    is dropped; the result-relay leg is era-agnostic.
    """
    from clio_agent.tools import gateway

    fired = {"elicit": False, "sample": False}
    backend = FastMCP("behavioral-backend")

    @backend.tool
    async def ask(ctx: Context) -> dict[str, Any]:
        result = await ctx.elicit("your name?", response_type=str)
        return {"answer": getattr(result, "data", None), "kind": type(result).__name__}

    @backend.tool
    async def wants_sample(ctx: Context) -> dict[str, Any]:
        reply = await ctx.session.create_message(
            messages=[
                mcp_types.SamplingMessage(
                    role="user", content=mcp_types.TextContent(type="text", text="hi")
                )
            ],
            max_tokens=16,
        )
        return {"sampled": getattr(reply.content, "text", str(reply.content))}

    @backend.tool
    async def structured() -> dict[str, Any]:
        return {"a": 1, "nested": {"b": [1, 2, 3]}}

    async def clio_elicit(context: Any, message: str, response_type: Any, params: Any, rc: Any) -> Any:
        fired["elicit"] = True
        return "clio-answer"

    async def front_sampling(messages: Any, params: Any, context: Any) -> str:
        fired["sample"] = True
        return "front-sampled-text"

    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: backend)
    gw = gateway.build_gateway(
        {"bk": MCPServerSpec(name="bk", transport="stdio", command="x")},
        handlers=MCPClientHandlers(elicitation=clio_elicit),
    )

    # Legacy front -> the gateway mirrors a legacy backend, where server-initiated
    # elicit/sample (the forwarding this branch preserves) are available.
    async with Client(gw, mode="legacy", sampling_handler=front_sampling) as client:
        elicit_result = await client.call_tool("bk_ask", {})
        sample_result = await client.call_tool("bk_wants_sample", {})
        relay_result = await client.call_tool("bk_structured", {})

    # elicit leg: CLIO's hook serviced it (not forwarded); backend got the answer.
    assert fired["elicit"] is True
    assert elicit_result.data["answer"] == "clio-answer"
    # sample leg: forwarded through the proxy to the FRONT sampling handler.
    assert fired["sample"] is True
    assert sample_result.data["sampled"] == "front-sampled-text"
    # result-relay leg: structured content intact through the proxy.
    assert relay_result.data == {"a": 1, "nested": {"b": [1, 2, 3]}}
