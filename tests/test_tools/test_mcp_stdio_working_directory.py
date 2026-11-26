"""A confined MCP must retain its directory independently of grant ordering."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import sandbox, sandbox_net
from clio_agent.tools.mcp_config import (
    MCPSpawnError,
    spec_from_declaration,
    transport_for,
    transport_from_spec,
)


def test_declared_stdio_rejects_empty_directory_before_transport_creation() -> None:
    """An explicit empty directory remains an invalid submission rather than ambient cwd."""
    spec = spec_from_declaration("owned", {"command": sys.executable, "args": ["-c", "pass"]})
    with pytest.raises(MCPSpawnError, match="working directory '' does not exist"):
        transport_for(spec, cwd="")


@pytest.fixture
def confined_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, tuple[Path, ...]]:
    """Compose the actual Codex fence around an absent first state grant."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing_state = tmp_path / "uncreated-state-directory"
    roots = (missing_state, workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "private-codex"))
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "clio-home"))
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"codex_binary": sys.executable, "codex_version": "0.162.1"},
    )
    monkeypatch.setattr(sandbox, "current_state", lambda: state)
    monkeypatch.setattr(sandbox, "effective_write_roots", lambda *args, **kwargs: roots)
    monkeypatch.setattr(sandbox_net, "open_child_egress", lambda *args, **kwargs: ("", None, {}))
    return workspace, roots


def assert_preserved_directory_and_grants(
    transport: Any, workspace: Path, roots: tuple[Path, ...]
) -> None:
    """Check actual composed argv and materialized permissions, without launching a child."""
    argv = list(transport.args)
    assert argv[argv.index("-C") + 1] == str(workspace)
    assert workspace.is_dir() and not roots[0].exists()
    assert argv[-4:] == ["--", sys.executable, "-c", "pass"]
    layer = argv[argv.index("-p") + 1]
    profile = tomllib.loads(
        (workspace.parent / "private-codex" / f"{layer}.config.toml").read_text()
    )
    grants = profile["permissions"]["clio"]["filesystem"]
    assert {path for path, mode in grants.items() if mode == "write"} == {
        str(root) for root in roots
    }


@pytest.mark.parametrize("mode", ["explicit", "inherited", "relative"])
def test_declared_stdio_preserves_directory_through_active_fence(
    confined_workspace: tuple[Path, tuple[Path, ...]],
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence, launcher and artifacts root must agree on the actual directory."""
    workspace, roots = confined_workspace
    cwd = str(workspace) if mode == "explicit" else None
    if mode == "relative":
        monkeypatch.chdir(workspace.parent)
        cwd = workspace.name
    spec = spec_from_declaration("owned", {"command": sys.executable, "args": ["-c", "pass"]})
    transport = transport_for(spec, cwd=cwd)
    assert_preserved_directory_and_grants(transport, workspace, roots)
    assert transport.cwd == (str(workspace) if cwd is not None else None)
    if cwd is not None:
        assert transport.env["CLIO_KIT_ARTIFACTS"] == str(workspace)


def test_configured_stdio_preserves_inherited_directory_through_active_fence(
    confined_workspace: tuple[Path, tuple[Path, ...]],
) -> None:
    """Saved stdio declarations retain inherited cwd under the same fence composer."""
    workspace, roots = confined_workspace
    transport = transport_from_spec(
        {"transport": "stdio", "command": sys.executable, "args": ["-c", "pass"]}
    )
    assert_preserved_directory_and_grants(transport, workspace, roots)


def test_a_fenced_server_routes_ssh_through_the_fence_helper(
    confined_workspace: tuple[Path, tuple[Path, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server's ssh, scp, sftp and Git over SSH leave the fence the way a shell's do."""
    import os

    from clio_agent.runtime import fence_ssh

    real_which = fence_ssh.shutil.which
    monkeypatch.setattr(
        fence_ssh.shutil,
        "which",
        lambda name, *, path=None: "/usr/bin/ssh" if name == "ssh" else real_which(name, path=path),
    )
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    spec = spec_from_declaration("owned", {"command": sys.executable, "args": ["-c", "pass"]})
    transport = transport_for(spec, cwd=str(confined_workspace[0]))
    shims = fence_ssh.shim_directory()
    assert shims is not None
    assert transport.env["PATH"].split(os.pathsep)[0] == str(shims)
    assert transport.env["GIT_SSH_COMMAND"] == fence_ssh.git_ssh_command(shims)
