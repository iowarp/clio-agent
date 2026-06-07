"""Tests for the low-friction MCP server declaration format (Workstream C)."""

from __future__ import annotations

import pytest
import yaml

from clio_agent.tools.mcp_config import (
    MCPConfigError,
    expand_env,
    load_mcp_servers,
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

    servers = load_mcp_servers(home=home, cwd=cwd, pack_servers=pack_servers, env={})
    assert servers["ndp"].command == "workspace-ndp"  # workspace wins
    assert servers["ndp"].source == "workspace"
    assert servers["geo"].command == "pack-geo"  # only in pack frontmatter
    assert servers["weather"].command == "user-weather"


def test_transport_for():
    stdio = transport_for(spec_from_declaration("ndp", "uvx clio-kit run ndp"))
    assert not isinstance(stdio, str)
    assert transport_for(spec_from_declaration("n", "https://h/mcp")) == "https://h/mcp"
