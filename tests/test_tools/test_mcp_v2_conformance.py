"""C1-S0: the dual-era conformance suite over the v2 exerciser (#1280, #1274).

Three layers, each load-bearing for the campaign:

1. PROBE PINS -- the 2026-09-01 probe verdict as executable tests: where the
   task-capability key rides per era (modern = ``server_capabilities.extensions``,
   legacy = per-tool ``execution.taskSupport``), and that the proxy front
   STRIPS the backend's declaration. C1-S1's routing reads exactly these keys;
   if an upstream bump moves them, these pins fail first and name the spot.
2. THE DEFECT, FIXED -- a ``task=required`` server mounted the way a user
   declares one (``MCPServerSpec`` -> ``build_gateway`` -> executor namespace
   route) used to refuse every call with the typed -32021 (#1274). C1-S1
   (#1281) fixes it with capability-keyed routing: once a namespace's task
   capability is negotiated True, ``_connect_namespace`` uses a DIRECT
   task-declaring client instead of the proxy path that suppresses the
   declaration.
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
    MCP_TASK_CAPABILITY_UNKNOWN,
    MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED,
    MCP_TASK_DIRECT_FACTORY_MISSING,
    MCP_TASKS_DECLARATION_SUPPRESSED,
    MCP_TASKS_DIRECT_ROUTE_SELECTED,
    MCPMissingRequiredClientCapabilityError,
)
from clio_agent.tools.gateway import (
    _list_declared_tools,
    build_gateway,
    namespace_proxies,
    namespace_specs,
)
from clio_agent.tools.mcp_config import MCPServerSpec, transport_for
from clio_agent.tools.mcp_connection_era import (
    latest_mcp_connection_era,
    latest_task_capability,
    resolved_connect_mode,
)
from clio_agent.tools.mcp_errors import typed_mcp_protocol_error
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_runtime import make_mcp_client
from clio_agent.tools.mcp_task_extension import tasks_declaration
from clio_agent.tools.mcp_task_records import InMemoryTaskRecordStore, set_task_record_store
from clio_agent.tools.mcp_task_routing import (
    record_definitive_capability,
    record_namespace_route_decision,
    recorded_route_heals,
    recorded_task_route_decisions,
    resolve_and_build_direct_client,
    resolve_namespace_route,
)

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


def _exerciser_spec(namespace: str) -> MCPServerSpec:
    return MCPServerSpec(
        name=namespace,
        transport="stdio",
        command=sys.executable,
        args=(str(EXERCISER_PATH),),
    )


def _declared_exerciser_executor(
    namespace: str = EXERCISER_NAMESPACE, *, preloaded_tools: dict[str, Any] | None = None
) -> AsyncMCPToolExecutor:
    """The exerciser mounted the way a user-declared server mounts: spec ->
    ``build_gateway`` -> executor namespace route (production wiring:
    ``agent.py`` passes ``namespace_proxies``; ``_active_tool_executor`` stamps
    the spec registry; ``AsyncMCPToolExecutor.__init__`` derives the
    direct-factory registry straight off the gateway -- #1281 F5).

    PRODUCTION ALWAYS PASSES ``preloaded_tools`` (#932; agent.py:280/:622,
    ``gact/relay_wiring.py``) -- adversarial review F1 found the ORIGINAL
    version of this helper omitted it, so ``start()``'s composite listing
    fan-out (not any production code path) was what happened to make
    capability discovery land before the target call. Callers now pass
    ``preloaded_tools`` explicitly, production-shaped; ``None`` is no longer
    the default for the acceptance tests (kept only for the probe-pin tests
    below that intentionally exercise the composite listing).
    """

    spec = _exerciser_spec(namespace)
    gw = build_gateway({namespace: spec})
    executor = AsyncMCPToolExecutor(
        gw, namespace_servers=namespace_proxies(gw), preloaded_tools=preloaded_tools
    )
    executor._clio_namespace_specs = namespace_specs(gw)  # noqa: SLF001 - mirrors production stamping
    return executor


def _preloaded_tools_from_listing(namespace: str, listed: list[Any]) -> dict[str, Any]:
    """Build the ``preloaded_tools`` dict a real discovery pass would hand
    the executor (mirrors ``gateway._list_declared_tools``'s callers)."""

    return {
        f"{namespace}_{tool.name}": tool.model_copy(update={"name": f"{namespace}_{tool.name}"})
        for tool in listed
    }


async def test_declared_path_serves_task_required_tools_readiness_ordered() -> None:
    """THE CAMPAIGN TARGET, PRODUCTION-WIRED (F1(a)): a user-declared
    ``task=required`` tool works on the FIRST call when the definitive
    discovery step (``gateway._list_declared_tools``, what ``gact/
    mcp_readiness.py``'s ``ensure_namespace``/``_list_one_namespace`` always
    runs before a session's first turn) has landed BEFORE the call --
    the readiness-ordered production shape, ``preloaded_tools`` populated
    from that SAME discovery so ``start()`` never does its own composite
    listing fan-out (adversarial review F1: the ORIGINAL version of this
    test passed for a reason production doesn't have)."""

    namespace = "v2exready"
    try:
        spec = _exerciser_spec(namespace)
        listed = _list_declared_tools(spec)
        capability = latest_task_capability(namespace)
        assert capability is not None
        assert capability.task_capable is True
        assert capability.source == "capabilities_extensions"

        gw = build_gateway({namespace: spec})
        executor = AsyncMCPToolExecutor(
            gw,
            namespace_servers=namespace_proxies(gw),
            preloaded_tools=_preloaded_tools_from_listing(namespace, listed),
        )
        executor._clio_namespace_specs = namespace_specs(gw)  # noqa: SLF001

        async with executor:
            outcome = await executor.call_tool_result(f"{namespace}_task_echo", {"payload": "ping"})
            assert outcome.model_text == "echo:ping"
        decisions = [d for ns, d in recorded_task_route_decisions() if ns == namespace]
        assert decisions, "no route decision recorded"
        assert decisions[-1].use_direct is True
        assert decisions[-1].reason == MCP_TASKS_DIRECT_ROUTE_SELECTED
    finally:
        _reap("mcp_exerciser.py")


async def test_declared_path_serves_task_required_tools_cold_race_then_heals() -> None:
    """THE CAMPAIGN TARGET, PRODUCTION-WIRED (F1(b)): with ``preloaded_tools``
    passed (production-shaped) but the capability registry COLD (no
    discovery has landed for this namespace yet -- the readiness race:
    ``mcp_readiness.py``'s own bounded wait lost, or a caller that skipped
    it), the FIRST call fails TERMINAL-FAST with the typed -32021 refusal
    (#1275: a deterministic protocol refusal, never a silent hang) -- the
    proxy path's OWN backend connect attempt, made during that failed call,
    opportunistically records the SAME server's capability True
    (``mcp_connection_era.instrument_client_era``). The SECOND call then
    HEALS (F2: the stale proxy-cached client is evicted and reconnected
    direct, typed ``MCP_TASK_ROUTE_HEALED``) and succeeds."""

    namespace = "v2excold"
    try:
        executor = _declared_exerciser_executor(
            namespace, preloaded_tools={f"{namespace}_task_echo": None}
        )
        assert latest_task_capability(namespace) is None, "capability must start COLD"

        async with executor:
            with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
                await executor.call_tool_result(f"{namespace}_task_echo", {"payload": "ping"})
            data = excinfo.value.protocol_data or {}
            assert TASKS_EXTENSION_ID in data["requiredCapabilities"]["extensions"]

            capability = latest_task_capability(namespace)
            assert capability is not None
            assert capability.task_capable is True, (
                "the proxy backend's own connect attempt (made to dispatch "
                "the failed call) must opportunistically record capability"
            )
            first_decisions = [d for ns, d in recorded_task_route_decisions() if ns == namespace]
            assert first_decisions[-1].use_direct is False
            assert first_decisions[-1].reason == MCP_TASK_CAPABILITY_UNKNOWN

            outcome = await executor.call_tool_result(f"{namespace}_task_echo", {"payload": "p2"})
            assert outcome.model_text == "echo:p2"
            healed_decisions = [d for ns, d in recorded_task_route_decisions() if ns == namespace]
            assert healed_decisions[-1].use_direct is True
            assert healed_decisions[-1].reason == MCP_TASKS_DIRECT_ROUTE_SELECTED
    finally:
        _reap("mcp_exerciser.py")


async def test_heal_attempt_is_bounded_when_the_direct_factory_never_lands() -> None:
    """#1281 F12 (adversarial review, regression): ``resolve_namespace_route``
    sees only the capability-derived INTENT, not whether a direct factory
    actually exists/constructs for THIS executor. Before the fix, a
    namespace with capability True but NO usable direct factory (a
    construction path that never threaded factories on, or F9's
    construction-failure demotion) evicted + reconnected + reported a heal
    on EVERY reuse, forever, since it never actually lands direct (measured
    by the reviewer: 3 calls -> 3 connects -> 2 false heal events).

    Regression: capability True + an EMPTY factory registry -> across N
    SUBSEQUENT calls, exactly ONE extra ``_connect_namespace`` invocation
    (the single bounded heal attempt) and ZERO ``mcp_task_route_healed``
    events for this namespace -- the heal is attempted at most once, never
    reported as successful when it never actually lands direct.
    """

    namespace = "v2exf12"
    try:
        spec = _exerciser_spec(namespace)
        gw = build_gateway({namespace: spec})
        executor = AsyncMCPToolExecutor(
            gw,
            namespace_servers=namespace_proxies(gw),
            preloaded_tools={f"{namespace}_plain_echo": None},
        )
        executor._clio_namespace_specs = namespace_specs(gw)  # noqa: SLF001
        # Deliberately EMPTY: no direct factory is ever threaded onto this
        # executor for this namespace -- the F12 scenario (a reserved-
        # namespace mount, or F9's permanent construction-failure demotion).
        executor._clio_namespace_direct_factories = {}  # noqa: SLF001

        connect_calls = 0
        original_connect = executor._connect_namespace  # noqa: SLF001

        async def _counting_connect(ns: str, proxy: Any) -> Any:
            nonlocal connect_calls
            connect_calls += 1
            return await original_connect(ns, proxy)

        executor._connect_namespace = _counting_connect  # type: ignore[method-assign]  # noqa: SLF001

        async with executor:
            # Cold connect: capability unknown at first, lands proxy; the
            # exerciser's own opportunistic capture (a genuinely modern,
            # task-capable backend) then lands capability True for free --
            # forced explicitly too, so the test's precondition never
            # depends on that capture's exact timing.
            await executor.call_tool_result(f"{namespace}_plain_echo", {"payload": "a"})
            from clio_agent.tools.mcp_connection_era import record_task_capability

            record_task_capability(namespace, task_capable=True, source="capabilities_extensions")

            connect_calls = 0  # only count the N calls below
            for i in range(3):
                outcome = await executor.call_tool_result(
                    f"{namespace}_plain_echo", {"payload": f"b{i}"}
                )
                assert "plain:" in outcome.model_text

        assert connect_calls == 1, "the heal attempt must be bounded to exactly once"
        heals = [ns for ns in recorded_route_heals() if ns == namespace]
        assert heals == [], "a heal that never lands direct must never be reported healed"
    finally:
        _reap("mcp_exerciser.py")


async def test_declared_path_plain_tools_work_on_a_task_capable_server() -> None:
    """Plain tools on a task-capable server work TODAY through the declared
    path -- C1-S1 must not move this. Whole-namespace routing means a plain
    tool on a task-capable server ALSO rides the direct route once capability
    is known True; the typed reason record says so. Production-wired
    (preloaded_tools + discovery-first, mirroring F1(a))."""

    namespace = "v2explain"
    try:
        spec = _exerciser_spec(namespace)
        listed = _list_declared_tools(spec)
        gw = build_gateway({namespace: spec})
        executor = AsyncMCPToolExecutor(
            gw,
            namespace_servers=namespace_proxies(gw),
            preloaded_tools=_preloaded_tools_from_listing(namespace, listed),
        )
        executor._clio_namespace_specs = namespace_specs(gw)  # noqa: SLF001

        async with executor:
            outcome = await executor.call_tool_result(
                f"{namespace}_plain_echo", {"payload": "ping"}
            )
            assert "plain:ping" in outcome.model_text
        decisions = [d for ns, d in recorded_task_route_decisions() if ns == namespace]
        assert decisions, "no route decision recorded for the exerciser namespace"
        assert decisions[-1].use_direct is True
        assert decisions[-1].reason == MCP_TASKS_DIRECT_ROUTE_SELECTED
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


async def test_v1_fixture_definitive_capability_is_false_and_keeps_the_proxy_route() -> None:
    """The DEFINITIVE read (``gateway._list_declared_tools``, the boot catalog
    pass) on a genuine v1 server sees neither era marker (extensions stripped,
    no per-tool ``execution`` arm on this fixture's one tool) and records a
    real negative -- so the call-time route decision keeps the proxy path
    with NO special reason (``tasks_declaration``'s own
    ``mcp_tasks_declaration_suppressed`` -- unchanged, still tested in
    ``test_mcp_tasks.py`` -- covers why THAT leg never advertises tasks)."""

    namespace = "v1fix-definitive"
    assert latest_task_capability(namespace) is None
    try:
        tools = _list_declared_tools(_v1_spec(namespace))
        assert [tool.name for tool in tools] == [V1_TOOL_NAME]

        capability = latest_task_capability(namespace)
        assert capability is not None
        assert capability.task_capable is False
        assert capability.source == "none"

        decision = resolve_namespace_route(namespace)
        assert decision.use_direct is False
        assert decision.reason is None

        declaration = tasks_declaration(ProxyClient, object())
        assert declaration.reason == MCP_TASKS_DECLARATION_SUPPRESSED
    finally:
        _reap("mcp_v1_fixture.py")


# --------------------------------------------------------------------------
# Layer 4: C1-S1 capability-keyed routing (#1281) -- the discovery reads and
# the route decision, in isolation from the full declared-path plumbing.
# --------------------------------------------------------------------------


async def test_record_definitive_capability_reads_modern_extensions() -> None:
    """In-memory modern exerciser: the definitive read finds the tasks id in
    the server-declared extensions -- ``source="capabilities_extensions"``."""

    async with Client(build_exerciser_server()) as client:
        tools = await client.list_tools()
        capability = record_definitive_capability("unit-modern", client, tools)
    assert capability.task_capable is True
    assert capability.source == "capabilities_extensions"
    assert latest_task_capability("unit-modern") == capability


async def test_declared_path_serves_a_task_when_capability_came_from_tool_execution() -> None:
    """#1281 F8 (adversarial review): drive a REAL call through a direct
    route whose capability was sourced from the LEGACY per-tool marker
    (``tool_execution``), end to end, and pin what actually happens.

    Probed (2026-09-01): a forced legacy-mode connect+list against the
    exerciser correctly sources capability ``tool_execution``; the DECLARED
    PATH's direct-client factory then connects on ITS OWN terms (default
    auto mode) -- which negotiates MODERN for this genuinely modern,
    task-capable server -- and the call SUCCEEDS. fastmcp-tasks is not
    structurally unable to serve a task here: the ``tool_execution`` source
    only proves capability was DISCOVERED via the legacy key, not that the
    ACTUAL connection that serves the call is itself legacy-negotiated (a
    real backend is free to negotiate its own best era on each connect).
    """

    namespace = "v2exlegacycap"
    try:
        # Force a LEGACY-negotiated read so capability sources tool_execution
        # (mirrors what a genuinely 2025-11-25 server would produce, without
        # needing a hand-rolled fixture with real task RPCs to dispatch to).
        async with Client(build_exerciser_server(), mode="legacy") as legacy_client:
            tools = await legacy_client.list_tools()
            capability = record_definitive_capability(namespace, legacy_client, tools)
        assert capability.source == "tool_execution"
        assert capability.task_capable is True

        spec = _exerciser_spec(namespace)
        gw = build_gateway({namespace: spec})
        executor = AsyncMCPToolExecutor(
            gw,
            namespace_servers=namespace_proxies(gw),
            preloaded_tools={f"{namespace}_task_echo": None, f"{namespace}_plain_echo": None},
        )
        executor._clio_namespace_specs = namespace_specs(gw)  # noqa: SLF001

        async with executor:
            outcome = await executor.call_tool_result(f"{namespace}_task_echo", {"payload": "ping"})
            assert outcome.model_text == "echo:ping"
        decisions = [d for ns, d in recorded_task_route_decisions() if ns == namespace]
        assert decisions, "no route decision recorded"
        assert decisions[-1].use_direct is True
        assert decisions[-1].reason == MCP_TASKS_DIRECT_ROUTE_SELECTED
    finally:
        _reap("mcp_exerciser.py")


async def test_record_definitive_capability_reads_legacy_per_tool_marker() -> None:
    """Legacy-mode front over the exerciser: extensions are stripped by the
    version sieve, so the definitive read falls back to the per-tool
    ``execution.task_support`` arm -- ``source="tool_execution"``."""

    async with Client(build_exerciser_server(), mode="legacy") as client:
        tools = await client.list_tools()
        capability = record_definitive_capability("unit-legacy", client, tools)
    assert capability.task_capable is True
    assert capability.source == "tool_execution"


async def test_record_definitive_capability_records_a_genuine_negative() -> None:
    """The frozen v1 fixture's one tool carries neither era marker: the
    definitive read records ``task_capable=False``, ``source="none"``."""

    try:
        client = make_mcp_client(transport_for(_v1_spec("unit-v1-negative")), server_id="unit-v1")
        async with client:
            tools = await client.list_tools()
            capability = record_definitive_capability("unit-v1-negative", client, tools)
        assert capability.task_capable is False
        assert capability.source == "none"
    finally:
        _reap("mcp_v1_fixture.py")


def test_capability_unknown_default_keeps_the_proxy_path() -> None:
    """Before ANY discovery lands for a namespace, the PURE decision keeps
    the proxy path with the typed capability-unknown reason -- the safe
    default (never a guess) until a listing pass or an opportunistic
    real-backend connect records a verdict.

    #1281 F4 (adversarial review): ``resolve_namespace_route`` is READ-ONLY
    (writes nothing, not even the ring) -- the ring only ever records the
    decision ACTUALLY TAKEN, via ``record_namespace_route_decision``, called
    explicitly here to prove that contract.
    """

    namespace = "never-discovered-c1s1-namespace"
    assert latest_task_capability(namespace) is None
    decision = resolve_namespace_route(namespace)
    assert decision.use_direct is False
    assert decision.reason == MCP_TASK_CAPABILITY_UNKNOWN
    assert (namespace, decision) not in recorded_task_route_decisions()
    record_namespace_route_decision(namespace, decision)
    assert (namespace, decision) in recorded_task_route_decisions()


def test_known_task_capable_namespace_with_factory_builds_direct_client() -> None:
    """A namespace with a recorded True verdict AND a mounted direct factory
    actually builds a client (not just an intent) -- ``resolve_and_build_
    direct_client`` returns it plus the decision ACTUALLY taken."""

    from clio_agent.tools.mcp_connection_era import record_task_capability

    namespace = "unit-known-task-capable-with-factory"
    record_task_capability(namespace, task_capable=True, source="capabilities_extensions")
    built = object()
    client, decision = resolve_and_build_direct_client(namespace, {namespace: lambda: built})
    assert client is built
    assert decision.use_direct is True
    assert decision.reason == MCP_TASKS_DIRECT_ROUTE_SELECTED


def test_known_task_capable_namespace_with_no_factory_demotes_typed() -> None:
    """#1281 F4: capability True but NO direct factory threaded onto this
    executor (a reserved-namespace mount, or a construction path predating
    C1-S1 stamping) -- the ring must record the decision ACTUALLY TAKEN
    (proxy), typed ``MCP_TASK_DIRECT_FACTORY_MISSING``, never the
    unreachable "direct" intent."""

    from clio_agent.tools.mcp_connection_era import record_task_capability

    namespace = "unit-known-task-capable-no-factory"
    record_task_capability(namespace, task_capable=True, source="capabilities_extensions")
    client, decision = resolve_and_build_direct_client(namespace, {})
    assert client is None
    assert decision.use_direct is False
    assert decision.reason == MCP_TASK_DIRECT_FACTORY_MISSING


def test_direct_factory_construction_failure_falls_back_to_proxy_typed() -> None:
    """#1281 F9: a direct factory that raises on construction (e.g.
    ``transport_for`` refusing a malformed spec at call time) must not
    hard-fail a call the proxy would still serve -- typed fallback, never a
    raw propagated exception."""

    from clio_agent.tools.mcp_connection_era import record_task_capability

    namespace = "unit-factory-construction-failure"
    record_task_capability(namespace, task_capable=True, source="capabilities_extensions")

    def _broken_factory() -> Any:
        raise RuntimeError("simulated transport_for failure")

    client, decision = resolve_and_build_direct_client(namespace, {namespace: _broken_factory})
    assert client is None
    assert decision.use_direct is False
    assert decision.reason == MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED


def test_capability_demotion_guard_refuses_a_downgraded_false() -> None:
    """#1281 F7: a True verdict sourced from the AUTHORITATIVE modern key
    (``capabilities_extensions``) must not be clobbered by a False read at a
    LEGACY-negotiated era (a possible #1186 downgrade race on a genuinely
    modern, task-capable server) -- the refusal is itself typed + queryable,
    never a silently dropped write."""

    from clio_agent.tools.mcp_connection_era import record_task_capability

    namespace = "unit-demotion-guard"
    true_record = record_task_capability(
        namespace, task_capable=True, source="capabilities_extensions", era="modern"
    )
    refused = record_task_capability(namespace, task_capable=False, source="none", era="legacy")
    assert refused == true_record, "a refused demotion must return the EXISTING record"
    assert latest_task_capability(namespace) == true_record

    # An EQUALLY authoritative (modern) False legitimately demotes.
    demoted = record_task_capability(namespace, task_capable=False, source="none", era="modern")
    assert demoted.task_capable is False
    assert latest_task_capability(namespace) == demoted
