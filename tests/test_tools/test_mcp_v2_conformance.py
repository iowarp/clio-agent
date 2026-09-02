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
from fastmcp.exceptions import MCPError
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

from clio_agent import conf
from clio_agent.errors import (
    MCP_PROTOCOL_DOWNGRADED_TO_LEGACY,
    MCPMissingRequiredClientCapabilityError,
)
from clio_agent.tools.gateway import build_gateway, namespace_proxies, namespace_specs
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_connection_era import latest_mcp_connection_era, resolved_connect_mode
from clio_agent.tools.mcp_errors import typed_mcp_protocol_error
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_runtime import make_mcp_client
from clio_agent.tools.mcp_task_records import InMemoryTaskRecordStore, set_task_record_store

from .mcp_exerciser import (
    EXERCISER_NAMESPACE,
    EXERCISER_PATH,
    MODERN_PROTOCOL_VERSION,
    TASKS_EXTENSION_ID,
    build_exerciser_server,
)
from .mcp_v1_fixture import V1_FIXTURE_PATH, V1_PROTOCOL_VERSION, V1_TOOL_NAME

# fastmcp splices the ui extension onto EVERY modern server; it must never be
# read as task capability. Pinned as the measured strip-result in the proxy test.
_UI_ONLY_EXTENSIONS = {"io.modelcontextprotocol/ui": {}}


class _NoExtensionClient(Client):
    """The suppressed-declaration control: fastmcp's ProxyClient failure mode
    in miniature (S1/S2 import this too)."""

    _auto_internal_extensions = False


@pytest.fixture(autouse=True)
def _clean_connect_mode_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every test starts from the real default (``auto``) -- asserted, not
    assumed: a config-FILE pin (which outranks env) fails here, loudly, instead
    of mis-verdicting an era test."""

    monkeypatch.delenv("CLIO_MCP_CONNECT_MODE", raising=False)
    conf.reload()
    assert resolved_connect_mode() == "auto", (
        "ambient tools.mcp.connect_mode pin detected (config file layer?) - "
        "era tests need the real default"
    )
    yield


@pytest.fixture(autouse=True)
def _isolated_store() -> Any:
    """Keep task records out of the process-wide registry."""

    set_task_record_store(InMemoryTaskRecordStore())
    yield
    set_task_record_store(None)


def _reap(needle: str) -> None:
    """Kill lingering stdio backends a proxy kept alive (keep_alive=True).

    Scoped to DESCENDANTS of this test process only (review finding: a
    machine-wide ``process_iter`` substring kill murders unrelated shells and
    editors whose command lines merely mention the path), matching the spawned
    script argv element.
    """

    for proc in psutil.Process().children(recursive=True):
        try:
            if any(needle in arg for arg in proc.cmdline()):
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
        assert client.protocol_version == MODERN_PROTOCOL_VERSION
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
        assert client.server_capabilities is not None
        assert TASKS_EXTENSION_ID in (client.server_capabilities.extensions or {})

    from fastmcp import FastMCP

    plain = FastMCP("plain")

    @plain.tool
    async def echo(payload: str) -> str:
        return payload

    async with Client(plain) as client:
        assert client.server_capabilities is not None
        assert client.server_capabilities.extensions == _UI_ONLY_EXTENSIONS


async def test_proxy_front_strips_the_backend_tasks_declaration() -> None:
    """The defect mechanism, pinned: a ``create_proxy(ProxyClient(backend))``
    front re-advertises only its OWN extensions -- the backend's tasks
    declaration is invisible through the mounted-proxy path, on BOTH keys."""

    proxy = create_proxy(ProxyClient(build_exerciser_server()))
    async with Client(proxy) as front:
        assert front.server_capabilities is not None
        # The measured strip-result: only the front's own ui splice survives;
        # the backend's tasks declaration is gone.
        assert front.server_capabilities.extensions == _UI_ONLY_EXTENSIONS
        tools = await front.list_tools()
        assert tools, "proxy front listed no tools"
        assert all(getattr(tool, "execution", None) is None for tool in tools)


async def test_unopted_client_receives_the_self_describing_refusal() -> None:
    """A client that did not declare tasks gets -32021 naming EXACTLY what to
    re-dial with (``requiredCapabilities`` -> the tasks extension id), and
    clio's boundary mapper types it."""

    async with _NoExtensionClient(build_exerciser_server()) as client:
        with pytest.raises(MCPError) as excinfo:
            await client.call_tool("task_echo", {"payload": "ping"})
    error = excinfo.value
    assert error.code == -32021
    assert TASKS_EXTENSION_ID in error.data["requiredCapabilities"]["extensions"]
    assert isinstance(typed_mcp_protocol_error(error), MCPMissingRequiredClientCapabilityError)


