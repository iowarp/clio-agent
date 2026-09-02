"""C1-S0: the dual-era conformance suite over the v2 exerciser (#1280, #1274).

Three layers, each load-bearing for the campaign:

1. PROBE PINS -- the 2026-09-01 probe verdict as executable tests: where the
   task-capability key rides per era (modern = ``server_capabilities.extensions``,
   legacy = per-tool ``execution.taskSupport``), and that the proxy front
   STRIPS the backend's declaration. C1-S1's routing reads exactly these keys;
   if an upstream bump moves them, these pins fail first and name the spot.
2. THE DEFECT, EXECUTABLE -- a ``task=required`` server mounted the way a user
   declares one (``MCPServerSpec`` -> ``build_gateway`` -> executor namespace
   route) refuses every call with the typed -32021 today. The desired behavior
   is the strict-xfail twin: C1-S1 removes the marker and deletes the
   defect pin.
3. THE FROZEN V1 ERA -- the hand-rolled 2025-11-25 fixture proves genuine
   legacy servers negotiate, list, and call through the same declared path
   (the byte-identical surface C1-S1 must not move).
"""

from __future__ import annotations

import sys
from typing import Any

import psutil
import pytest
from fastmcp import Client
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

from clio_agent import conf
from clio_agent.errors import (
    MCP_PROTOCOL_DOWNGRADED_TO_LEGACY,
    MCPMissingRequiredClientCapabilityError,
)
from clio_agent.tools.gateway import build_gateway, namespace_proxies, namespace_specs
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_connection_era import latest_mcp_connection_era
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_runtime import make_mcp_client
from clio_agent.tools.mcp_task_records import InMemoryTaskRecordStore, set_task_record_store

from .mcp_exerciser import (
    EXERCISER_NAMESPACE,
    EXERCISER_PATH,
    TASKS_EXTENSION_ID,
    build_exerciser_server,
)
from .mcp_v1_fixture import V1_FIXTURE_PATH, V1_PROTOCOL_VERSION, V1_TOOL_NAME


