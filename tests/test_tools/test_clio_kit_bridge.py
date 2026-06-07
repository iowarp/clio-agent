"""Tests for the shared clio-kit bridge (transport resolution + tool calls)."""

from __future__ import annotations

import pytest

from clio_agent.tools import clio_kit_bridge as bridge


def test_transport_prefers_local_checkout(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "clio-kit"
    checkout.mkdir()
    monkeypatch.setenv("CLIO_KIT_PATH", str(checkout))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)

    transport = bridge.clio_kit_transport("geo")

    assert transport.command == "uv"
    assert transport.args == [
        "--directory",
        str(checkout.resolve()),
        "run",
        "clio-kit",
        "mcp-server",
        "geo",
    ]


def test_transport_uses_explicit_command(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("CLIO_KIT_COMMAND", "uv --directory /opt/clio-kit run clio-kit")

    transport = bridge.clio_kit_transport("geo")

    assert transport.command == "uv"
    assert transport.args[-2:] == ["mcp-server", "geo"]


def test_transport_uses_path_command(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda name: "/usr/bin/clio-kit")

    transport = bridge.clio_kit_transport("geo")

    assert transport.command == "/usr/bin/clio-kit"
    assert transport.args == ["mcp-server", "geo"]


def test_transport_falls_back_to_uvx(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)

    transport = bridge.clio_kit_transport("geo")

    assert transport.command == "uvx"
    assert transport.args == ["--from", "clio-kit", "clio-kit", "mcp-server", "geo"]


def test_launcher_source_reports_local(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "clio-kit"
    checkout.mkdir()
    monkeypatch.setenv("CLIO_KIT_PATH", str(checkout))
    assert bridge.clio_kit_launcher_source() == "local_path"


def test_launcher_source_empty_when_unavailable(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.delenv("CLIO_KIT_ALLOW_UVX", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    assert bridge.clio_kit_launcher_source() == ""


def test_decode_tool_result_from_data() -> None:
    class R:
        data = {"status": "success"}

    assert bridge.decode_tool_result(R()) == {"status": "success"}


def test_decode_tool_result_from_text() -> None:
    class Part:
        text = '{"status": "ok"}'

    class R:
        data = None
        structured_content = None
        content = [Part()]

    assert bridge.decode_tool_result(R()) == {"status": "ok"}


@pytest.mark.asyncio
async def test_call_returns_error_when_launch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport that cannot start must yield a structured error, not raise."""

    def boom(_server: str):
        raise RuntimeError("no clio-kit")

    monkeypatch.setattr(bridge, "clio_kit_transport", boom)
    result = await bridge.call_clio_kit_tool("geo", "render_feature_map", {})
    assert result["code"] == "clio_kit_unavailable"
    assert "render_feature_map" in result["error"]
