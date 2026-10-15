"""Default web declarations must use the preinstalled Desktop adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.tools import desktop_mcp_runtime


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Model a bundle with both installed launch modules, without starting children."""
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(desktop_mcp_runtime, "resolve_bundled_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(
        desktop_mcp_runtime.importlib.util, "find_spec", lambda _name: SimpleNamespace()
    )
    return tmp_path


def test_default_clio_kit_web_uses_the_preinstalled_adapter(bundled: Path) -> None:
    result = desktop_mcp_runtime.bundled_module_launcher("clio-kit", ["mcp-server", "web"])
    assert result is not None
    assert result[1] == ["-c", "from web_mcp.server import main; main()"]


def test_web_provider_arguments_are_forwarded_without_launcher_arguments(bundled: Path) -> None:
    result = desktop_mcp_runtime.bundled_module_launcher(
        "clio-kit", ["mcp-server", "web", "--remote-url", "http://example.test"]
    )
    assert result is not None
    assert result[1] == [
        "-c",
        "from web_mcp.server import main; main()",
        "--remote-url",
        "http://example.test",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["mcp-server", "web", "--branch", "custom"],
        ["mcp-server", "web", "--branch=custom"],
        ["mcp-server", "web", "-b", "custom"],
        ["mcp-server", "web", "-bcustom"],
        ["mcp-server", "fs"],
    ],
)
def test_explicit_branches_and_other_servers_keep_clio_kit(
    bundled: Path, arguments: list[str]
) -> None:
    result = desktop_mcp_runtime.bundled_module_launcher("clio-kit", arguments)
    assert result is not None
    assert result[1] == ["-c", "from clio_kit import cli; cli()", *arguments]


def test_missing_preinstalled_web_adapter_keeps_the_standard_launcher(
    bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        desktop_mcp_runtime.importlib.util,
        "find_spec",
        lambda name: None if name == "web_mcp" else SimpleNamespace(),
    )
    result = desktop_mcp_runtime.bundled_module_launcher("clio-kit", ["mcp-server", "web"])
    assert result is not None
    assert result[1] == ["-c", "from clio_kit import cli; cli()", "mcp-server", "web"]
