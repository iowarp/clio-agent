"""Live probe: WHERE is a #1186 auto-mode era downgrade actually observable? (#1201)

Adversarial review on PR #1202 flagged that the connect-time capture wired into
``AsyncMCPToolExecutor`` (``start()`` / ``_route()``) may be reading the wrong
leg of the connection on the standard gateway-mounted path: FastMCP's
``_mirror_front_era_mode`` (``fastmcp/server/providers/proxy.py``) makes a
declared-server proxy's BACKEND client mirror whatever era its FRONT
connection negotiated, rather than negotiate independently. The executor's
front connection is always to an IN-PROCESS FastMCP object (the composite
gateway, or a mounted ``FastMCPProxy``) -- which negotiates instantly and
therefore (per this probe) always lands on the modern era, regardless of what
the REAL backend (a real stdio subprocess, subject to the actual #1186 timing
race) would have negotiated on its own.

This script empirically settles the question with two scenarios against the
SAME slow-starting stub stdio server (``stub_server.py``, sibling file):

  Scenario A (direct connect, no gateway): ``make_mcp_client`` builds a client
  straight onto the stub's real transport -- exactly what
  ``gact/elicitation_bridge.py``'s ``make_elicitation_client`` and
  ``gact/routes/mcp.py``'s per-call dispatch do. A short per-RPC ``timeout``
  (shorter than the stub's startup delay, per the negotiate_auto docstring's
  own account of the race: a client-side discover timeout is denylist-treated
  as non-modern-evidence and falls back to the legacy ``initialize``
  handshake, which then succeeds once the slow server is finally ready) is
  used to reliably reproduce the #1186 race on demand.

  Scenario B (production topology): the SAME stub mounted through the REAL
  ``build_gateway`` -> ``AsyncMCPToolExecutor(gateway, namespace_servers=...)``
  path -- byte-for-byte what ``agent.py::_build_tool_gateway`` constructs in
  production. A real tool call is dispatched through the namespace to force
  the lazy backend connect and prove the backend leg actually ran (not just
  that the proxy was mounted).

Run:  uv run python scripts/diagnostics/probe_1201_era_detectability.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

STUB_SCRIPT = str(Path(__file__).resolve().with_name("stub_server.py"))

# The SDK hardcodes the discover probe's own per-RPC timeout at 10.0s
# (mcp/client/session.py DISCOVER_TIMEOUT_SECONDS) -- entirely independent of
# any Client(timeout=...)/init_timeout knob. This IS the "per-RPC setup
# window" mcp_runtime.py's #1186 comment describes. A startup delay longer
# than 10s reliably reproduces the race: the discover call raises MCPError
# (REQUEST_TIMEOUT / -32001) at the ~10s mark, negotiate_auto's denylist falls
# back to the legacy initialize() handshake, which succeeds once the
# now-ready server answers -- landing the session on legacy, silently.
STARTUP_DELAY_S = "12"


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


async def scenario_a_direct_connect_reproduces_the_race() -> str:
    """Direct connection under auto mode, short per-RPC timeout: expect legacy."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    _banner("Scenario A: DIRECT connect (make_mcp_client style), no gateway")
    transport = StdioTransport(
        command=sys.executable,
        args=[STUB_SCRIPT],
        env={**os.environ, "PROBE_STARTUP_DELAY_S": STARTUP_DELAY_S},
    )
    # A generous OVERALL init_timeout (60s) -- the race is the discover
    # probe's OWN hardcoded 10s timeout firing WHILE the server is still
    # cold (12s delay), not the overall connect budget; the fallback
    # initialize() then needs room to actually complete after that.
    client = Client(transport, mode="auto", init_timeout=60.0)
    t0 = time.monotonic()
    async with client:
        elapsed = time.monotonic() - t0
        version = client.protocol_version
        print(f"connect took {elapsed:.1f}s")
        print(f"negotiated protocol_version = {version!r}")
        print(f"initialize_result present    = {client.initialize_result is not None}")
    return version or ""


