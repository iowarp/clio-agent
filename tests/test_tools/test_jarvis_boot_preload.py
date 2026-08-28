"""Regression: the curated JARVIS surface must resolve on a cold boot.

v1.7.0 live-verification campaign, final blocker. Confirmed by a black-box
trace against a real cluster: on a cold boot with relay fully configured and
discovery SUCCEEDING (``relay first discovery reason=None
federation=present``), the raw ``remote_jarvis_*``/``remote_spack_*``
federation tools mount and work, but the six curated ``jarvis_*`` tools
(``jarvis_create_pipeline``, ``jarvis_describe``, ``jarvis_add_step``,
``jarvis_edit_step``, ``jarvis_run``, ``jarvis_get_execution``) brick typed
``custom_agent_tool_unavailable`` with a BARE detail -- no
"server mount failed reason=..." -- meaning
``_resolve_declared_tools_with_on_demand_mount`` never even attempted a
mount.

Root cause (two compounding gaps):

1. ``ClioAgent._build_tool_gateway`` seeded ``self._tool_definitions`` from
   ``list_builtin_tool_definitions()`` + ``list_relay_tool_definitions(federation)``
   only -- the curated JARVIS surface (an in-process FastMCP server, exactly
   like the fs/shell built-ins) was never queried for its own tool list, so
   the six ``jarvis_*`` tools were absent from every executor's
   ``to_dspy_tools()`` snapshot from construction onward.
2. ``jarvis`` carries no ``MCPServerSpec`` (it is a curated in-process
   surface mounted directly via ``build_gateway(jarvis_jobs=...)``, not a
   spawned/proxied declared server), so it is also never a
   ``_clio_namespace_specs`` key -- the on-demand-mount fallback in
   ``gact/agents/builders.py`` only attempts a mount for a namespace present
   in that map, so it silently skips jarvis rather than attempting (and
   failing) a real mount.

The fix is ``clio_agent.tools.gateway.list_jarvis_tool_definitions``,
seeded into ``ClioAgent._build_tool_gateway`` and
``gact/relay_wiring.py::_refresh_agent_relay_tool_surfaces`` the same way
the relay federation projection already is (#1232 gap) -- matching how the
in-process built-ins are handled, never the (structurally inapplicable)
on-demand MCPServerSpec mount path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from mcp.types import Tool as McpTool

from clio_agent.agent import ClioAgent
from clio_agent.gact.agents.builders import _resolve_declared_tools_with_on_demand_mount
from clio_agent.tools.gateway import (
    build_gateway,
    list_builtin_tool_definitions,
    list_jarvis_tool_definitions,
    list_relay_tool_definitions,
    namespace_specs,
)
from clio_agent.tools.jarvis_jobs import JARVIS_TOOL_NAMES, JarvisJobs
from clio_agent.tools.relay_transport import RelayRemoteMcpCatalog
from clio_agent.tools.remote_mcp import RemoteMcpFederation


class _RecordingExecutor:
    """Minimal stand-in exposing exactly what
    ``_resolve_declared_tools_with_on_demand_mount`` reads/writes (mirrors
    ``tests/test_gact/test_on_demand_mount.py``'s ``_FakeExecutor``), seeded
    from REAL ``build_gateway``/``list_*_tool_definitions`` output so the
    mounting/listing logic under test is genuine, not reimplemented."""

    def __init__(self, preloaded: dict[str, Any], declared_specs: dict[str, Any]) -> None:
        self._mcp_tools: dict[str, Any] = dict(preloaded)
        self._clio_namespace_specs = declared_specs

    def to_dspy_tools(self) -> list[Any]:
        return [SimpleNamespace(name=name) for name in self._mcp_tools]

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        del namespace
        self._mcp_tools.update(tools)


def _stub_jarvis_jobs() -> JarvisJobs:
    """A JarvisJobs surface whose client factory is never called: only the
    six curated tools' NAMES need to resolve for this test, never dispatch."""

    return JarvisJobs(lambda: None)  # type: ignore[arg-type]


def test_pre_fix_composition_bricks_jarvis_with_no_mount_attempt() -> None:
    """Characterizes the exact brick shape: builtins + relay federation only
    (the composition ``ClioAgent._build_tool_gateway`` used before the fix),
    with a jarvis_jobs surface mounted on the gateway but never queried for
    its own tool definitions. The six curated tools are unresolvable AND
    ``mount_failures`` is empty -- proving no mount was ever attempted, the
    exact "bare detail" signature from the live trace."""

    jarvis = _stub_jarvis_jobs()
    gateway = build_gateway({}, jarvis_jobs=jarvis)
    preloaded = list_builtin_tool_definitions()
    preloaded.update(list_relay_tool_definitions(None))
    executor = _RecordingExecutor(preloaded, dict(namespace_specs(gateway)))

    available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
        executor, list(JARVIS_TOOL_NAMES)
    )

    assert not (set(JARVIS_TOOL_NAMES) & set(available)), (
        "the old builtins+relay-only preload must not resolve any jarvis tool"
    )
    assert mount_failures == {}, "no on-demand mount is even attempted for an undeclared namespace"
    assert "jarvis" not in executor._clio_namespace_specs, (
        "jarvis carries no MCPServerSpec -- it is never a declared_specs key by design"
    )


