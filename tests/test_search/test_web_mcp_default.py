"""CLIO declares the Web MCP server itself, loading by default, with a writable state dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.search import web_mcp_default
from clio_agent.search.settings import build_settings
from clio_agent.search.web_mcp_default import (
    DEFAULT_WEB_MCP_NAME,
    DEFAULT_WEB_MCP_SOURCE,
    with_default_web_mcp,
)
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_environment import stdio_environment


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_mcp_default, "_launcher_available", lambda: True)


@pytest.mark.usefixtures("launcher")
@pytest.mark.parametrize(
    "settings",
    [
        build_settings(),
        build_settings(backend="clio_web_search", clio_web_search_url="http://h:8089"),
    ],
)
def test_a_usable_backend_declares_the_web_mcp_for_the_main_agent(settings: object) -> None:
    specs = with_default_web_mcp({}, settings=settings)  # type: ignore[arg-type]
    web = specs[DEFAULT_WEB_MCP_NAME]
    assert (web.command, web.args) == ("clio-kit", ("mcp-server", "web"))
    assert web.always_load is True
    assert web.source == DEFAULT_WEB_MCP_SOURCE


@pytest.mark.usefixtures("launcher")
def test_search_off_declares_nothing() -> None:
    assert with_default_web_mcp({}, settings=build_settings(backend="none")) == {}


def test_a_missing_launcher_declares_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(web_mcp_default.shutil, "which", lambda _name: None)
    assert with_default_web_mcp({}, settings=build_settings()) == {}
    assert "reason=launcher_missing" in caplog.text


@pytest.mark.usefixtures("launcher")
def test_a_declared_web_mcp_replaces_the_default_and_loads_by_default() -> None:
    mine = MCPServerSpec(
        name="search",
        transport="stdio",
        command="clio-kit",
        args=("mcp-server", "web", "--remote-url", "http://mine:8089"),
        source="user",
    )
    other = MCPServerSpec(name="hdf5", transport="stdio", command="clio-kit", source="user")
    specs = with_default_web_mcp({"search": mine, "hdf5": other}, settings=build_settings())
    assert set(specs) == {"search", "hdf5"}
    assert specs["search"].always_load is True
    assert specs["search"].args == mine.args
    assert specs["hdf5"].always_load is False


@pytest.mark.usefixtures("launcher")
def test_another_server_named_web_is_kept_as_declared() -> None:
    http = MCPServerSpec(name="web", transport="http", url="http://x/mcp", source="user")
    assert with_default_web_mcp({"web": http}, settings=build_settings()) == {"web": http}


def test_every_web_mcp_spawn_gets_a_writable_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path))
    state = str(web_mcp_default.web_mcp_state_dir())
    assert state.startswith(str(tmp_path))
    routed = stdio_environment({}, command="clio-kit", args=["mcp-server", "web"])
    assert routed["WEB_STATE_DIR"] == state
    explicit = stdio_environment(
        {"WEB_REMOTE_URL": "http://mine:8089"}, command="clio-kit", args=["mcp-server", "web"]
    )
    assert explicit["WEB_STATE_DIR"] == state
    own = stdio_environment(
        {"WEB_STATE_DIR": "/own"}, command="clio-kit", args=["mcp-server", "web"]
    )
    assert own["WEB_STATE_DIR"] == "/own"
    monkeypatch.delenv("WEB_STATE_DIR", raising=False)
    other = stdio_environment({}, command="clio-kit", args=["mcp-server", "hdf5"])
    assert "WEB_STATE_DIR" not in other
