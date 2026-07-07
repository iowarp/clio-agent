"""Tests for the low-friction MCP server declaration format (Workstream C)."""

from __future__ import annotations

import sys

import pytest
import yaml

from clio_agent.tools.mcp_config import (
    MCPConfigError,
    MCPTransportError,
    expand_env,
    load_mcp_servers,
    resolve_expert_servers,
    spec_from_declaration,
    specs_from_mapping,
    transport_for,
    transport_from_spec,
)


def test_expand_env_default_and_required():
    env = {"FOO": "bar"}
    assert expand_env("${FOO}", env=env) == "bar"
    assert expand_env("${MISSING:-fallback}", env=env) == "fallback"
    assert expand_env("a/${FOO}/b", env=env) == "a/bar/b"
    with pytest.raises(MCPConfigError):
        expand_env("${UNSET_REQUIRED}", env={})


def test_string_command_form():
    spec = spec_from_declaration("ndp", "uvx clio-kit run ndp --basemap", source="pack:demo")
    assert spec.transport == "stdio"
    assert spec.command == "uvx"
    assert spec.args == ("clio-kit", "run", "ndp", "--basemap")
    assert spec.usable


def test_string_url_form():
    spec = spec_from_declaration("notion", "https://mcp.notion.com/mcp")
    assert spec.transport == "http"
    assert spec.url == "https://mcp.notion.com/mcp"
    assert spec.usable


def test_string_form_env_expansion():
    spec = spec_from_declaration(
        "ndp", "uv --directory ${CLIO_KIT_PATH:-../clio-kit} run clio-kit mcp-server ndp", env={}
    )
    assert spec.args[1] == "../clio-kit"
    spec2 = spec_from_declaration("x", "tool ${REQUIRED_UNSET}", env={})
    assert not spec2.usable  # required var unset -> recorded, not raised


def test_mapping_form_advanced_env_headers():
    stdio = spec_from_declaration(
        "w", {"command": "uvx", "args": "weather-mcp", "env": {"K": "${TOK}"}}, env={"TOK": "t"}
    )
    assert stdio.transport == "stdio" and stdio.env["K"] == "t" and stdio.args == ("weather-mcp",)
    http = spec_from_declaration(
        "n",
        {"url": "https://h/mcp", "headers": {"Authorization": "Bearer ${TOK}"}},
        env={"TOK": "t"},
    )
    assert http.transport == "http" and http.headers["Authorization"] == "Bearer t"


def test_underscore_server_name_is_rejected_with_structured_error():
    """An ``_`` in a server name breaks ``_namespace_of`` — reject at declaration."""
    spec = spec_from_declaration("my_server", "uvx clio-kit run x")
    assert not spec.usable
    assert any("my_server" in e and "namespace" in e for e in spec.validation_errors)


def test_non_lowercase_alnum_server_name_is_rejected():
    """Names outside ``[a-z0-9-]`` (uppercase, dots) are rejected too."""
    assert not spec_from_declaration("MyServer", "uvx x").usable
    assert not spec_from_declaration("srv.one", "uvx x").usable
    assert not spec_from_declaration("", "uvx x").usable


def test_valid_hyphen_and_digit_server_names_stay_usable():
    """Legal ``[a-z0-9-]`` names remain usable — no false positives."""
    assert spec_from_declaration("ndp-geo2", "uvx clio-kit run x").usable
    assert spec_from_declaration("fs", "uvx x").usable


def test_specs_from_mapping():
    specs = specs_from_mapping(
        {"ndp": "uvx clio-kit run ndp", "geo": "uvx clio-kit run geo"}, source="pack:p"
    )
    assert set(specs) == {"ndp", "geo"}
    assert specs["geo"].command == "uvx" and specs["geo"].source == "pack:p"