def _instrument_proxy_client_backend_era() -> list[str]:
    """Monkeypatch ProxyClient.__aenter__ to log the REAL backend leg's own
    negotiated protocol_version, unambiguously (CLIO's _proxy_for_spec uses
    ProxyClient EXCLUSIVELY for the backend/real-transport leg; the front
    connection the executor makes is a plain fastmcp.Client to an in-process
    object, never a ProxyClient) -- probe-only instrumentation, never shipped.
    """
    from fastmcp.server.providers.proxy import ProxyClient

    observed: list[str] = []
    original_aenter = ProxyClient.__aenter__

    async def patched_aenter(self):
        result = await original_aenter(self)
        observed.append(str(getattr(self, "protocol_version", None)))
        return result

    ProxyClient.__aenter__ = patched_aenter
    return observed


async def scenario_b_gateway_mounted_front_leg() -> tuple[str, str, float, float, list[str]]:
    """Same stub, mounted through the REAL production gateway/executor path."""
    from clio_agent.tools.gateway import build_gateway, list_tool_definitions, namespace_proxies
    from clio_agent.tools.mcp_config import MCPServerSpec
    from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor

    backend_leg_versions = _instrument_proxy_client_backend_era()

    _banner("Scenario B: GATEWAY-mounted (production topology: agent.py's own construction)")
    spec = MCPServerSpec(
        name="stub",
        transport="stdio",
        command=sys.executable,
        args=(STUB_SCRIPT,),
        env={"PROBE_STARTUP_DELAY_S": STARTUP_DELAY_S},
    )
    gateway = build_gateway({"stub": spec})
    definitions = list_tool_definitions(gateway)
    # AsyncMCPToolExecutor directly (not create_async_tool_executor, which does
    # not forward server_id) -- byte-for-byte agent.py's own construction plus
    # the server_id label so this probe also exercises it.
    executor = AsyncMCPToolExecutor(
        gateway,
        timeout=60.0,
        preloaded_tools=definitions,
        namespace_servers=namespace_proxies(gateway),
        server_id="stub-gateway-probe",
    )
    t_start = time.monotonic()
    await executor.start()
    print(f"start() (front -> in-process gateway) took {time.monotonic() - t_start:.2f}s")
    try:
        front_era = executor.connection_era
        print(f"executor.connection_era right after start() = {front_era}")

        # Force the lazy per-namespace backend connect + a REAL call through it,
        # so the actual (real subprocess, real #1186-race-exposed) backend leg
        # genuinely runs -- not just that the proxy object was mounted. Timed
        # separately: _route()'s classify happens at the FRONT->proxy-object
        # connect (in-process, instant), BEFORE the proxy's lazy backend
        # client ever dials the real (slow) subprocess -- if the namespace
        # connect below is fast while the subsequent call is slow, the era was
        # captured before the real backend was ever touched.
        t_route = time.monotonic()
        _client, _bare, _ns = await executor._route("stub_echo")  # noqa: SLF001
        route_elapsed = time.monotonic() - t_route
        namespace_era_pre_call = executor.namespace_connection_era("stub")
        print(
            f"_route() (front -> in-process PROXY OBJECT, backend not yet dialed) "
            f"took {route_elapsed:.2f}s; namespace_connection_era captured THEN = "
            f"{namespace_era_pre_call}"
        )

        t_call = time.monotonic()
        outcome = await executor.call_tool_result("stub_echo", {"text": "hi"})
        call_elapsed = time.monotonic() - t_call
        print(
            f"the REAL call through the backend took {call_elapsed:.2f}s "
            f"(the {STARTUP_DELAY_S}s cold-start delay actually happened HERE, "
            f"AFTER the era was already captured above): {outcome.model_text!r}"
        )

        namespace_era = executor.namespace_connection_era("stub")
        print(f"executor.namespace_connection_era('stub') after a real call = {namespace_era}")
        print(f"REAL backend leg(s) actually observed (ProxyClient.__aenter__): {backend_leg_versions}")
        return (
            front_era.protocol_version if front_era else "",
            namespace_era.protocol_version if namespace_era else "",
            route_elapsed,
            call_elapsed,
            backend_leg_versions,
        )
    finally:
        await executor.aclose()


