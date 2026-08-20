"""Regression coverage for a live MCP tool-dispatch defect: a real backend
connect/protocol failure was silently reported to the model as "Unknown
tool" -- indistinguishable from a hallucinated tool name -- and, once that
was fixed, the honestly-surfaced failure turned out to be a SECOND, deeper
defect: namespace-direct backend connects never actually negotiate with a
legacy-only backend at all.

Live symptom (fully reproduced, twice): calling ``spotter_list_runs`` /
``workload_run_campaign`` against pack-mounted MCP servers (spotter-ai,
phenotype -- legacy fastmcp 3.4.7 stdio backends invoked by a direct exe
path) failed with ``fastmcp.exceptions.ToolError: Unknown tool: 'list_runs'``
even though the backend genuinely has that tool (proven live via a standalone
probe) and clio's own tool catalog correctly lists the prefixed name.

Root cause #1, traced through the REAL dispatch path
(``AsyncMCPToolExecutor._route`` / ``_connect_namespace`` in
``tools/mcp_executor.py``, namespace-direct routing added by #932):

* ``tools/gateway.py::_proxy_for_spec`` mounts each declared server behind a
  ``fastmcp.server.providers.proxy.FastMCPProxy``. ``FastMCPProxy`` IS a
  fastmcp ``AggregateProvider`` wrapping exactly ONE child provider (its own
  ``ProxyProvider`` around the one backend) -- there is never a second
  provider to fall through to.
* ``AggregateProvider`` defaults to ``provider_error_strategy="warn"``. When
  the single child provider's ``get_tool()``/``list_tools()`` raises ANY
  non-``NotFoundError`` exception -- a genuine backend connect/protocol
  failure included -- ``aggregate.py::_get_highest_version_result`` logs it
  (``"Error during get_tool(...) from provider ProxyProvider(): ..."``,
  reproduced verbatim below) and returns ``None`` for the tool.
* fastmcp's ``FastMCP.call_tool()`` (server.py) reads that ``None`` as "no
  such tool" and raises ``NotFoundError(f"Unknown tool: {name!r}")``, which
  the client re-raises as ``fastmcp.exceptions.ToolError`` -- the exact
  observed symptom, for ANY reason the backend leg failed, not just a truly
  absent tool.

Fix #1: ``_proxy_for_spec`` builds its ``FastMCPProxy`` with
``provider_error_strategy="raise"`` so a real single-provider backend failure
propagates as itself instead of being downgraded to a fabricated "not found".
This changes nothing for an ACTUALLY missing tool: ``aggregate.py`` special-
cases ``NotFoundError`` to keep degrading quietly regardless of this setting
(covered by ``test_missing_tool_still_reports_not_found`` below).

Root cause #2 (owner ruling 2026-08-14, found once #1 stopped hiding it):
once honest, the SAME live race turned out to be 100% DETERMINISTIC on a
quiet box, not timing -- fastmcp's ``_mirror_front_era_mode()`` pins a
namespace-direct backend connect to the front's era. Our front
(``Client(proxy)`` in ``mcp_executor.py``) is ALWAYS modern (in-process,
instant), so the mirror ALWAYS returns an exact modern version string for
every declared backend. ``Client._negotiate()``'s pinned-exact-version branch
FABRICATES a ``DiscoverResult`` locally
(``session.adopt(_synthesize_discover(mode))``) and never contacts the peer
at all -- a genuinely legacy-only backend (fastmcp 3.4.7 in an
agent-blueprint's own venv) then never receives the ``initialize`` request
its session-lifecycle state machine requires as its FIRST message
(``mcp/server/session.py::_received_request``, the installed SDK's own
gate), so it rejects every later request forever: "Received request before
initialization was complete", surfaced client-side as
``MCPError(-32602, 'Invalid request parameters')``.

Fix #2: ``_proxy_for_spec``'s ``_client_factory`` no longer blindly applies
the mirrored value. Mirroring "legacy" is kept (that branch DOES send a real
``initialize`` handshake, needed so a legacy front's push-forwarding reaches
a like-negotiated backend -- ``test_gateway_mirrors_front_era_to_backend``);
a modern pin now runs real ``mode="auto"`` negotiation instead, landing on
the identical practical outcome for a genuinely modern backend (same test,
first case) while honestly negotiating legacy for one that is not.
``test_namespace_direct_connect_negotiates_for_real_not_mirrored`` below pins
this deterministically with a hand-rolled legacy-only stdio stub (no
external venv dependency): pre-fix red (the exact live ``MCPError``), post-fix
green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psutil
import pytest
from mcp.shared.exceptions import MCPError

from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import build_gateway, list_tool_definitions, namespace_proxies
from clio_agent.tools.mcp_config import MCPServerSpec

# A real stdio MCP server (fastmcp, real subprocess -- not a mock) whose
# ``tools/list`` handler ALWAYS raises a plain, non-NotFoundError exception.
# This is the deterministic stand-in for the live race (a legacy backend
# rejecting a request mid-handshake): what matters for this defect is only
# that the single child provider's list_tools()/get_tool() genuinely fails,
# not *why* it failed.
FLAKY_STUB = """
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

mcp = FastMCP("flaky")

@mcp.tool
def list_runs() -> dict:
    return {"runs": []}

class BreakListTools(Middleware):
    async def on_list_tools(self, context, call_next):
        raise RuntimeError(
            "simulated backend protocol race: received request before "
            "initialization was complete"
        )

mcp.add_middleware(BreakListTools())
mcp.run()
"""

# A real, working stdio MCP server -- used by test_missing_tool_still_reports_not_found
# to prove the fix leaves a genuinely absent tool's error path untouched.
OK_STUB = """
from fastmcp import FastMCP

