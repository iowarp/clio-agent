"""Pinned npm preparation and verified execution inside the CLIO environment."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import obsidian_client_runtime as runtime


@pytest.mark.parametrize("native_ok", [True, False])
def test_installer_creates_environment_and_requires_native_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_ok: bool
) -> None:
    """First install must create its workspace and never trust a failed native probe."""
    node_root = tmp_path / "node"
    npm = node_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm.parent.mkdir(parents=True)
    npm.write_text("// test-only npm fixture")
    node = node_root / "node.exe"
    node.write_text("test-only executable fixture")
    client = runtime.ManagedObsidianClient(tmp_path / "managed")
    progress: list[str] = []

    def prepare(workspace: Path) -> dict[str, str]:
        assert workspace.is_dir()
        return {"node": str(node)}

    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if "ci" in command:
            assert json.loads((client.project / "package.json").read_text())["dependencies"] == {
                "obsidian-headless": runtime.CLIENT_VERSION
            }
            assert (client.project / "package-lock.json").read_bytes() == (
                runtime.CLIENT_FILES / "package-lock.json"
            ).read_bytes()
            entry = client.project / "node_modules" / "obsidian-headless" / "cli.js"
            entry.parent.mkdir(parents=True)
            entry.write_text("// test-only official CLI fixture")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        assert "better-sqlite3" in command[-1]
        return subprocess.CompletedProcess(command, 0 if native_ok else 1, b"", b"")

    monkeypatch.setattr(runtime, "prepare_client_environment", prepare)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    if native_ok:
        client.install(progress.append)
        assert client.command() == [
            str(node),
            str(client.project / "node_modules" / "obsidian-headless" / "cli.js"),
        ]
        assert client.installed()
        manifest = client.project / "package.json"
        content = json.loads(manifest.read_text())
        manifest.write_text(json.dumps(content, indent=4) + "\n", encoding="utf-8")
        assert client.installed(), "Git or OS newline formatting does not change the pinned client"
        content["dependencies"]["obsidian-headless"] = "0.0.0"
        manifest.write_text(json.dumps(content), encoding="utf-8")
        assert not client.installed(), "A different dependency still requires repair"
    else:
        with pytest.raises(runtime.ObsidianClientError, match="could not start"):
            client.install(progress.append)
        assert not client.installed()
        assert not (client.project / "installed.json").exists()
    assert len(commands) == 2 and "--no-audit" in commands[0]


def test_missing_managed_receipt_never_falls_back_to_personal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing a system ob command does not make the managed client ready."""
    personal = tmp_path / "ob.exe"
    personal.write_text("test-only personal executable")
    monkeypatch.setenv("PATH", str(tmp_path))
    client = runtime.ManagedObsidianClient(tmp_path / "managed")
    with pytest.raises(runtime.ObsidianClientError, match="Install"):
        client.command()


def test_environment_preparation_scopes_only_the_installer_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparing a client must not grant the running agent access to runtime caches."""
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", "agent-workspace")
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        assert command[1:3] == ["-m", "clio_agent.runtime.obsidian_node"]
        return subprocess.CompletedProcess(command, 0, '{"node":"managed-node"}', "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.prepare_client_environment(tmp_path) == {"node": "managed-node"}
    assert calls[0]["env"]["CLIO_ALLOWED_ROOTS"] == str(tmp_path.resolve())
    assert os.environ["CLIO_ALLOWED_ROOTS"] == "agent-workspace"
