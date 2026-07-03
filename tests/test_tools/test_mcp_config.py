"""Tests for the low-friction MCP server declaration format (Workstream C)."""

from __future__ import annotations

import pytest
import yaml

from clio_agent.tools.mcp_config import (
    MCPConfigError,
    expand_env,
    load_mcp_servers,
    resolve_expert_servers,
    spec_from_declaration,
    specs_from_mapping,
    transport_for,
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
