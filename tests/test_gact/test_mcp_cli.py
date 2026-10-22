"""``clio-agent mcp`` and ``GET /v1/mcp/configuration`` read and write the saved services."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_user_configuration import get_user_mcp_server
from clio_agent.ui.mcp_cli import run_mcp_cli


@pytest.fixture
def user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    return tmp_path / "user"


def test_add_list_and_remove_a_local_and_a_remote_service(
    user_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run_mcp_cli(["add", "colab", "--", "uvx", "git+https://github.com/googlecolab/colab-mcp"])
        == 0
    )
    assert get_user_mcp_server("colab") == {
        "transport": "stdio",
        "command": "uvx",
        "args": ["git+https://github.com/googlecolab/colab-mcp"],
        "env": {},
        "always_load": True,
    }
    assert (
        run_mcp_cli(["add", "docs", "--url", "https://mcp.example.org", "--only-when-asked"]) == 0
    )
    assert get_user_mcp_server("docs") == {
        "transport": "http",
        "url": "https://mcp.example.org",
        "always_load": False,
    }
    capsys.readouterr()
    run_mcp_cli(["list"])
    listed = capsys.readouterr().out
    assert "colab  stdio  uvx git+https://github.com/googlecolab/colab-mcp" in listed
    assert "docs  http  https://mcp.example.org" in listed
    assert run_mcp_cli(["remove", "colab"]) == 0
    assert get_user_mcp_server("colab") is None
    assert run_mcp_cli(["remove", "colab"]) == 1


def test_a_bad_declaration_is_refused(user_dir: Path) -> None:
    with pytest.raises(SystemExit, match="NAME=VALUE"):
        run_mcp_cli(["add", "x", "--env", "NOPE", "--", "python"])
    with pytest.raises(SystemExit, match="not both"):
        run_mcp_cli(["add", "x", "--url", "https://a", "--", "python"])


def test_saved_services_are_listed_without_starting_them(user_dir: Path, tmp_path: Path) -> None:
    run_mcp_cli(["add", "colab", "--env", "TOKEN=secret", "--", "uvx", "colab-mcp"])
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        rows = client.get("/v1/mcp/configuration").json()["servers"]
    (row,) = rows
    assert (row["name"], row["transport"], row["always_load"]) == ("colab", "stdio", True)
    assert row["spec"]["env"]["TOKEN"] != "secret"