@pytest.fixture(autouse=True)
def _clean_connect_mode_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every test starts from the real default (``auto``), never an ambient pin."""

    monkeypatch.delenv("CLIO_MCP_CONNECT_MODE", raising=False)
    conf.reload()
    yield


@pytest.fixture(autouse=True)
def _isolated_store() -> Any:
    """Keep task records out of the process-wide registry."""

    set_task_record_store(InMemoryTaskRecordStore())
    yield
    set_task_record_store(None)


def _reap(needle: str) -> None:
    """Kill any lingering stdio backend a proxy kept alive (keep_alive=True)."""

    for proc in psutil.process_iter(["cmdline"]):
        try:
            if needle in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            continue


# --------------------------------------------------------------------------
# Layer 1: probe pins (the 2026-09-01 verdict, executable)
# --------------------------------------------------------------------------


async def test_modern_listing_omits_per_tool_execution() -> None:
    """2026-07-28 wire: ``Tool.execution`` is GONE from tools/list -- per-tool
    ``taskSupport`` cannot be the modern negotiation key."""

    async with Client(build_exerciser_server()) as client:
        assert client.protocol_version == "2026-07-28"
        tools = await client.list_tools()
        assert tools, "exerciser listed no tools"
        assert all(getattr(tool, "execution", None) is None for tool in tools)


async def test_legacy_listing_carries_the_three_task_support_arms() -> None:
    """2025-11-25 wire: per-tool ``execution.taskSupport`` IS the key -- all
    three SEP-1686 arms visible (the forbidden arm only because the exerciser
    sets ``Tool.execution`` explicitly)."""

    async with Client(build_exerciser_server(), mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        support = {
            tool.name: getattr(getattr(tool, "execution", None), "task_support", None)
            for tool in await client.list_tools()
        }
    assert support["task_echo"] == "required"
    assert support["task_optional_echo"] == "optional"
    assert support["forbidden_echo"] == "forbidden"
    assert support["plain_echo"] is None


async def test_modern_capabilities_declare_the_tasks_extension() -> None:
    """Modern era: the SERVER-DECLARED extensions carry the tasks id -- and a
    task-free server's do not (the ui splice fastmcp adds everywhere must
    never be read as task capability)."""

    async with Client(build_exerciser_server()) as client:
        extensions = (
            (client.server_capabilities.extensions or {}) if (client.server_capabilities) else {}
        )
        assert TASKS_EXTENSION_ID in extensions

    from fastmcp import FastMCP

    plain = FastMCP("plain")

    @plain.tool
    async def echo(payload: str) -> str:
        return payload

    async with Client(plain) as client:
        extensions = (
            (client.server_capabilities.extensions or {}) if (client.server_capabilities) else {}
        )
        assert TASKS_EXTENSION_ID not in extensions


async def test_proxy_front_strips_the_backend_tasks_declaration() -> None:
    """The defect mechanism, pinned: a ``create_proxy(ProxyClient(backend))``
    front re-advertises only its OWN extensions -- the backend's tasks
    declaration is invisible through the mounted-proxy path, on BOTH keys."""

    proxy = create_proxy(ProxyClient(build_exerciser_server()))
    async with Client(proxy) as front:
        extensions = (
            (front.server_capabilities.extensions or {}) if (front.server_capabilities) else {}
        )
        assert TASKS_EXTENSION_ID not in extensions
        tools = await front.list_tools()
        assert all(getattr(tool, "execution", None) is None for tool in tools)


async def test_unopted_client_receives_the_self_describing_refusal() -> None:
    """A client that did not declare tasks gets -32021 naming EXACTLY what to
    re-dial with (``requiredCapabilities`` -> the tasks extension id)."""

    class _NoExtensionClient(Client):
        _auto_internal_extensions = False

    async with _NoExtensionClient(build_exerciser_server()) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("task_echo", {"payload": "ping"})
    rendered = repr(excinfo.value)
    assert "-32021" in rendered or "tasks" in rendered


# --------------------------------------------------------------------------
# Layer 2: the declared path (the #1274 defect, executable)
# --------------------------------------------------------------------------


def _declared_exerciser_executor() -> AsyncMCPToolExecutor:
    """The exerciser mounted EXACTLY the way a user-declared server mounts:
    spec -> ``build_gateway`` -> executor namespace route (production wiring:
    ``agent.py`` passes ``namespace_proxies``; ``_active_tool_executor`` stamps
    the spec registry the S1 branch will read)."""

    spec = MCPServerSpec(
        name=EXERCISER_NAMESPACE,
        transport="stdio",
        command=sys.executable,
        args=(str(EXERCISER_PATH),),
    )
    gw = build_gateway({EXERCISER_NAMESPACE: spec})
    executor = AsyncMCPToolExecutor(gw, namespace_servers=namespace_proxies(gw))
    executor._clio_namespace_specs = namespace_specs(gw)  # noqa: SLF001 - mirrors production stamping
    return executor


@pytest.mark.xfail(
    strict=True,
    reason="#1274: the declared path suppresses the tasks declaration; C1-S1 (#1281) flips this",
)
async def test_declared_path_serves_task_required_tools() -> None:
    """THE CAMPAIGN TARGET: a user-declared ``task=required`` tool simply works."""

    try:
        async with _declared_exerciser_executor() as executor:
            outcome = await executor.call_tool_result(
                f"{EXERCISER_NAMESPACE}_task_echo", {"payload": "ping"}
            )
            assert "echo:ping" in outcome.model_text
    finally:
        _reap("mcp_exerciser.py")


async def test_declared_path_today_refuses_task_required_typed() -> None:
    """The defect pin C1-S1 DELETES: today the same call dies -32021, typed.
    (Its value: proves the refusal is at least typed and terminal, never a
    silent hang -- the #1275 floor.)"""

    try:
        async with _declared_exerciser_executor() as executor:
            with pytest.raises(MCPMissingRequiredClientCapabilityError):
                await executor.call_tool_result(
                    f"{EXERCISER_NAMESPACE}_task_echo", {"payload": "ping"}
                )
    finally:
        _reap("mcp_exerciser.py")


async def test_declared_path_plain_tools_work_on_a_task_capable_server() -> None:
    """Plain tools on a task-capable server work TODAY through the declared
    path -- C1-S1 must not move this."""

    try:
        async with _declared_exerciser_executor() as executor:
            outcome = await executor.call_tool_result(
                f"{EXERCISER_NAMESPACE}_plain_echo", {"payload": "ping"}
            )
            assert "plain:ping" in outcome.model_text
    finally:
        _reap("mcp_exerciser.py")


# --------------------------------------------------------------------------
# Layer 3: the frozen v1 era
# --------------------------------------------------------------------------


def _v1_spec() -> MCPServerSpec:
    return MCPServerSpec(
        name="v1fix",
        transport="stdio",
        command=sys.executable,
        args=(str(V1_FIXTURE_PATH),),
    )


async def test_v1_fixture_negotiates_legacy_and_serves_the_tool() -> None:
    """A genuine 2025-11-25 server: auto-mode negotiation lands legacy and the
    plain tool round-trips on the camelCase wire."""

    from clio_agent.tools.mcp_config import transport_for

    try:
        client = make_mcp_client(transport_for(_v1_spec()), server_id="v1fix")
        async with client:
            assert client.protocol_version == V1_PROTOCOL_VERSION
            tools = await client.list_tools()
            assert [tool.name for tool in tools] == [V1_TOOL_NAME]
            result = await client.call_tool(V1_TOOL_NAME, {"payload": "ping"})
            assert "legacy:ping" in str(result.content)
    finally:
        _reap("mcp_v1_fixture.py")


async def test_v1_fixture_through_the_declared_path() -> None:
    """The same frozen server through spec -> gateway -> front: the call works
    and the era registry records a REAL legacy downgrade under auto mode --
    the byte-identical v1 surface C1-S1 must preserve."""

    gw = build_gateway({"v1fix": _v1_spec()})
    try:
        async with Client(gw) as front:
            result = await front.call_tool(f"v1fix_{V1_TOOL_NAME}", {"payload": "ping"})
            assert "legacy:ping" in str(result.content)

        era = latest_mcp_connection_era("v1fix")
        assert era is not None
        assert era.era == "legacy"
        assert era.protocol_version == V1_PROTOCOL_VERSION
        assert era.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    finally:
        _reap("mcp_v1_fixture.py")
