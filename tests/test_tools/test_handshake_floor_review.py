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

Finding 3 (the ``/v1/mcp/handshake`` endpoint surfacing the discovered fields via
the ``mcp_rows`` owner helper) is pinned in ``tests/test_gact/test_mcp_handshake.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastmcp import Client, FastMCP
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


@pytest.mark.asyncio
async def test_capability_envelope_no_declaration_is_empty() -> None:
    """No declaration, no handler -> empty clientCapabilities (honest default today)."""
    assert await _advertised_caps() == {}


@pytest.mark.asyncio
async def test_capability_envelope_declared_form_only_without_handler() -> None:
    """A form-only DECLARATION advertises form WITHOUT url and WITHOUT a live handler."""
    caps = await _advertised_caps(capabilities=MCPClientCapabilities(elicitation_form=True))
    assert caps == {"elicitation": {"form": {}}}


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