def test_load_precedence_frontmatter_user_workspace(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    (home / ".config" / "clio-agent").mkdir(parents=True)
    (cwd / ".clio").mkdir(parents=True)

    pack_servers = {"earthscope": {"ndp": "pack-ndp", "geo": "pack-geo"}}
    (home / ".config" / "clio-agent" / "mcp.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"ndp": "user-ndp", "weather": "user-weather"}})
    )
    (cwd / ".clio" / "mcp.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"ndp": "workspace-ndp"}})
    )

    # Point the user-config resolver at the XDG layout this test writes into, so the
    # fixture is deterministic across OSes (on Windows the resolver otherwise uses
    # %LOCALAPPDATA%/clio-agent, not home/.config/clio-agent).
    env = {"XDG_CONFIG_HOME": str(home / ".config")}
    servers = load_mcp_servers(home=home, cwd=cwd, pack_servers=pack_servers, env=env)
    assert servers["ndp"].command == "workspace-ndp"  # workspace wins
    assert servers["ndp"].source == "workspace"
    assert servers["geo"].command == "pack-geo"  # only in pack frontmatter
    assert servers["weather"].command == "user-weather"


def test_resolve_expert_servers_select_and_local():
    glob = specs_from_mapping(
        {"ndp": "uvx clio-kit run ndp", "geo": "uvx clio-kit run geo"}, source="pack:p"
    )
    # per-expert SELECT from global by name
    sel = resolve_expert_servers(glob, ["geo"])
    assert set(sel) == {"geo"} and sel["geo"].command == "uvx"
    # undeclared name -> recorded error, not silent
    bad = resolve_expert_servers(glob, ["nope"])
    assert not bad["nope"].usable
    # per-expert LOCAL declaration (mapping) adds/overrides for that expert only
    local = resolve_expert_servers(glob, {"private": "uvx my-private-mcp"})
    assert local["private"].command == "uvx" and local["private"].args == ("my-private-mcp",)
    # no declaration -> nothing extra
    assert resolve_expert_servers(glob, None) == {}


def test_transport_for():
    import shutil

    # The launcher is resolved to an ABSOLUTE path (``sh`` is universally on PATH).
    stdio = transport_for(spec_from_declaration("ndp", "sh -c true"))
    assert not isinstance(stdio, str)
    assert stdio.command == shutil.which("sh")
    assert transport_for(spec_from_declaration("n", "https://h/mcp")) == "https://h/mcp"


def test_transport_for_stdio_cwd(tmp_path):
    """stdio transports spawn in the given cwd; http transports ignore it."""
    work = tmp_path / "ws"
    work.mkdir()
    stdio = transport_for(spec_from_declaration("ndp", "sh -c true"), cwd=str(work))
    assert getattr(stdio, "cwd", None) == str(work)
    # Default (no cwd) keeps the spawning process's directory.
    default = transport_for(spec_from_declaration("ndp", "sh -c true"))
    assert getattr(default, "cwd", None) is None
    # http ignores cwd entirely.
    assert transport_for(spec_from_declaration("n", "https://h/mcp"), cwd=str(work)) == (
        "https://h/mcp"
    )


def test_transport_for_resolves_relative_launcher_to_absolute(tmp_path):
    """Regression for the cwd-bound spawn bug (gact-tui default-deploy ``os error 2``).

    A *relative* launcher (``uvx``/``sh``) combined with a set ``cwd`` makes the OS
    resolve the executable relative to that cwd (``<cwd>/sh``), which does not exist
    -> the subprocess dies with ``No such file or directory (os error 2)``, the proxy
    connection drops, and the expert fails downstream as opaque "tools unavailable".
    Resolving to an absolute path up front (while still spawning IN ``cwd``) fixes it.
    """
    import os
    import shutil

    work = tmp_path / "ws"
    work.mkdir()
    stdio = transport_for(spec_from_declaration("ndp", "sh -c true"), cwd=str(work))
    assert os.path.isabs(stdio.command)
    assert stdio.command == shutil.which("sh")
    assert stdio.cwd == str(work)  # still spawns in the workspace


def test_transport_for_missing_cwd_fails_loud():
    """A nonexistent working directory raises a precise MCPSpawnError, never os error 2."""
    from clio_agent.tools.mcp_config import MCPSpawnError

    with pytest.raises(MCPSpawnError, match="working directory"):
        transport_for(spec_from_declaration("geo", "sh -c true"), cwd="/no/such/dir/xyz123")


def test_transport_for_unresolved_command_fails_loud():
    """An unresolvable launcher raises a precise MCPSpawnError naming the command."""
    from clio_agent.tools.mcp_config import MCPSpawnError

    with pytest.raises(MCPSpawnError, match="not found on PATH"):
        transport_for(spec_from_declaration("geo", "definitely-not-a-real-binary-xyz123 run"))


# --- transport_from_spec: ONE canonical accepted set (#770 C2) -------------


@pytest.mark.parametrize("kind", ["http", "streamable-http"])
def test_transport_from_spec_http_family_yield_streamable_http(kind: str) -> None:
    """``http``/``streamable-http`` build a ``StreamableHttpTransport`` on the url.

    This is the crux of the C2 fix: before, ``streamable-http`` was accepted by
    some call sites and rejected by others (500 / vanished tool). Both aliases
    must now resolve to a ``StreamableHttpTransport``. ``sse`` is a DISTINCT wire
    protocol and is covered separately below.
    """
    from fastmcp.client.transports import StreamableHttpTransport

    transport = transport_from_spec({"transport": kind, "url": "https://mcp.example.com/mcp"})
    assert isinstance(transport, StreamableHttpTransport)


def test_transport_from_spec_sse_yields_sse_transport() -> None:
    """``sse`` builds an ``SSETransport`` — NOT a ``StreamableHttpTransport``.

    FastMCP treats SSE and Streamable-HTTP as distinct wire protocols. Routing an
    ``sse`` descriptor through ``StreamableHttpTransport`` lands a real SSE MCP
    server in error/no_tools. The construction must match what FastMCP's own
    ``infer_transport`` selects for an ``/sse`` URL.
    """
    from fastmcp.client.transports import (
        SSETransport,
        StreamableHttpTransport,
        infer_transport,
    )

    url = "https://mcp.example.com/sse"
    transport = transport_from_spec({"transport": "sse", "url": url})
    assert isinstance(transport, SSETransport)
    assert not isinstance(transport, StreamableHttpTransport)
    # Matches FastMCP's own routing for an /sse URL.
    assert type(transport) is type(infer_transport(url))


def test_transport_from_spec_stdio_yields_stdio_transport() -> None:
    from fastmcp.client.transports import StdioTransport

    transport = transport_from_spec({"transport": "stdio", "command": "echo", "args": ["hi"]})
    assert isinstance(transport, StdioTransport)


def test_transport_from_spec_unknown_transport_raises_typed_error() -> None:
    with pytest.raises(MCPTransportError, match="unknown MCP transport"):
        transport_from_spec({"transport": "carrier-pigeon", "url": "x"})


def test_transport_from_spec_stdio_missing_command_raises() -> None:
    with pytest.raises(MCPTransportError, match="command"):
        transport_from_spec({"transport": "stdio", "args": []})


def test_transport_from_spec_http_missing_url_raises() -> None:
    with pytest.raises(MCPTransportError, match="url"):
        transport_from_spec({"transport": "streamable-http"})


def test_transport_from_spec_stdio_pdeathsig_wrapped_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stdio child built via the shared helper is setpriv-wrapped on Linux.

    pdeathsig folding is what stops REST-installed stdio servers from orphaning on
    a hard clio-server kill — and it must apply to EVERY stdio spawn path (install /
    list / call / reconnect), not just the agent path. Proving it inside the helper
    proves it for all of them at once.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.shutil.which", lambda _name: "/usr/bin/setpriv"
    )
    transport = transport_from_spec({"transport": "stdio", "command": "uvx", "args": ["geo-mcp"]})
    assert transport.command == "/usr/bin/setpriv"
    assert list(transport.args) == ["--pdeathsig", "SIGKILL", "--", "uvx", "geo-mcp"]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_transport_from_spec_stdio_no_pdeathsig_off_linux(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Cross-platform guard: on Windows/macOS the stdio spawn is an unwrapped
    passthrough (setpriv is Linux-only), mirroring pdeathsig_wrapped_command."""
    monkeypatch.setattr(sys, "platform", platform)
    transport = transport_from_spec({"transport": "stdio", "command": "uvx", "args": ["geo-mcp"]})
    assert transport.command == "uvx"
    assert list(transport.args) == ["geo-mcp"]
