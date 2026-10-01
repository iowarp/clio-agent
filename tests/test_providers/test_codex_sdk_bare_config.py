"""The Codex SDK runtime runs as a bare model: none of the user's own MCP servers or
plugins load.

Found live (2026-09-29, opal): every turn failed on a hidden ``mcpToolCall`` --
Codex merges ``-c`` overrides into the user's ``config.toml``, so
``mcp_servers={}`` left every declared server in place. Each declared server and
plugin is switched off by name instead (``<table>.<name>.enabled=false``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.providers.codex import sdk_client
from clio_agent.providers.codex.errors import CodexSDKError

CONFIG = """
model = "gpt-6-sol"

[mcp_servers.pdf2md-service]
command = "pdf2md"
enabled = false

[mcp_servers."21st"]
url = "https://example.com/mcp"

[mcp_servers.node_repl.env]
X = "1"

[plugins."browser@openai-bundled"]
enabled = true

[plugins."computer-use@openai-bundled"]
enabled = true
"""


def _home(tmp_path: Path, text: str) -> dict[str, str]:
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    return {"CODEX_HOME": str(tmp_path)}


def test_every_declared_server_and_plugin_is_switched_off(tmp_path: Path) -> None:
    overrides = sdk_client.user_config_disables(_home(tmp_path, CONFIG))

    # Codex's ``-c`` splits the key path on dots and keeps quotes as part of a name
    # (found live: a quoted key became a new, transport-less server), so names ride
    # bare.
    assert set(overrides) == {
        "mcp_servers.pdf2md-service.enabled=false",
        "mcp_servers.21st.enabled=false",
        "mcp_servers.node_repl.enabled=false",
        "plugins.browser@openai-bundled.enabled=false",
        "plugins.computer-use@openai-bundled.enabled=false",
    }


def test_a_name_codex_cannot_address_is_a_typed_failure(tmp_path: Path) -> None:
    with pytest.raises(CodexSDKError, match="a.b"):
        sdk_client.user_config_disables(_home(tmp_path, '[mcp_servers."a.b"]\ncommand = "x"\n'))


def test_no_user_config_needs_nothing_switched_off(tmp_path: Path) -> None:
    assert sdk_client.user_config_disables({"CODEX_HOME": str(tmp_path)}) == ()


def test_an_unreadable_config_is_a_typed_failure(tmp_path: Path) -> None:
    """A runtime we cannot make bare must not run: its hidden actions would reach
    the user as failed turns."""
    with pytest.raises(CodexSDKError, match="config.toml"):
        sdk_client.user_config_disables(_home(tmp_path, "[mcp_servers\nbroken"))


def test_the_runtime_is_launched_with_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _home(tmp_path, CONFIG).items():
        monkeypatch.setenv(key, value)
    overrides = sdk_client.bare_lm_config_overrides()
    assert overrides[: len(sdk_client.BARE_LM_CONFIG_OVERRIDES)] == (
        sdk_client.BARE_LM_CONFIG_OVERRIDES
    )
    assert "plugins.computer-use@openai-bundled.enabled=false" in overrides


def test_every_thread_switches_them_off_too(tmp_path: Path) -> None:
    """Found live (exp67 benchmark, 2026-10-01): a thread's ``config`` is re-derived from
    config.toml, so the runtime-level ``-c`` disables never reached it -- every thread had
    the user's servers live (21st, node_repl, shadcn ...), Codex added list_mcp_resources
    and every turn hit a hidden mcpToolCall. The thread config carries the same disables."""
    config = sdk_client.bare_lm_thread_config(_home(tmp_path, CONFIG))

    assert config["mcp_servers"] == {
        "pdf2md-service": {"enabled": False},
        "21st": {"enabled": False},
        "node_repl": {"enabled": False},
    }
    assert config["plugins"] == {
        "browser@openai-bundled": {"enabled": False},
        "computer-use@openai-bundled": {"enabled": False},
    }
    assert config["features"] == sdk_client.BARE_LM_FEATURES
    assert config["web_search"] == "disabled"
    assert config["model_auto_compact_token_limit"] == sdk_client.NO_AUTO_COMPACT_TOKEN_LIMIT


def test_a_thread_config_for_no_user_config_switches_nothing_by_name(tmp_path: Path) -> None:
    config = sdk_client.bare_lm_thread_config({"CODEX_HOME": str(tmp_path)})

    assert config["mcp_servers"] == {}
    assert config["plugins"] == {}


def test_the_plugin_system_is_off() -> None:
    assert "features.plugins=false" in sdk_client.BARE_LM_CONFIG_OVERRIDES
    assert sdk_client.BARE_LM_FEATURES["plugins"] is False
