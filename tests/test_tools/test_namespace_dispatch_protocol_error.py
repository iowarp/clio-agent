"""Regression coverage for a live MCP tool-dispatch defect: a real backend
connect/protocol failure was silently reported to the model as "Unknown
tool" -- indistinguishable from a hallucinated tool name.

Live symptom (fully reproduced): calling ``spotter_list_runs`` /
``workload_run_campaign`` against pack-mounted MCP servers (spotter-ai,
phenotype -- legacy fastmcp 3.4.7 stdio backends invoked by a direct exe
path) failed with ``fastmcp.exceptions.ToolError: Unknown tool: 'list_runs'``
even though the backend genuinely has that tool (proven live via a standalone
probe) and clio's own tool catalog correctly lists the prefixed name.

Root cause, traced through the REAL dispatch path
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

Live evidence: fastmcp's own ``_mirror_front_era_mode()`` pins the backend
connect to the front's (always-modern, in-process) negotiated era; a
legacy-only backend occasionally answers that with "Received request before
initialization was complete" during era renegotiation -- a real, diagnosable
connectivity event, not evidence the tool is absent. This module reproduces
the swallow DETERMINISTICALLY (no timing dependency) by making a real stdio
backend's ``list_tools`` handler always raise, which is the same shape of
failure fastmcp's aggregate layer mishandles regardless of what triggered it.

Fix: ``_proxy_for_spec`` now builds its ``FastMCPProxy`` with
``provider_error_strategy="raise"`` so a real single-provider backend failure
propagates as itself instead of being downgraded to a fabricated "not found".
This changes nothing for an ACTUALLY missing tool: ``aggregate.py`` special-
cases ``NotFoundError`` to keep degrading quietly regardless of this setting
(covered by ``test_missing_tool_still_reports_not_found`` below).
"""

from __future__ import annotations

import json
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

# Fails its first two list_tools() requests with an MCPError-shaped raise
# (simulating the #1186-class connect-time race), then succeeds -- the live
# behavior: EVERY tool call spawns a brand new backend process
# (tools/gateway.py::_proxy_for_spec's per-request client_factory), so a
# retry cannot reuse in-memory state; the attempt count is persisted to a
# file the fresh subprocess reads on each spawn.
RECOVERS_STUB = """
import sys
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

counter_path = Path(sys.argv[1])

mcp = FastMCP("recovers")

@mcp.tool
def list_runs() -> dict:
    return {"runs": []}

class FailUntilWarm(Middleware):
    async def on_list_tools(self, context, call_next):
        attempt = int(counter_path.read_text()) if counter_path.exists() else 0
        counter_path.write_text(str(attempt + 1))
        if attempt < 2:
            raise RuntimeError(
                f"simulated backend protocol race (attempt {attempt})"
            )
        return await call_next(context)

mcp.add_middleware(FailUntilWarm())
mcp.run()
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


def test_first_call_race_recovers_via_bounded_retry(tmp_path: Path) -> None:
    """Live evidence (2026-08-14 restart on the fix branch): the SAME
    #1186-class race still hit the real spotter/phenotype backends after the
    provider_error_strategy fix -- honestly, as MCPError, but honesty alone
    left ``spotter_wait_for_new_runs`` / ``workload_run_campaign`` still
    failing 3/3 live attempts. AsyncMCPToolExecutor.call_tool_result now
    retries a namespace's FIRST request across a bounded number of fresh
    backend spawns when it fails with a raw MCPError (a session-layer
    rejection -- the request never reached the tool, so no side effect can be
    duplicated). This proves the retry actually RECOVERS the call once the
    backend stops racing, not merely that the failure is reported honestly.
    """

    script = tmp_path / "recovers_mcp_stub.py"
    script.write_text(RECOVERS_STUB, encoding="utf-8")
    counter_path = tmp_path / "attempts.txt"
    spec = MCPServerSpec(
        name="recovers",
        transport="stdio",
        command=sys.executable,
        args=(str(script), str(counter_path)),
    )
    gateway = build_gateway({"recovers": spec})
    # The boot-time listing pass (list_tool_definitions) does NOT retry -- it
    # would burn one scripted failure and degrade this namespace to no tools,
    # same as the live serve's own tool_listing_failed degrade. Fabricate the
    # catalog entry (as the other tests in this module do) so routing reaches
    # the namespace-direct proxy with the counter untouched, then let the call
    # itself see the full fail-fail-succeed sequence deterministically.
    preloaded = dict(list_tool_definitions(gateway))
    preloaded["recovers_list_runs"] = preloaded["fs_read_file"]
    counter_path.write_text("0")
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=preloaded,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        result = executor.call_tool("recovers_list_runs", {})
        assert json.loads(result) == {"runs": []}
        # At least 3 real backend spawns (2 scripted failures + the recovering
        # call) proves this succeeded THROUGH retries, not on the first try
        # (fastmcp's own client may issue more than one list_tools() round
        # trip per logical attempt -- this asserts the floor our retry
        # contract guarantees, not fastmcp's internal call count).
        assert int(counter_path.read_text()) >= 3
    finally:
        executor.close()
        _reap("recovers_mcp_stub.py")
