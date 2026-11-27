"""A declared stdio server mounts even when the workspace root was deleted, and a server
that still cannot mount says why where its tools are missing (dry 17: relay and ParaView
never mounted because the conversation's folder did not exist yet, so the agent fell
back to the shell)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from clio_agent.tools import mount_failures
from clio_agent.tools.gateway import build_gateway, namespace_specs
from clio_agent.tools.mcp_config import MCPSpawnError, spec_from_declaration


@pytest.fixture(autouse=True)
def _fresh_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mount_failures, "_FAILED", {})


def _spec(name: str = "compute") -> Any:
    return spec_from_declaration(name, {"command": sys.executable, "args": ["-c", "pass"]})


def test_a_deleted_workspace_root_is_recreated_before_its_servers_mount(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "wing"
    gateway = build_gateway({"compute": _spec()}, cwd=str(root))
    assert root.is_dir()
    assert "compute" in namespace_specs(gateway)
    assert mount_failures.failures(str(root)) == {}


def test_a_root_with_only_http_servers_is_not_created(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    http = spec_from_declaration("remote", "https://mcp.example.invalid/mcp")
    build_gateway({"remote": http}, cwd=str(root))
    assert not root.exists()


def test_a_server_that_cannot_mount_is_recorded_and_named_for_the_agent(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    root = blocker / "workspace"  # under a file: cannot be created

    gateway = build_gateway({"compute": _spec()}, cwd=str(root))

    assert "compute" not in namespace_specs(gateway)
    why = mount_failures.failures(str(root))["compute"]
    assert "does not exist" in why
    said = mount_failures.note(str(root), ["compute_describe"])
    assert said.startswith("The compute server is in this conversation but did not start")
    assert why in said
    assert mount_failures.note(str(root), ["COMPUTE"]) == said
    assert mount_failures.note(str(root), ["computer_x", "other"]) == ""
    assert mount_failures.note(str(tmp_path / "elsewhere"), ["compute"]) == ""


def test_a_later_mount_clears_the_refusal(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    calls: list[str | None] = []

    def factory(spec: Any, cwd: str | None) -> Any:
        calls.append(cwd)
        if len(calls) == 1:
            raise MCPSpawnError("launcher went missing")
        from fastmcp import FastMCP

        return FastMCP(spec.name)

    build_gateway({"compute": _spec()}, cwd=str(root), proxy_factory=factory)
    assert mount_failures.failures(str(root)) == {"compute": "launcher went missing"}
    build_gateway({"compute": _spec()}, cwd=str(root), proxy_factory=factory)
    assert mount_failures.failures(str(root)) == {}


def test_the_search_view_shows_a_server_that_did_not_start() -> None:
    from clio_agent.gact.search_tools import _present

    view = _present(
        {"query": "select:compute_run"},
        None,
        {
            "query": "select:compute_run",
            "tools": [],
            "missing": ["compute_run"],
            "deferred_total": 3,
            "not_started": "The compute server is in this conversation but did not start.",
        },
    )
    block = next(b for b in view["blocks"] if b["id"] == "not-started")
    assert block["severity"] == "warning"
    assert "did not start" in block["text"]
