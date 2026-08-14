"""The spawn-diet executor hooks (#934) against REAL executors + stub servers.

The S3 lesson (fake-green while the live reap failed) applies verbatim: the
connect hook must be proven on the real _route path, not on fakes.
"""

from __future__ import annotations

import sys

import pytest

from clio_agent.tools import spawn_diet
from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import build_gateway, list_tool_definitions, namespace_proxies
from clio_agent.tools.mcp_config import MCPServerSpec

STUB = """
import json, os, sys
from fastmcp import FastMCP

server = FastMCP("stub")

@server.tool
def echo(text: str) -> str:
    return text

with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(f"start {os.getpid()}\\n")
server.run()
"""


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn_diet, "_cache_path", lambda: tmp_path / "diet.json")
    monkeypatch.setattr(spawn_diet, "_scans_scheduled", set())
    monkeypatch.setattr(spawn_diet, "_pending_learns", {})
    monkeypatch.setattr(spawn_diet, "_applied_plans", {})
    yield


def test_connect_hook_fires_per_namespace_on_real_route(tmp_path, monkeypatch) -> None:
    """EVERY namespace's first routed call must fire namespace_connected —
    the learn moment. One namespace connecting must not satisfy the others."""

    connected: list[str] = []
    monkeypatch.setattr(spawn_diet, "namespace_connected", connected.append)

    specs: dict[str, MCPServerSpec] = {}
    for ns in ("alpha", "beta"):
        script = tmp_path / f"stub_{ns}.py"
        script.write_text(STUB, encoding="utf-8")
        specs[ns] = MCPServerSpec(
            name=ns,
            transport="stdio",
            command=sys.executable,
            args=(str(script), str(tmp_path / f"{ns}-marker.txt")),
        )
    gateway = build_gateway(specs)
    definitions = list_tool_definitions(gateway)
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=definitions,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        assert executor.call_tool("alpha_echo", {"text": "a"})
        assert connected == ["alpha"]
        # Second call on a connected namespace: no re-fire (ctx cached).
        assert executor.call_tool("alpha_echo", {"text": "a2"})
        assert connected == ["alpha"]
        assert executor.call_tool("beta_echo", {"text": "b"})
        assert connected == ["alpha", "beta"]
    finally:
        executor.close()


def test_connect_failure_fires_spawn_failed(tmp_path, monkeypatch) -> None:
    """A namespace whose stdio spawn cannot connect reports spawn_failed —
    the drop-plan feedback loop's trigger."""

    failed: list[str] = []
    monkeypatch.setattr(spawn_diet, "spawn_failed", failed.append)
    connected: list[str] = []
    monkeypatch.setattr(spawn_diet, "namespace_connected", connected.append)

    script = tmp_path / "stub_ok.py"
    script.write_text(STUB, encoding="utf-8")
    specs = {
        "ok": MCPServerSpec(
            name="ok",
            transport="stdio",
            command=sys.executable,
            args=(str(script), str(tmp_path / "ok-marker.txt")),
        ),
        "broken": MCPServerSpec(
            name="broken",
            transport="stdio",
            command=sys.executable,
            args=("-c", "import sys; sys.exit(3)"),
        ),
    }
    gateway = build_gateway(specs)
    # The broken namespace can't be listed; preload only the ok tool plus a
    # fabricated broken_echo definition so routing reaches the broken proxy.
    ok_defs = {}
    for name, definition in list_tool_definitions(gateway).items():
        ok_defs[name] = definition
    ok_defs["broken_echo"] = ok_defs["ok_echo"]
    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=ok_defs,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        from mcp.shared.exceptions import MCPError

        # tools/gateway.py::_proxy_for_spec now builds its FastMCPProxy with
        # provider_error_strategy="raise" (live namespace-dispatch fix): a
        # genuine backend connect failure ("Connection closed" -- the process
        # exited immediately) must reach the caller as the real MCPError, not
        # get swallowed by fastmcp's AggregateProvider default ("warn") into
        # a fabricated NotFoundError -> ToolError("Unknown tool: 'echo'") that
        # is indistinguishable from the model hallucinating a tool name.
        with pytest.raises(MCPError):
            executor.call_tool("broken_echo", {"text": "x"})
        assert failed == ["broken"]
        assert connected == []
    finally:
        executor.close()
