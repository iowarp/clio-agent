"""Tests for declaration-driven, flag-gated, additive MCP gateway building.

These tests are fully hermetic: a tiny in-process FastMCP is wrapped as a
declared proxy (no subprocess, no network). With the ``CLIO_DECLARED_MCPS`` flag
ON, declared tools appear namespaced in the gateway and catalog; with the flag
OFF (default), the gateway and catalog are byte-for-byte the current behavior.
"""

from __future__ import annotations

import warnings

import pytest
from fastmcp import Client, FastMCP

from clio_agent.tools.catalog import TOOL_CATALOG
from clio_agent.tools.gateway import (
    DECLARED_MCPS_FLAG,
    _list_tools_sync,
    _mount_with_namespace,
    build_gateway,
    build_tool_catalog,
    declared_mcps_enabled,
)
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.servers.fs_server import fs_server


@pytest.fixture
def declared_server() -> FastMCP:
    """A tiny in-process FastMCP standing in for a declared MCP server."""
    server = FastMCP("declared-demo")

    @server.tool(tags={"alpha", "beta"})
    def ping(text: str) -> str:
        """Echo the text back."""
        return text

    return server


def _in_process_factory(declared: FastMCP):
    """Proxy factory that wraps an in-process server (no subprocess)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        def factory(_spec: MCPServerSpec) -> FastMCP:
            return FastMCP.as_proxy(Client(declared))

    return factory


def _fresh_base() -> FastMCP:
    """A fresh gateway with one built-in in-process server mounted (fs)."""
    base = FastMCP("clio-gateway")
    _mount_with_namespace(base, fs_server, "fs")
    return base


# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------


def test_flag_default_off():
    assert declared_mcps_enabled(env={}) is False
    assert declared_mcps_enabled(env={DECLARED_MCPS_FLAG: ""}) is False
    assert declared_mcps_enabled(env={DECLARED_MCPS_FLAG: "0"}) is False
    assert declared_mcps_enabled(env={DECLARED_MCPS_FLAG: "false"}) is False


def test_flag_on_truthy_values():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert declared_mcps_enabled(env={DECLARED_MCPS_FLAG: value}) is True


# ---------------------------------------------------------------------------
# Flag ON: declared tools appear namespaced in gateway + catalog
# ---------------------------------------------------------------------------


def test_flag_on_declared_tool_namespaced_in_gateway(declared_server: FastMCP):
    base = _fresh_base()
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway(
        {"demo": spec},
        base_gateway=base,
        proxy_factory=_in_process_factory(declared_server),
    )
    names = {t.name for t in _list_tools_sync(gw)}
    # declared tool is namespaced under the spec name
    assert "demo_ping" in names
    # additive: the pre-existing in-process server is untouched
    assert any(n.startswith("fs_") for n in names)


def test_flag_on_declared_tool_callable_through_gateway(declared_server: FastMCP):
    base = _fresh_base()
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway(
        {"demo": spec},
        base_gateway=base,
        proxy_factory=_in_process_factory(declared_server),
    )

    import asyncio

    async def _call():
        async with Client(gw) as client:
            return await client.call_tool("demo_ping", {"text": "hi"})

    result = asyncio.run(_call())
    assert result.data == "hi"


def test_flag_on_declared_tool_in_catalog(declared_server: FastMCP):
    base = _fresh_base()
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway(
        {"demo": spec},
        base_gateway=base,
        proxy_factory=_in_process_factory(declared_server),
    )
    catalog = build_tool_catalog(gw)
    assert "demo_ping" in catalog
    entry = catalog["demo_ping"]
    assert entry.owner == "demo"
    assert "demo" in entry.tags
    assert "alpha" in entry.tags and "beta" in entry.tags
    assert "demo" in entry.visible_to
    # static catalog entries survive the merge
    assert "hdf5_analyze_file" in catalog
    assert catalog["hdf5_analyze_file"] == TOOL_CATALOG["hdf5_analyze_file"]


def test_static_catalog_entries_win_over_derived(declared_server: FastMCP):
    """A declared tool whose name collides with a static entry keeps the static one."""
    base = _fresh_base()
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway(
        {"demo": spec},
        base_gateway=base,
        proxy_factory=_in_process_factory(declared_server),
    )
    # craft a static catalog that already owns demo_ping with a different owner
    custom_static = dict(TOOL_CATALOG)
    from clio_agent.tools.catalog import ToolCatalogEntry

    custom_static["demo_ping"] = ToolCatalogEntry(
        name="demo_ping",
        owner="preexisting",
        tags=frozenset({"static"}),
        visible_to=frozenset({"preexisting"}),
    )
    catalog = build_tool_catalog(gw, static_catalog=custom_static)
    assert catalog["demo_ping"].owner == "preexisting"


# ---------------------------------------------------------------------------
# Flag OFF / no declarations: gateway + catalog equal current behavior
# ---------------------------------------------------------------------------


def test_no_declared_specs_is_noop_on_gateway():
    base = _fresh_base()
    before = {t.name for t in _list_tools_sync(base)}
    gw = build_gateway({}, base_gateway=base)
    after = {t.name for t in _list_tools_sync(gw)}
    assert gw is base
    assert after == before


def test_unusable_spec_is_skipped(declared_server: FastMCP):
    base = _fresh_base()
    before = {t.name for t in _list_tools_sync(base)}
    bad = MCPServerSpec(
        name="bad",
        transport="stdio",
        validation_errors=("required environment variable ${X} is unset",),
    )
    gw = build_gateway(
        {"bad": bad},
        base_gateway=base,
        proxy_factory=_in_process_factory(declared_server),
    )
    after = {t.name for t in _list_tools_sync(gw)}
    assert after == before  # nothing mounted for an unusable spec


def test_catalog_without_declared_gateway_equals_static():
    """build_tool_catalog(None) is the static catalog (default behavior)."""
    catalog = build_tool_catalog(None)
    assert catalog == dict(TOOL_CATALOG)
    # and it is a copy, not the module dict
    assert catalog is not TOOL_CATALOG


def test_default_gateway_singleton_unchanged_by_flag_off():
    """The module-level gateway singleton is the current in-process set of tools.

    With the flag default OFF the agent path never calls build_gateway, so the
    singleton must expose exactly the historical in-process namespaces.
    """
    from clio_agent.tools.gateway import gateway

    names = {t.name for t in _list_tools_sync(gateway)}
    # historical in-process tools still present, nothing declared mounted
    assert "hdf5_analyze_file" in names
    assert "ndp_search_datasets" in names
    assert not any(n.startswith("demo_") for n in names)


def test_collision_with_in_process_namespace_is_skipped(declared_server: FastMCP):
    """A declared server whose name shadows an in-process namespace is skipped."""
    base = _fresh_base()  # has 'fs' mounted in-process
    before = {t.name for t in _list_tools_sync(base)}
    spec = MCPServerSpec(name="fs", transport="stdio", command="x")
    gw = build_gateway(
        {"fs": spec},
        base_gateway=base,
        proxy_factory=_in_process_factory(declared_server),
    )
    after = {t.name for t in _list_tools_sync(gw)}
    # fs tools remain the in-process ones; declared 'ping' did NOT get mounted
    assert after == before
    assert "fs_ping" not in after
