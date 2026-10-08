"""Verify missing wheel helpers, exact release alignment, and safe failed hydration."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.runtime import codex_desktop_setup, document_install, sandbox_cli, sandbox_codex
from clio_agent.runtime import codex_windows_helpers as helpers


def pe_binary(path: Path, machine: int = 0x8664) -> Path:
    """Create a minimal test-only PE header for executable architecture detection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"MZ" + bytes(58) + (64).to_bytes(4, "little") + b"PE\0\0" + machine.to_bytes(2, "little")
    )
    return path


def mock_release(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.161.0",
    target: str = "x86_64",
    defect: str = "",
) -> list[str]:
    """Serve test release metadata and bytes through the actual HTTP and hash code."""
    calls: list[str] = []
    payloads = {name: f"test-only {version} {name}".encode() for name in helpers._HELPERS}
    assets = [
        {
            "name": f"{name}-{target}-pc-windows-msvc.exe",
            "size": len(data),
            "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
            "browser_download_url": f"https://github.com/openai/codex/releases/download/rust-v{version}/{name}-{target}-pc-windows-msvc.exe",
        }
        for name, data in payloads.items()
    ]
    if defect == "missing":
        assets.pop()
    elif defect == "digest":
        assets[0]["digest"] = "sha256:unverified"
    elif defect == "url":
        assets[0]["browser_download_url"] = "https://example.test/helper.exe"
    elif defect == "oversize":
        assets[0]["size"] = helpers._MAX_BYTES + 1

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tag_name": "rust-v0.1.0" if defect == "tag" else f"rust-v{version}",
                    "assets": assets,
                },
            )
        name = next(
            name
            for name in helpers._HELPERS
            if request.url.path.rsplit("/", 1)[-1].startswith(name)
        )
        body = payloads[name]
        if defect == "corrupt-second" and name == helpers._HELPERS[1]:
            body = b"X" * len(body)
        return httpx.Response(200, content=body)

    original = httpx.Client
    monkeypatch.setattr(
        helpers.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return calls


@pytest.mark.parametrize(
    "machine,target", [(0x8664, "x86_64"), (0xAA64, "aarch64")], ids=["x64", "arm64"]
)
def test_missing_helpers_match_executable_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine: int, target: str
) -> None:
    binary = pe_binary(tmp_path / "runtime with spaces" / "codex.exe", machine)
    calls = mock_release(monkeypatch, target=target)
    result = helpers.ensure_codex_windows_helpers(binary, "0.161.0", platform_name="win32")
    assert result["version"] == "0.161.0"
    assert result["target"] == f"{target}-pc-windows-msvc"
    assert len(calls) == 3
    assert all((binary.parent / f"{name}.exe").is_file() for name in helpers._HELPERS)
    helpers.ensure_codex_windows_helpers(binary, "0.161.0", platform_name="win32")
    assert len(calls) == 3  # Verified cached bytes work without network.
    (binary.parent / "codex-command-runner.exe").write_bytes(b"corrupt")
    helpers.ensure_codex_windows_helpers(binary, "0.161.0", platform_name="win32")
    assert len(calls) == 6
    assert b"corrupt" != (binary.parent / "codex-command-runner.exe").read_bytes()


def test_new_client_does_not_reuse_previous_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = pe_binary(tmp_path / "codex.exe")
    mock_release(monkeypatch, version="0.157.1")
    helpers.ensure_codex_windows_helpers(binary, "0.157.1", platform_name="win32")
    # Restore Client before installing another transport for the newer release.
    monkeypatch.undo()
    calls = mock_release(monkeypatch, version="0.161.0")
    helpers.ensure_codex_windows_helpers(binary, "0.161.0", platform_name="win32")
    assert len(calls) == 3
    assert b"0.161.0" in (binary.parent / "codex-command-runner.exe").read_bytes()


@pytest.mark.parametrize(
    "defect", ["tag", "missing", "digest", "url", "oversize", "corrupt-second"]
)
def test_failed_download_preserves_previous_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    binary = pe_binary(tmp_path / "codex.exe")
    for name in helpers._HELPERS:
        (tmp_path / f"{name}.exe").write_bytes(b"previous verified installation")
    receipt = tmp_path / helpers._RECEIPT
    receipt.write_text("previous receipt", encoding="utf-8")
    mock_release(monkeypatch, defect=defect)
    with pytest.raises(helpers.CodexWindowsHelpersError):
        helpers.ensure_codex_windows_helpers(binary, "0.161.0", platform_name="win32")
    assert all(
        (tmp_path / f"{name}.exe").read_bytes() == b"previous verified installation"
        for name in helpers._HELPERS
    )
    assert receipt.read_text() == "previous receipt"
    assert not list(tmp_path.glob(".clio-helpers-*"))


