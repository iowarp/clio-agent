"""Connect-mode pin for execution clients (#1186).

Era negotiation is timing-sensitive: a modern server whose first response
outlives the per-RPC setup window kills the connect with -32022 even though
both peers speak 2026-07-28. ``tools.mcp.connect_mode`` pins the version for
uniformly-modern fleets (adopt directly, no probe); ``auto`` keeps SDK
negotiation. These lock the knob's resolution and its default OFF state.
"""

from __future__ import annotations

from typing import Any

from clio_agent import conf
from clio_agent.tools.mcp_runtime import make_mcp_client


class _CapturingClient:
    """Stands in for fastmcp.Client; records constructor kwargs."""

    def __init__(self, target: Any, **kwargs: Any) -> None:
        self.target = target
        self.kwargs = kwargs


def _make(monkeypatch, tmp_path, yaml_body: str | None) -> _CapturingClient:
    monkeypatch.chdir(tmp_path)
    if yaml_body is not None:
        config_dir = tmp_path / ".clio"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(yaml_body, encoding="utf-8")
    conf.reload()
    client = make_mcp_client(object(), client_cls=_CapturingClient)
    assert isinstance(client, _CapturingClient)
    return client


def test_default_leaves_sdk_auto_negotiation(monkeypatch, tmp_path):
    """No config -> no ``mode`` kwarg: the SDK's auto negotiation stays in charge."""
    client = _make(monkeypatch, tmp_path, None)
    assert "mode" not in client.kwargs


def test_config_file_pins_connect_mode(monkeypatch, tmp_path):
    """A file-pinned version reaches the client as ``mode`` verbatim."""
    client = _make(
        monkeypatch,
        tmp_path,
        "tools:\n  mcp:\n    connect_mode: '2026-07-28'\n",
    )
    assert client.kwargs["mode"] == "2026-07-28"


def test_env_pins_connect_mode(monkeypatch, tmp_path):
    """The env override works when no file layer names the key."""
    monkeypatch.setenv("CLIO_MCP_CONNECT_MODE", "2026-07-28")
    client = _make(monkeypatch, tmp_path, None)
    assert client.kwargs["mode"] == "2026-07-28"


def test_explicit_auto_is_not_forwarded(monkeypatch, tmp_path):
    """``auto`` spelled out means the same as unset — never passed through."""
    client = _make(
        monkeypatch,
        tmp_path,
        "tools:\n  mcp:\n    connect_mode: auto\n",
    )
    assert "mode" not in client.kwargs


class _FakeAsyncClient:
    """Minimal async-context client so executor setup succeeds without a server."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def list_tools(self):
        return []


def test_call_timeout_resolves_through_config(monkeypatch, tmp_path):
    """tools.mcp.call_timeout_s reaches the sync executor when no explicit timeout is passed."""
    from clio_agent.tools.execution import create_sync_tool_executor

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".clio"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "tools:\n  mcp:\n    call_timeout_s: 120\n",
        encoding="utf-8",
    )
    conf.reload()
    executor = create_sync_tool_executor(object(), client_factory=lambda _t: _FakeAsyncClient())
    assert executor._timeout == 120.0


def test_explicit_call_timeout_wins_over_config(monkeypatch, tmp_path):
    """An explicit timeout argument is never overridden by config."""
    from clio_agent.tools.execution import create_sync_tool_executor

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".clio"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "tools:\n  mcp:\n    call_timeout_s: 120\n",
        encoding="utf-8",
    )
    conf.reload()
    executor = create_sync_tool_executor(object(), timeout=7.0, client_factory=lambda _t: _FakeAsyncClient())
    assert executor._timeout == 7.0