def test_jarvis_tools_resolve_on_a_cold_boot_the_way_production_seeds_them() -> None:
    """FAILING-FIRST for the v1.7.0 blocker: mirroring
    ``ClioAgent._build_tool_gateway``'s real boot-preload composition
    (builtins + relay federation + the curated JARVIS surface), the six
    curated jarvis_* tools must resolve with no mount_failures -- exactly
    the observable a real custom agent's tool resolve checks."""

    jarvis = _stub_jarvis_jobs()
    gateway = build_gateway({}, jarvis_jobs=jarvis)
    preloaded = list_builtin_tool_definitions()
    preloaded.update(list_relay_tool_definitions(None))
    preloaded.update(list_jarvis_tool_definitions(jarvis))
    executor = _RecordingExecutor(preloaded, dict(namespace_specs(gateway)))

    available, mount_failures = _resolve_declared_tools_with_on_demand_mount(
        executor, list(JARVIS_TOOL_NAMES)
    )

    assert set(JARVIS_TOOL_NAMES) <= set(available), (
        "all six curated jarvis tools must resolve on a cold boot"
    )
    assert mount_failures == {}


def test_list_jarvis_tool_definitions_empty_without_jarvis_jobs() -> None:
    assert list_jarvis_tool_definitions(None) == {}


def test_list_jarvis_tool_definitions_prefixes_and_covers_all_six() -> None:
    jarvis = _stub_jarvis_jobs()

    definitions = list_jarvis_tool_definitions(jarvis)

    assert set(definitions) == set(JARVIS_TOOL_NAMES)
    for name, tool in definitions.items():
        assert tool.name == name


def _client_factory_never_called() -> Any:
    raise AssertionError("client factory must not be opened during agent construction")


def test_real_agent_construction_seeds_jarvis_and_relay_fetch_artifact() -> None:
    """PINS ``ClioAgent._build_tool_gateway``'s seeding call site (agent.py
    ~389-392) -- the review's D1 finding: pre-fix that call site was pinned
    by NOTHING, since every other test in this file (above) re-implements
    the builtins+relay+jarvis composition by hand instead of constructing a
    real ``ClioAgent``. Reverting the seeding call left the full suite green.

    Constructs a REAL ``ClioAgent`` with a jarvis surface AND a relay
    federation carrying the follow-server-mounted ``relay_fetch_artifact``
    tool (#1200), and asserts both land on ``agent._tool_definitions`` -- the
    dict every executor's ``preloaded_tools`` and ``/v1/tools`` actually
    read. Both client factories raise if ever opened: construction is proven
    I/O-free (nothing here ever dispatches a real call), so this stays a fast
    unit test."""

    jarvis = JarvisJobs(_client_factory_never_called)
    catalog = RelayRemoteMcpCatalog(
        revision="d" * 64,
        tools={},
        follow_tools={
            "relay_wait": McpTool(
                name="relay_wait",
                inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}},
            )
        },
    )
    federation = RemoteMcpFederation(
        catalog, _client_factory_never_called, cluster_hint="ares-p5run2"
    )

    agent = ClioAgent(remote_mcp_federation=federation, jarvis_jobs=jarvis)
    try:
        assert agent._tool_definitions is not None, (
            "boot-preload catalog derivation must not have degraded"
        )
        names = set(agent._tool_definitions)
        assert set(JARVIS_TOOL_NAMES) <= names, (
            "the curated jarvis_* tools must seed at real ClioAgent construction"
        )
        assert "relay_fetch_artifact" in names, (
            "the follow-server-mounted relay_fetch_artifact tool must seed too"
        )
    finally:
        agent.shutdown()
