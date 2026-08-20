"""In-process MCP compatibility proofs and pinned cross-version evidence.

The tests below prove the two legs available in one FastMCP 4 process:

* a default new client and new server negotiate ``2026-07-28`` and round-trip;
* a legacy-mode client negotiates ``2025-11-25`` with a new server and round-trips,
  exercising the new server's legacy-handshake acceptance.

The P0.3 spike additionally ran separate pinned stdio virtual environments. A
FastMCP 4.0.0b1 client downgraded to ``2025-11-25`` against a FastMCP 3.2.4
server and returned ``{"echo": "hi"}``; a FastMCP 3.2.4 client called the same
tool successfully on a FastMCP 4.0.0b1 server (the old client exposes no
negotiated-version attribute). The old-client/old-server baseline also
round-tripped. Those process-isolated legs cannot import both incompatible MCP
SDK major versions into this test process, so their verified stdio evidence is
recorded here rather than simulated.
"""

import sys
from pathlib import Path

import psutil
import pytest
from fastmcp import Client, FastMCP

from clio_agent.tools.gateway import build_gateway
from clio_agent.tools.mcp_config import MCPServerSpec

# A stdio backend whose one tool reports the protocol era its OWN (backend)
# session negotiated. Reachable through the real gateway proxy path, it lets the
# test assert BOTH legs of the chain, proving the proxy mirrors the front era.
ERA_STUB = '''
from fastmcp import Context, FastMCP

mcp = FastMCP("era")

@mcp.tool
async def era(ctx: Context) -> dict:
    rc = ctx.request_context
    return {"backend_era": getattr(rc, "protocol_version", None)}

mcp.run()
'''


def _reap(needle: str) -> None:
    """Kill any lingering stdio backend the proxy kept alive (keep_alive=True)."""
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if needle in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            continue


def _echo_server() -> FastMCP:
    """Return the in-memory reference server used by each matrix leg."""
    server = FastMCP("protocol-compat-matrix")

    @server.tool
    def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    return server


@pytest.mark.asyncio
async def test_new_client_to_new_server() -> None:
    """The current line negotiates 2026-07-28 and round-trips a tool call."""
    async with Client(_echo_server()) as client:
        result = await client.call_tool("echo", {"text": "hi"})

        assert client.protocol_version == "2026-07-28"
        assert result.data == {"echo": "hi"}


@pytest.mark.asyncio
async def test_legacy_handshake_to_new_server() -> None:
    """The new server accepts the legacy protocol era and round-trips."""
    async with Client(_echo_server(), mode="legacy") as client:
        result = await client.call_tool("echo", {"text": "hi"})

        assert client.protocol_version == "2025-11-25"
        assert result.data == {"echo": "hi"}


@pytest.mark.asyncio
async def test_gateway_mirrors_front_era_to_backend(tmp_path: Path) -> None:
    """Through the ACTUAL gateway proxy (finding #3): the backend leg speaks the
    SAME protocol era the front negotiated. A modern front reaches a modern
    backend (both 2026-07-28); a legacy front reaches a legacy backend (both
    2025-11-25). Passing a prebuilt Client to create_proxy used to pin the
    backend to ``auto`` and let a legacy front cross to a modern backend.
    """

    script = tmp_path / "era_mcp.py"
    script.write_text(ERA_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="era",
        transport="stdio",
        command=sys.executable,
        args=(str(script),),
    )
    gw = build_gateway({"era": spec})  # real _proxy_for_spec -> transport -> mirror
    try:
        # Modern front: both legs at 2026-07-28.
        async with Client(gw) as client:
            assert client.protocol_version == "2026-07-28"
            result = await client.call_tool("era_era", {})
            assert result.data == {"backend_era": "2026-07-28"}

        # Legacy front: both legs at 2025-11-25.
        async with Client(gw, mode="legacy") as client:
            assert client.protocol_version == "2025-11-25"
            result = await client.call_tool("era_era", {})
            assert result.data == {"backend_era": "2025-11-25"}
    finally:
        _reap("era_mcp.py")