def test_helper_failure_never_elevates_or_records_enforcement(tmp_path: Path) -> None:
    calls: list[str] = []

    def failed(_binary: Path, _version: str) -> dict[str, Any]:
        raise helpers.CodexWindowsHelpersError("matching helper unavailable")

    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        detection=sandbox_codex.CodexDetection(
            True,
            str(tmp_path / "codex.exe"),
            "0.161.0",
            sandbox_codex.REASON_CODEX_DETECTED,
            source=sandbox_codex.CODEX_SOURCE_BUNDLED,
        ),
        helper_preparer=failed,
        elevator=lambda _binary: calls.append("elevate"),
        verifier=lambda *_args, **_kwargs: calls.append("verify"),
        marker_writer=lambda *_args, **_kwargs: calls.append("mark"),
    )
    assert not result.ok and not result.elevated
    assert result.reason == "codex_windows_helpers_unavailable"
    assert not calls


def test_installer_never_records_incomplete_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result: dict[str, Any] = {
        "runtime_id": "locked",
        "javascript": {"status": "ready"},
        "native_tools": {},
        "capabilities": {},
    }
    monkeypatch.setattr(document_install, "prepare_document_runtime", lambda *_a, **_kw: result)
    monkeypatch.setattr(document_install, "prepare_office_runtime", lambda: "test office")
    monkeypatch.setattr(document_install, "ensure_github_cli", lambda: tmp_path / "gh")

    def failed() -> dict[str, Any]:
        raise helpers.CodexWindowsHelpersError("matching helper unavailable")

    monkeypatch.setattr(document_install, "ensure_bundled_codex_windows_helpers", failed)
    with pytest.raises(helpers.CodexWindowsHelpersError, match="matching helper"):
        document_install.install_document_runtime(
            tmp_path / "workspace", cache_root=tmp_path / "cache"
        )
    assert not (tmp_path / "cache" / "locked" / "installed.json").exists()


def test_install_never_creates_accounts_implicitly() -> None:
    calls: list[str] = []
    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        allow_elevation=False,
        detection=sandbox_codex.CodexDetection(
            True, "test-codex", "0.157.1", sandbox_codex.REASON_CODEX_DETECTED
        ),
        gate=lambda **_kwargs: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
        elevator=lambda _binary: calls.append("elevate"),
        verifier=lambda *_args, **_kwargs: calls.append("verify"),
    )
    assert result.reason == sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED
    assert not result.ok and not result.elevated and not calls


@pytest.mark.parametrize("enforced", [True, False], ids=["verified", "refused"])
def test_installer_recovers_existing_fence_only_after_proof(
    monkeypatch: pytest.MonkeyPatch, enforced: bool
) -> None:
    monkeypatch.setattr(codex_desktop_setup.sys, "platform", "win32")

    def provision(*, allow_elevation: bool) -> sandbox_cli.CodexProvisionResult:
        assert allow_elevation is False
        return sandbox_cli.CodexProvisionResult(
            ok=enforced,
            status="test",
            reason="verified" if enforced else "unverified",
            detail="real fence probe verdict",
        )

    monkeypatch.setattr(sandbox_cli, "provision_codex_windows", provision)
    if enforced:
        assert codex_desktop_setup.prepare_existing_windows_fence()["status"] == "available"
    else:
        with pytest.raises(document_install.DocumentRuntimeError, match="verification failed"):
            codex_desktop_setup.prepare_existing_windows_fence()


def test_client_update_rejects_missing_windows_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.components import verify

    monkeypatch.setitem(sys.modules, "codex_cli_bin", object())

    def failed() -> dict[str, Any]:
        raise helpers.CodexWindowsHelpersError("matching helper unavailable")

    monkeypatch.setattr(helpers, "ensure_bundled_codex_windows_helpers", failed)
    result = verify.verify_codex()
    assert result["ok"] is False
    assert result["code"] == "codex_windows_helpers_unavailable"


def test_non_windows_install_never_downloads_helpers() -> None:
    assert helpers.ensure_codex_windows_helpers(
        Path("missing"), "invalid", platform_name="linux"
    ) == {"status": "not_required"}
