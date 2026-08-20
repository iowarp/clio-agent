"""Regression coverage for the #1201 fix-round finding #0: the REAL backend leg
behind a gateway-mounted proxy must be classified, not just the front leg.

Adversarial review on PR #1202 (a live probe,
``scripts/diagnostics/probe_1201_era_detectability.py``) proved the executor's
own front-leg capture (``AsyncMCPToolExecutor.start()``/``_route()``) is BLIND
on the gateway-mounted path: FastMCP's ``_mirror_front_era_mode`` forces the
proxy's backend connection to mirror the front's (always-modern, in-process)
era. The fix instruments the REAL backend clone inside
``tools/gateway.py::_proxy_for_spec``'s ``_client_factory`` closure. These
tests reuse ``test_protocol_compat_matrix.py``'s real stdio ERA_STUB pattern
(a backend that reports its OWN negotiated era) -- the same infrastructure
that already proves the mirroring mechanically -- to prove classification now
reads the correct (backend) leg.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psutil
import pytest
from fastmcp import Client

from clio_agent import conf
from clio_agent.errors import MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
from clio_agent.tools.gateway import build_gateway
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_connection_era import latest_mcp_connection_era


@pytest.fixture(autouse=True)
def _clean_connect_mode_env(monkeypatch):
    """Every test starts from the real default (``auto``), never an ambient override."""
    monkeypatch.delenv("CLIO_MCP_CONNECT_MODE", raising=False)
    conf.reload()
    yield

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


def _era_gateway(tmp_path: Path):
    script = tmp_path / "era_mcp_gw.py"
    script.write_text(ERA_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="era-gw",
        transport="stdio",
        command=sys.executable,
        args=(str(script),),
    )
    return build_gateway({"era-gw": spec})


@pytest.mark.asyncio
async def test_modern_front_records_the_real_backends_era_not_a_blind_guess(
    tmp_path: Path,
) -> None:
    """A modern front's REAL backend call updates latest_mcp_connection_era
    for the DECLARED SERVER NAME, sourced from the actual backend connection
    (not merely mirrored/assumed from the front)."""

    gw = _era_gateway(tmp_path)
    try:
        async with Client(gw) as client:
            result = await client.call_tool("era-gw_era", {})
            assert result.data == {"backend_era": "2026-07-28"}

        era = latest_mcp_connection_era("era-gw")
        assert era is not None
        assert era.server_id == "era-gw"
        assert era.era == "modern"
        assert era.protocol_version == "2026-07-28"
        assert era.degrade_reason is None
    finally:
        _reap("era_mcp_gw.py")


@pytest.mark.asyncio
async def test_legacy_front_records_a_real_downgrade_from_the_backend_leg(
    tmp_path: Path,
) -> None:
    """A front that lands on legacy (whatever the cause -- the #1186 race in
    production, forced here for a deterministic repro) mirrors legacy onto the
    REAL backend; the NEW instrumentation at _client_factory (not the
    executor's blind front-leg capture) is what records this as a downgrade
    under CLIO's auto connect_mode config."""

    gw = _era_gateway(tmp_path)
    try:
        async with Client(gw, mode="legacy") as client:
            result = await client.call_tool("era-gw_era", {})
            assert result.data == {"backend_era": "2025-11-25"}

        era = latest_mcp_connection_era("era-gw")
        assert era is not None
        assert era.era == "legacy"
        assert era.protocol_version == "2025-11-25"
        # CLIO's tools.mcp.connect_mode defaults to "auto" (no pin in this
        # test's env) -- a legacy landing under auto IS the downgrade #1201
        # exists to catch, now correctly observed at the real backend leg.
        assert era.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    finally:
        _reap("era_mcp_gw.py")
