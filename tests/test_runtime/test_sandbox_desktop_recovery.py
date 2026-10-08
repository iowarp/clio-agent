"""Recover a Desktop fence receipt without dropping connected-source protection."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import sandbox, sandbox_cli, sandbox_codex
from clio_agent.runtime.sandbox_codex_profile import synthesize_codex_profile
from clio_agent.runtime.storage_access import require_storage_fence
from clio_agent.tools import desktop_mcp_runtime


def detected() -> sandbox_codex.CodexDetection:
    """Use an injected bundle detection; never launch a machine setup from tests."""
    return sandbox_codex.CodexDetection(
        installed=True,
        binary_path="bundled/codex.exe",
        version="0.157.1",
        reason=sandbox_codex.REASON_CODEX_DETECTED,
        source=sandbox_codex.CODEX_SOURCE_BUNDLED,
    )


@pytest.mark.parametrize("enforced", [True, False], ids=["verified", "refused"])
def test_existing_accounts_recover_only_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforced: bool
) -> None:
    """Real marker/profile/guard integration: existing accounts alone never unblock tools."""
    home = tmp_path / "desktop" / "data" / "clio-agent"
    monkeypatch.setenv("CLIO_AGENT_HOME", str(home))
    private = home / "config" / "storage-auth"
    private.mkdir(parents=True)
    (private / "credentials.json").write_text("test-only", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_provisioned",
        lambda **_kwargs: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )
    before = sandbox._resolve_backend(platform="win32", codex_detection=detected())
    assert not before.active
    with pytest.raises(PermissionError, match="Set up protected execution"):
        require_storage_fence(before.mechanism, before.active, (home,))
    calls: list[str] = []

    def no_elevation(_binary: str) -> tuple[bool, str]:
        raise AssertionError("Existing accounts must not repeat UAC/account creation")

    def grant() -> list[dict[str, Any]]:
        calls.append("grant")
        return [{"grant": "bundled_runtime", "status": "granted"}]

    def verify(_binary: str, _root: str, *, platform: str) -> tuple[bool, str]:
        assert platform == "win32"
        calls.append("verify")
        return enforced, (
            sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED
            if enforced
            else sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED
        )

    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        detection=detected(),
        elevator=no_elevation,
        grantor=grant,
        verifier=verify,
        helper_preparer=lambda _binary, _version: {"status": "available"},
    )
    assert calls == ["grant", "verify"]
    assert result.elevated is False
    assert result.ok is enforced
    marker = sandbox_codex._read_codex_marker()
    assert marker and marker["enforcement_verified"] is enforced
    after = sandbox._resolve_backend(platform="win32", codex_detection=detected())
    assert after.active is enforced
    if enforced:
        require_storage_fence(after.mechanism, after.active, (home,))
        assert synthesize_codex_profile([home])["filesystem"][str(private.resolve())] == "deny"
        assert "without UAC" in result.detail
    else:
        with pytest.raises(PermissionError, match="Set up protected execution"):
            require_storage_fence(after.mechanism, after.active, (home,))


def test_bundled_grants_include_runtime_without_private_desktop_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle's Python/Node/MCP tree is executable; adjacent credentials are not granted."""
    desktop = tmp_path / "CLIO Desktop"
    bundle = desktop / "data" / "bundled-runtime" / "gact-runtime"
    bundle.mkdir(parents=True)
    private = desktop / "data" / "clio-agent" / "config" / "storage-auth"
    private.mkdir(parents=True)
    monkeypatch.setattr(desktop_mcp_runtime, "resolve_bundled_runtime_root", lambda: bundle)
    plan = sandbox_cli.build_fleet_runtime_grant_plan()
    grant = next(item for item in plan if item.label == "bundled_runtime")
    assert Path(grant.path) == bundle
    assert grant.inherit and grant.exists
    assert grant.users == tuple(sandbox_cli.CODEX_SANDBOX_USERS)
    assert not private.is_relative_to(Path(grant.path))
    assert all(Path(item.path) != desktop and Path(item.path) != desktop / "data" for item in plan)


@pytest.mark.parametrize(
    ("started", "escaped", "returncode", "expected"),
    [
        (True, False, 5, sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED),
        (False, False, 1, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        (False, False, 0, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        (True, False, 0, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        (True, True, 0, sandbox_codex.REASON_CODEX_ENFORCEMENT_ESCAPED),
        (True, True, 5, sandbox_codex.REASON_CODEX_ENFORCEMENT_ESCAPED),
    ],
    ids=["denied", "launcher-error", "no-child", "no-denial", "escaped", "escaped-error"],
)
def test_probe_requires_child_write_and_outside_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started: bool,
    escaped: bool,
    returncode: int,
    expected: str,
) -> None:
    """An absent outside file never certifies a broken launcher as a working fence."""
    parent = tmp_path / "Temp with spaces"
    parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(parent))
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "isolated-agent"))
    monkeypatch.setattr(sandbox_codex, "write_codex_layer", lambda *_args, **_kwargs: "test-layer")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == sandbox_codex._CODEX_PROBE_TIMEOUT_S
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        allow = Path(argv[argv.index("-C") + 1])
        assert allow.parent == parent
        assert argv[-4:-1] == ["cmd", "/d", "/c"]
        targets = argv[-1].split(" && ")
        assert targets[0] == "type nul > started.txt"
        relative = targets[1].removeprefix("type nul > ")
        assert relative.startswith("..\\clio-agent-codex-out-")
        outside = (allow / relative.replace("\\", "/")).resolve()
        assert outside.parent.is_dir()
        if started:
            (allow / "started.txt").write_text("", encoding="utf-8")
        if escaped:
            outside.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="profile failure")

    monkeypatch.setattr(subprocess, "run", run)
    ok, reason = sandbox_codex._run_codex_enforcement_probe("test-codex", str(tmp_path))
    assert reason == expected
    assert ok is (expected == sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED)
