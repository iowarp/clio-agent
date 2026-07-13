"""Tests for the CLIO MCP gateway (built-ins + declared MCP proxy mounts).

Core ships only the universal in-process built-ins (fs/shell); domain tools are
declared MCP servers mounted as proxies. These tests are hermetic: declared
servers are stood up as in-process FastMCP proxies (no subprocess, no network).
"""

import warnings

import pytest
from fastmcp import Client, FastMCP

from clio_agent.tools.gateway import (
    _list_tools_sync,
    _mount_with_namespace,
    _namespace_of,
    build_gateway,
    gateway,
    get_gateway,
    list_capabilities,
)
from clio_agent.tools.mcp_config import MCPServerSpec, spec_from_declaration


def test_mount_helper_uses_namespace_when_supported():
    """Gateway helper should use FastMCP namespace API when available."""
    calls = []

    class Parent:
        def mount(self, server, namespace=None):
            calls.append((server, namespace))

    server = object()
    _mount_with_namespace(Parent(), server, "fs")

    assert calls == [(server, "fs")]


def test_mount_helper_falls_back_to_prefix():
    """Installed FastMCP 2.x exposes prefix, so the helper must preserve it."""
    calls = []

    class Parent:
        def mount(self, server, prefix=None):
            calls.append((server, prefix))

    server = object()
    _mount_with_namespace(Parent(), server, "shell")

    assert calls == [(server, "shell")]


def test_namespace_of_splits_on_first_underscore():
    assert _namespace_of("ndp_search_datasets") == "ndp"
    assert _namespace_of("fs_read_file") == "fs"
    assert _namespace_of("nounderscore") == "nounderscore"


@pytest.mark.asyncio
async def test_gateway_exposes_only_builtins_by_default():
    """The singleton gateway mounts only the universal built-ins (fs/shell)."""
    async with Client(gateway) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}

    assert "shell_bash" in names
    assert "fs_read_file" in names
    assert "fs_propose_edit" in names
    assert "fs_apply_edit_write" in names
    # No domain/case tools live in core anymore.
    assert not any(n.startswith("hdf5_") for n in names)
    assert not any(n.startswith("ndp_") for n in names)


@pytest.mark.asyncio
async def test_get_gateway_helper():
    """get_gateway() returns the gateway instance."""
    gw = get_gateway()
    assert gw is gateway
    assert gw.name == "clio-gateway"


def test_list_capabilities_namespaces_built_ins():
    caps = list_capabilities()
    assert {c["server"] for c in caps} == {"fs", "shell"}


# ---------------------------------------------------------------------------
# Declared MCP proxy mounts (the only source of domain tools)
# ---------------------------------------------------------------------------


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


def test_build_gateway_mounts_builtins_plus_declared(declared_server: FastMCP):
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway({"demo": spec}, proxy_factory=_in_process_factory(declared_server))
    names = {t.name for t in _list_tools_sync(gw)}
    # built-ins always present
    assert "shell_bash" in names
    assert any(n.startswith("fs_") for n in names)
    # declared tool namespaced under the spec name
    assert "demo_ping" in names


def test_build_gateway_declared_tool_callable(declared_server: FastMCP):
    import asyncio

    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway({"demo": spec}, proxy_factory=_in_process_factory(declared_server))

    async def _call():
        async with Client(gw) as client:
            return await client.call_tool("demo_ping", {"text": "hi"})

    result = asyncio.run(_call())
    assert result.data == "hi"


def test_build_gateway_no_specs_is_builtins_only():
    gw = build_gateway({})
    names = {t.name for t in _list_tools_sync(gw)}
    assert "shell_bash" in names
    assert not any(n.startswith("demo_") for n in names)


def test_build_gateway_skips_unusable_spec(declared_server: FastMCP):
    bad = MCPServerSpec(
        name="bad",
        transport="stdio",
        validation_errors=("required environment variable ${X} is unset",),
    )
    gw = build_gateway({"bad": bad}, proxy_factory=_in_process_factory(declared_server))
    names = {t.name for t in _list_tools_sync(gw)}
    assert not any(n.startswith("bad_") for n in names)


def test_build_gateway_skips_underscore_named_declared_server(declared_server: FastMCP):
    """A declared server whose name contains ``_`` is unusable and never mounts.

    The name is validated at declaration (``spec_from_declaration``) because ``_``
    delimits the tool namespace; the resulting validation error keeps the server
    out of the gateway rather than mis-namespacing its tools.
    """
    spec = spec_from_declaration("my_server", "x")
    assert not spec.usable
    gw = build_gateway({"my_server": spec}, proxy_factory=_in_process_factory(declared_server))
    names = {t.name for t in _list_tools_sync(gw)}
    assert not any(n.startswith("my_") for n in names)


def test_build_gateway_skips_builtin_namespace_collision(declared_server: FastMCP):
    """A declared server may not shadow a reserved built-in namespace."""
    spec = MCPServerSpec(name="fs", transport="stdio", command="x")
    gw = build_gateway({"fs": spec}, proxy_factory=_in_process_factory(declared_server))
    names = {t.name for t in _list_tools_sync(gw)}
    # fs tools remain the in-process ones; declared 'ping' did NOT get mounted
    assert "fs_read_file" in names
    assert "fs_ping" not in names


def test_build_gateway_mounts_user_server_named_web(declared_server: FastMCP):
    """``web`` is a free namespace — a user MCP server named ``web`` mounts.

    Regression guard for the #769 phantom-builtin fix: ``web`` sat in the
    reserved-namespace set with no backing server, so a user server named
    ``web`` was silently skipped. Only ``fs`` and ``shell`` are reserved.
    """
    spec = MCPServerSpec(name="web", transport="stdio", command="x")
    gw = build_gateway({"web": spec}, proxy_factory=_in_process_factory(declared_server))
    names = {t.name for t in _list_tools_sync(gw)}
    assert "web_ping" in names


def test_build_gateway_threads_cwd_to_stdio_only(declared_server: FastMCP):
    """``cwd`` reaches stdio specs (per-workspace spawn) but never http specs."""
    seen: dict[str, str | None] = {}

    def factory(spec: MCPServerSpec, cwd: str | None = None) -> FastMCP:
        seen[spec.name] = cwd
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return FastMCP.as_proxy(Client(declared_server))

    specs = {
        "local": MCPServerSpec(name="local", transport="stdio", command="x"),
        "remote": MCPServerSpec(name="remote", transport="http", url="https://h/mcp"),
    }
    build_gateway(specs, cwd="/work/space", proxy_factory=factory)

    assert seen["local"] == "/work/space"  # stdio honors the workspace cwd
    assert seen["remote"] is None  # http stays shared, ignores cwd


def test_build_gateway_legacy_proxy_factory_without_cwd(declared_server: FastMCP):
    """A factory that takes only the spec still works (back-compat)."""
    spec = MCPServerSpec(name="demo", transport="stdio", command="x")
    gw = build_gateway(
        {"demo": spec},
        cwd="/work/space",
        proxy_factory=_in_process_factory(declared_server),
    )
    names = {t.name for t in _list_tools_sync(gw)}
    assert "demo_ping" in names