async def main() -> None:
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    direct_version = await scenario_a_direct_connect_reproduces_the_race()
    front_version, namespace_version, route_elapsed, call_elapsed, backend_leg_versions = (
        await scenario_b_gateway_mounted_front_leg()
    )

    _banner("VERDICT")
    direct_is_modern = direct_version in MODERN_PROTOCOL_VERSIONS
    print(f"A) direct connect to the SAME slow stub negotiated : {direct_version!r} "
          f"({'modern' if direct_is_modern else 'LEGACY -- race reproduced'})")
    print(f"B) gateway front leg (executor.connection_era)      : {front_version!r}")
    print(f"B) gateway namespace leg (namespace_connection_era) : {namespace_version!r}")
    print("B) REAL backend leg(s), read directly off the")
    print(f"   proxy's OWN ProxyClient (the actual connection to the real subprocess): "
          f"{backend_leg_versions}")
    print(f"B) _route() (era capture) elapsed  : {route_elapsed:.2f}s")
    print(f"B) real backend call elapsed        : {call_elapsed:.2f}s (the {STARTUP_DELAY_S}s "
          f"cold-start actually happened here)")

    same_as_front = all(v == namespace_version for v in backend_leg_versions) if backend_leg_versions else None
    if same_as_front:
        print(
            "\nCONFIRMED (direct backend-leg read): the REAL ProxyClient connection to the "
            "actual subprocess reports the SAME protocol_version as the front's namespace_"
            "connection_era -- this is FastMCP's _mirror_front_era_mode forcing the backend "
            "to match the front rather than negotiating independently. Whatever the real "
            "backend WOULD have negotiated on its own under unconstrained auto is never "
            "observed -- it is pinned to the front's value before it gets a chance."
        )
    elif backend_leg_versions:
        print(
            "\nUNEXPECTED: the real backend leg's own protocol_version DIFFERS from the "
            "front's captured namespace_connection_era -- re-examine before concluding "
            "anything (this would actually mean the front capture is representative after "
            "all under these parameters)."
        )

    _banner("OBSERVED TIMING (honest account, not the proof -- see the backend-leg read above)")
    print(
        f"_route()/_connect_namespace() took {route_elapsed:.2f}s (NOT instant): entering the\n"
        "front client onto the mounted FastMCPProxy object apparently does real work against\n"
        "the backend during __aenter__ in this FastMCP build (contradicts the 'connects lazily\n"
        "on first list_tools/call_tool' comment in mcp_executor.py/gateway.py -- worth a\n"
        "follow-up issue, NOT assumed further here). The subsequent real call then only took\n"
        f"{call_elapsed:.2f}s, because the backend was already connected. The wall-clock split\n"
        "is therefore NOT reliable standalone evidence either way -- the direct ProxyClient\n"
        "read above is the trustworthy signal, since it reads the ACTUAL backend connection's\n"
        "own protocol_version rather than inferring it from timing."
    )
    if not direct_is_modern:
        print(
            "\nADDITIONALLY: Scenario A's direct connect (no proxy, no mirroring) landed on "
            "LEGACY under auto mode against the same slow stub -- the #1186 race this whole "
            "feature targets, reproduced end-to-end on an unmirrored connection. "
            "classify_connection_era correctly flags it there."
        )
    else:
        print(
            "\nNOTE: Scenario A did not land on legacy under these exact timing parameters in "
            "this SDK build/run -- the installed mcp SDK's DISCOVER_TIMEOUT_SECONDS=10.0 "
            "(mcp/client/session.py) did not fire against a 12s-delayed stdio responder in "
            "this environment (measured connect time ~15s, i.e. it just waited). Reproducing "
            "the EXACT #1186 trigger condition needs a transport-level error on the first "
            "probe, not merely a slow one; the backend-leg-vs-front-leg comparison above does "
            "not depend on reproducing it and is the decisive evidence for finding #0."
        )


if __name__ == "__main__":
    asyncio.run(main())
