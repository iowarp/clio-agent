"""P1.1 handshake-floor conformance: per-request ``_meta`` + ``server/discover``.

The 2026-07-28 revision removed the ``initialize`` handshake: a client instead
stamps per-request ``_meta`` carrying ``clientInfo`` + ``clientCapabilities`` on
every call, and probes ``server/discover`` on connect. FastMCP 4.0.0b1 provides
that plumbing natively; the tests below split into two kinds, explicitly:

* CONFORMANCE PINS — behavior FastMCP already satisfies, pinned so an upgrade
  cannot silently drop it (the ``_meta`` capability envelope; ``server/discover``
  consumption populating ``server_info``/``server_capabilities``).
* CLIO SEAM — behavior CLIO genuinely adds at the one stamping site
  (``make_mcp_client``): the true client identity in ``clientInfo`` (FastMCP
  defaults it to ``name='mcp'``), and the discovered server info surfaced through
  the handshake probe (:class:`MCPServerReport`).

Plus a deletion guard: no ``initialize``/``resources/subscribe``/``ping`` stub
survives on the modern MCP path (the #1111 deletion-inventory item).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastmcp import Client, Context, FastMCP

from clio_agent import __version__
from clio_agent.providers.handshake.mcp import _probe_one
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_runtime import make_mcp_client

# A stdio backend that echoes the ``_meta`` its OWN session received, so the test
# can read the exact wire envelope CLIO's factory-built client stamped upstream.
META_STUB = '''
from fastmcp import Context, FastMCP

mcp = FastMCP("meta-stub", instructions="meta stub instructions")

@mcp.tool
async def seen_meta(ctx: Context) -> dict:
    meta = getattr(ctx.request_context, "meta", None)
    return {"meta": dict(meta) if meta else None}

mcp.run()
'''

CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"
PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"


def _reap(needle: str) -> None:
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if needle in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            continue


def _meta_server() -> FastMCP:
    """In-memory server whose tool returns the ``_meta`` it received."""
    server = FastMCP("handshake-floor", instructions="reference server instructions")

    @server.tool
    async def seen_meta(ctx: Context) -> dict[str, Any]:
        meta = getattr(ctx.request_context, "meta", None)
        return {"meta": dict(meta) if meta else None}

    return server


# --------------------------------------------------------------------------- #
# CONFORMANCE PIN: FastMCP already stamps the per-request _meta envelope.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_meta_envelope_is_present() -> None:
    """Every outgoing request carries ``_meta`` with capabilities + protocol version."""
    async with make_mcp_client(_meta_server()) as client:
        result = await client.call_tool("seen_meta", {})

    meta = result.data["meta"]
    assert meta is not None, "no _meta on the tools/call request"
    assert CLIENT_CAPS_KEY in meta, "clientCapabilities absent from _meta"
    assert CLIENT_INFO_KEY in meta, "clientInfo absent from _meta"
    assert meta[PROTOCOL_KEY] == "2026-07-28"


# --------------------------------------------------------------------------- #
# CLIO SEAM: the factory stamps CLIO's true identity into clientInfo.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_factory_client_stamps_clio_client_info() -> None:
    """A factory-built client identifies as ``clio-agent`` (not FastMCP's ``mcp``)."""
    async with make_mcp_client(_meta_server()) as client:
        result = await client.call_tool("seen_meta", {})

    client_info = result.data["meta"][CLIENT_INFO_KEY]
    assert client_info["name"] == "clio-agent"
    assert client_info["version"] == __version__


def test_factory_forwards_client_info_kwarg() -> None:
    """The one stamping site forwards a CLIO ``client_info`` to the client class."""

    class _Fake:
        def __init__(self, target: Any, **kwargs: Any) -> None:
            self.target = target
            self.kwargs = kwargs

    client = make_mcp_client("t", client_cls=_Fake)
    info = client.kwargs.get("client_info")
    assert info is not None, "factory did not forward a client_info identity"
    assert info.name == "clio-agent"
    assert info.version == __version__


# --------------------------------------------------------------------------- #
# CONFORMANCE PIN: server/discover is consumed on connect.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_server_discover_populates_negotiated_state() -> None:
    """``server/discover`` returns and is parsed into the client's negotiated state."""
    async with make_mcp_client(_meta_server()) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info is not None
        assert client.server_capabilities is not None
        # Modern connections negotiate via DiscoverResult, never InitializeResult.
        assert client.initialize_result is None
        assert client.instructions == "reference server instructions"


# --------------------------------------------------------------------------- #
# CLIO SEAM: the handshake probe surfaces the discovered server info.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handshake_probe_records_discovered_server_info(tmp_path: Path) -> None:
    """``_probe_one`` records ``server/discover`` output on :class:`MCPServerReport`."""
    script = tmp_path / "meta_mcp.py"
    script.write_text(META_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="meta",
        transport="stdio",
        command=sys.executable,
        args=(str(script),),
    )
    try:
        report = await _probe_one(spec, timeout_s=30.0)
    finally:
        _reap("meta_mcp.py")

    assert report.ok, report.error
    assert report.protocol_version == "2026-07-28"
    assert report.server_version  # the backend's own version, from discover
    assert report.instructions == "meta stub instructions"


# --------------------------------------------------------------------------- #
# DELETION GUARD: no deprecated-protocol stubs on the modern MCP path.
# --------------------------------------------------------------------------- #


def test_no_deprecated_protocol_stubs_on_modern_path() -> None:
    """No ``initialize``/``resources/subscribe``/``ping`` call survives (#1111)."""
    root = Path(__file__).resolve().parents[2] / "src" / "clio_agent"
    modern_path = [
        *(root / "tools").glob("*.py"),
        *(root / "providers" / "handshake").glob("*.py"),
        root / "gact" / "routes" / "mcp.py",
    ]
    forbidden = re.compile(
        r"\.initialize\s*\(|resources/subscribe|\.subscribe\s*\(|PingRequest|\.ping\s*\(|SubscribeRequest"
    )
    offenders: list[str] = []
    for file in modern_path:
        for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{file.name}:{lineno}: {line.strip()}")
    assert not offenders, "deprecated-protocol stubs on the modern path:\n" + "\n".join(offenders)
