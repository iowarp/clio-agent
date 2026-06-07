"""Tests for standard MCP server declaration parsing (Workstream C foundation)."""

from __future__ import annotations

import json

import pytest

from clio_agent.tools.mcp_config import (
    MCPConfigError,
    expand_env,
    load_mcp_servers,
    spec_from_entry,
    transport_for,
)


def test_expand_env_default_and_required():
    env = {"FOO": "bar"}
    assert expand_env("${FOO}", env=env) == "bar"
    assert expand_env("${MISSING:-fallback}", env=env) == "fallback"
    assert expand_env("a/${FOO}/b", env=env) == "a/bar/b"
    with pytest.raises(MCPConfigError):
        expand_env("${UNSET_REQUIRED}", env={})


def test_spec_from_entry_stdio():
    spec = spec_from_entry(
        "ndp",
        {
            "command": "uvx",
            "args": ["--from", "clio-kit", "clio-kit", "mcp-server", "ndp"],
            "timeout": 600000,
        },
        source="pack:demo",
    )
    assert spec.transport == "stdio"
    assert spec.command == "uvx"
    assert spec.args[-1] == "ndp"
    assert spec.timeout_ms == 600000
    assert spec.usable


def test_spec_from_entry_stdio_env_expansion():
    spec = spec_from_entry(
        "ndp",
        {
            "command": "uv",
            "args": ["--directory", "${CLIO_KIT_PATH:-../clio-kit}", "run", "clio-kit"],
        },
        env={},
    )
    assert spec.args[1] == "../clio-kit"  # default applied
    assert spec.usable


def test_spec_from_entry_http():
    spec = spec_from_entry("notion", {"type": "http", "url": "https://mcp.notion.com/mcp"})
    assert spec.transport == "http"
    assert spec.url == "https://mcp.notion.com/mcp"
    assert spec.usable


def test_spec_from_entry_http_inferred_from_url():
    spec = spec_from_entry(
        "x",
        {"url": "https://h/mcp", "headers": {"Authorization": "Bearer ${TOK}"}},
        env={"TOK": "t"},
    )
    assert spec.transport == "http"
    assert spec.headers["Authorization"] == "Bearer t"


def test_invalid_entries_recorded_not_raised():
    missing_cmd = spec_from_entry("bad", {"type": "stdio"})
    assert not missing_cmd.usable
    assert any("command" in e for e in missing_cmd.validation_errors)

    missing_url = spec_from_entry("bad2", {"type": "http"})
    assert not missing_url.usable

    bad_required_env = spec_from_entry(
        "bad3", {"command": "x", "args": ["${REQUIRED_UNSET}"]}, env={}
    )
    assert not bad_required_env.usable


def test_load_mcp_servers_precedence(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    pack = tmp_path / "pack"
    for p in (home, cwd, pack):
        p.mkdir(parents=True)

    (pack / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ndp": {"command": "pack-ndp"},
                    "geo": {"command": "pack-geo"},
                }
            }
        )
    )
    (home / ".config" / "clio-agent").mkdir(parents=True)
    (home / ".config" / "clio-agent" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ndp": {"command": "user-ndp"},  # overrides pack
                    "weather": {"command": "user-weather"},
                }
            }
        )
    )
    (cwd / ".clio").mkdir(parents=True)
    (cwd / ".clio" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ndp": {"command": "workspace-ndp"},  # overrides user + pack
                }
            }
        )
    )

    servers = load_mcp_servers(home=home, cwd=cwd, pack_roots=[pack], env={})
    assert servers["ndp"].command == "workspace-ndp"  # workspace wins
    assert servers["ndp"].source == "workspace"
    assert servers["geo"].command == "pack-geo"  # only in pack
    assert servers["weather"].command == "user-weather"  # only in user


def test_transport_for_stdio_and_http():
    stdio = transport_for(spec_from_entry("ndp", {"command": "uvx", "args": ["x"]}))
    # StdioTransport from fastmcp; just assert it is not the url passthrough
    assert not isinstance(stdio, str)
    http = transport_for(spec_from_entry("n", {"type": "http", "url": "https://h/mcp"}))
    assert http == "https://h/mcp"