async def test_plain_staller_progress_reaches_the_client() -> None:
    """The exerciser's provable progress arm: every step's notification lands
    on the call's progress handler (the C1-S2 "progress resets the clock" and
    "visible waiting" legs build on exactly this)."""

    seen: list[tuple[float, float | None]] = []

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append((progress, total))

    async with Client(build_exerciser_server()) as client:
        result = await client.call_tool(
            "plain_staller",
            {"seconds": 0.1, "steps": 3},
            progress_handler=_on_progress,
        )
    assert result.data == "plain-stalled"
    assert [p for p, _ in seen] == [1.0, 2.0, 3.0]


# --------------------------------------------------------------------------
# Layer 2: the declared path (the #1274 defect, executable)
# --------------------------------------------------------------------------


def _declared_exerciser_executor() -> AsyncMCPToolExecutor:
    """The exerciser mounted the way a user-declared server mounts: spec ->
    ``build_gateway`` -> executor namespace route (production wiring:
    ``agent.py`` passes ``namespace_proxies``; ``_active_tool_executor`` stamps
    the spec registry the S1 branch will read). One deliberate divergence:
    production also passes ``preloaded_tools`` (#932) so ``start()`` skips the
    composite listing fan-out -- omitted here, so the exerciser spawns once for
    that listing and once for the namespace route; assertions are unaffected."""

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
    raises=MCPMissingRequiredClientCapabilityError,
    reason="#1274: the declared path suppresses the tasks declaration; C1-S1 (#1281) flips this",
)
async def test_declared_path_serves_task_required_tools() -> None:
    """THE CAMPAIGN TARGET: a user-declared ``task=required`` tool simply works.

    ``raises=`` pins the ONE acceptable failure (the typed -32021 refusal): a
    half-fix that fails any other way -- spawn error, timeout, error-shaped
    success -- fails this test instead of hiding behind the xfail.
    """

    try:
        async with _declared_exerciser_executor() as executor:
            outcome = await executor.call_tool_result(
                f"{EXERCISER_NAMESPACE}_task_echo", {"payload": "ping"}
            )
            assert outcome.model_text == "echo:ping"
    finally:
        _reap("mcp_exerciser.py")


async def test_declared_path_today_refuses_task_required_typed() -> None:
    """The defect pin C1-S1 DELETES: today the same call dies -32021, typed.
    (Its value: proves the refusal is at least typed and terminal, never a
    silent hang -- the #1275 floor.)"""

    try:
        async with _declared_exerciser_executor() as executor:
            with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
                await executor.call_tool_result(
                    f"{EXERCISER_NAMESPACE}_task_echo", {"payload": "ping"}
                )
        # -32021 is a generic missing-capability code (sampling qualifies too):
        # pin that THIS refusal names the tasks extension specifically.
        data = excinfo.value.protocol_data or {}
        assert TASKS_EXTENSION_ID in data["requiredCapabilities"]["extensions"]
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


def _v1_spec(name: str) -> MCPServerSpec:
    return MCPServerSpec(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(V1_FIXTURE_PATH),),
    )


async def test_v1_fixture_negotiates_legacy_and_serves_the_tool() -> None:
    """A genuine 2025-11-25 server: auto-mode negotiation lands legacy and the
    plain tool round-trips on the camelCase wire."""

    from clio_agent.tools.mcp_config import transport_for

    try:
        client = make_mcp_client(transport_for(_v1_spec("v1fix")), server_id="v1fix")
        async with client:
            assert client.protocol_version == V1_PROTOCOL_VERSION
            tools = await client.list_tools()
            assert [tool.name for tool in tools] == [V1_TOOL_NAME]
            result = await client.call_tool(V1_TOOL_NAME, {"payload": "ping"})
            assert result.content[0].text == "legacy:ping"
    finally:
        _reap("mcp_v1_fixture.py")


async def test_v1_fixture_through_the_declared_path() -> None:
    """The same frozen server through spec -> gateway -> front: the call works
    and the era registry records a REAL legacy downgrade under auto mode --
    the byte-identical v1 surface C1-S1 must preserve.

    Own server id ("v1fixgw"): the era registry is process-global, and the
    direct-path test above seeds a "v1fix" record first in file order -- a
    shared id would satisfy the assertion without proving THIS path recorded.
    """

    assert latest_mcp_connection_era("v1fixgw") is None, "stale v1fixgw era record"
    gw = build_gateway({"v1fixgw": _v1_spec("v1fixgw")})
    try:
        async with Client(gw) as front:
            result = await front.call_tool(f"v1fixgw_{V1_TOOL_NAME}", {"payload": "ping"})
            assert result.content[0].text == "legacy:ping"

        era = latest_mcp_connection_era("v1fixgw")
        assert era is not None
        assert era.era == "legacy"
        assert era.protocol_version == V1_PROTOCOL_VERSION
        assert era.degrade_reason == MCP_PROTOCOL_DOWNGRADED_TO_LEGACY
    finally:
        _reap("mcp_v1_fixture.py")