mcp = FastMCP("ok")

@mcp.tool
def real_tool() -> str:
    return "ok"

mcp.run()
"""

# A hand-rolled, MINIMAL raw line-delimited JSON-RPC stdio server -- no
# fastmcp/mcp SDK import at all -- that mirrors EXACTLY the installed SDK's
# (mcp==1.29.0, spotter-ai's own venv) server-side session-lifecycle gate
# (mcp/server/session.py::_received_request): every request except
# `initialize`/`notifications/initialized`/`ping` is rejected with
# "Received request before initialization was complete" until a REAL
# `initialize` request has actually been received. It never implements
# `server/discover` (the modern probe), matching a genuinely legacy-only
# backend -- clio-agent's own venv has no mcp==1.x to depend on, so this is
# self-contained rather than requiring an external installed package.
LEGACY_ONLY_STUB = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

initialized = False
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        initialized = True
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "legacy-only-stub", "version": "0.0.1"},
        }})
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    elif not initialized:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {
            "code": -32602,
            "message": "Received request before initialization was complete",
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
            {"name": "ping_tool", "description": "", "inputSchema": {"type": "object", "properties": {}}}
        ]}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": "pong"}]}})
    else:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}})
"""


def _reap(needle: str) -> None:
    """Kill any lingering stdio backend the proxy spawned (defensive cleanup)."""
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if needle in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            continue


def test_backend_protocol_error_surfaces_honestly_not_as_unknown_tool(
    tmp_path: Path,
) -> None:
    """A real backend failure must reach the caller as itself, never as a
    fabricated 'Unknown tool' -- the live spotter_list_runs / workload_run_campaign
    defect, reproduced through clio's REAL namespace-direct dispatch path
    (AsyncMCPToolExecutor._route / _connect_namespace via the sync wrapper).
    """

    script = tmp_path / "flaky_mcp_stub.py"
    script.write_text(FLAKY_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="flaky",
        transport="stdio",
        command=sys.executable,
        args=(str(script),),
    )
    gateway = build_gateway({"flaky": spec})  # real _proxy_for_spec, real proxy
    preloaded = dict(list_tool_definitions(gateway))
    # The boot-time listing pass ALSO hits the flaky backend and degrades
    # this namespace to no tools (a separate, already-typed degrade -- see
    # gateway.py's tool_listing_failed warning); fabricate the catalog entry
    # so routing reaches the namespace-direct proxy exactly like a live
    # session whose catalog was built before this call started failing.
    preloaded["flaky_list_runs"] = preloaded["fs_read_file"]
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=preloaded,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        with pytest.raises(MCPError) as excinfo:
            executor.call_tool("flaky_list_runs", {})
        # The defect under test: this must NOT be a "the tool doesn't exist"
        # error -- the tool genuinely exists and the model must not be told
        # otherwise for a transient/real backend failure.
        assert "Unknown tool" not in str(excinfo.value)
    finally:
        executor.close()
        _reap("flaky_mcp_stub.py")


def test_missing_tool_still_reports_not_found(tmp_path: Path) -> None:
    """The fix (provider_error_strategy='raise') must not touch the genuine
    not-found path: calling a bare name the backend never registered still
    raises the honest 'Unknown tool' -- aggregate.py special-cases
    NotFoundError regardless of provider_error_strategy."""

    script = tmp_path / "ok_mcp_stub.py"
    script.write_text(OK_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="ok",
        transport="stdio",
        command=sys.executable,
        args=(str(script),),
    )
    gateway = build_gateway({"ok": spec})
    preloaded = dict(list_tool_definitions(gateway))
    assert "ok_real_tool" in preloaded
    preloaded["ok_this_tool_does_not_exist"] = preloaded["ok_real_tool"]
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=preloaded,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            executor.call_tool("ok_this_tool_does_not_exist", {})
        assert "Unknown tool" in str(excinfo.value)
    finally:
        executor.close()
        _reap("ok_mcp_stub.py")


def test_namespace_direct_connect_negotiates_for_real_not_mirrored(
    tmp_path: Path,
) -> None:
    """Root cause #2: a namespace-direct connect must NEGOTIATE with the real
    backend, never fabricate/assume its era by mirroring the (always-modern,
    in-process) front. Pre-fix, gateway.py::_proxy_for_spec pinned the
    backend Client's mode to the front's exact modern version string;
    Client._negotiate()'s pinned branch never sends `initialize` at all
    (session.adopt(_synthesize_discover(...)) fabricates the result locally),
    so this genuinely legacy-only stub -- which requires a real `initialize`
    before anything else, exactly like the installed SDK the live spotter-ai/
    phenotype backends actually run -- rejected the tool call deterministically
    with the exact live error shape. Post-fix, the backend connect runs real
    "auto" negotiation instead and the call succeeds.
    """

    script = tmp_path / "legacy_only_stub.py"
    script.write_text(LEGACY_ONLY_STUB, encoding="utf-8")
    spec = MCPServerSpec(
        name="legacyonly",
        transport="stdio",
        command=sys.executable,
        args=(str(script),),
    )
    gateway = build_gateway({"legacyonly": spec})  # real _proxy_for_spec -> real mirror decision
    preloaded = dict(list_tool_definitions(gateway))
    assert "legacyonly_ping_tool" in preloaded
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=preloaded,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        result = executor.call_tool("legacyonly_ping_tool", {})
        assert "pong" in result
    finally:
        executor.close()
        _reap("legacy_only_stub.py")
