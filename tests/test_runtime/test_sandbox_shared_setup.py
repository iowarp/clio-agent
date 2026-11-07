"""Startup, button and CLI use one engine and one validated runtime-access cache."""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.sandbox_setup import run_sandbox_setup
from clio_agent.runtime import codex_desktop_access as access
from clio_agent.runtime import codex_desktop_setup, sandbox, sandbox_cli, sandbox_codex


def test_entrypoints_share_one_grant_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real cache/engine integration repairs once and reuses across all entrypoints."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(sandbox, "_STATE", None)
    grant = sandbox_cli.FleetGrant("runtime", str(runtime), ("offline", "online"), True, True)
    monkeypatch.setattr(access.paths, "user_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(access, "build_fleet_runtime_grant_plan", lambda: [grant])
    traversals: list[list[str]] = []

    def icacls(argv: list[str]) -> tuple[int, str]:
        if len(argv) > 2:
            traversals.append(argv)
        return 0, "unchanged root ACL"

    monkeypatch.setattr(access, "_run_icacls", icacls)
    monkeypatch.setattr(sandbox_cli, "_run_icacls", icacls)
    monkeypatch.setattr(
        sandbox_codex,
        "detect_codex",
        lambda **kwargs: sandbox_codex.CodexDetection(
            True, "test-codex", "0.145.0", sandbox_codex.REASON_CODEX_DETECTED
        ),
    )
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **kwargs: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )
    monkeypatch.setattr(codex_desktop_setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(sandbox_cli, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    monkeypatch.setattr(
        sandbox_cli,
        "provision_codex_windows",
        functools.partial(sandbox_cli.provision_codex_windows, platform="win32"),
    )
    assert codex_desktop_setup.prepare_existing_windows_fence()["status"] == "available"
    result = run_sandbox_setup(platform="win32")
    assert result.status == sandbox_cli.OUTCOME_ALREADY_PROVISIONED
    assert sandbox_cli.run_sandbox_cli("setup", json_output=True) == 0
    assert len(traversals) == 1
    assert any(argument.startswith("offline:") for argument in traversals[0])
    assert any(argument.startswith("online:") for argument in traversals[0])
    assert "ready" in capsys.readouterr().err


@pytest.mark.parametrize("allow_elevation", [False, True])
def test_elevation_permission_does_not_force_native_requalification(allow_elevation: bool) -> None:
    """Allowing first-time account creation must not discard a valid native receipt."""

    def native(*args: object, **kwargs: object) -> dict[str, str]:
        # An explicit setup may re-probe a cached failure, never discard a valid proof.
        assert kwargs.get("force", False) is False
        assert kwargs["retry_unavailable"] is allow_elevation
        return {"status": "verified", "reason": "cached_native_proof"}

    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        allow_elevation=allow_elevation,
        detection=sandbox_codex.CodexDetection(
            True, "test-codex", "0.167.0", sandbox_codex.REASON_CODEX_DETECTED
        ),
        mxc_preparer=native,
    )
    assert result.ok and not result.elevated


def test_failed_runtime_grants_are_failed_for_every_caller() -> None:
    """A previously valid fence proof must not conceal failed executable access."""
    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        detection=sandbox_codex.CodexDetection(
            True, "test-codex", "0.145.0", sandbox_codex.REASON_CODEX_DETECTED
        ),
        gate=lambda **kwargs: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
        grantor=lambda: [{"status": "failed", "grant": "runtime"}],
    )
    assert not result.ok
    assert result.reason == "fleet_runtime_access_failed"
