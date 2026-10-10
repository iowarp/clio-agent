"""Desktop never certifies failed runtime grants or creates sandbox accounts implicitly."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime import codex_desktop_access, codex_desktop_setup, sandbox_cli
from clio_agent.runtime.document_runtime import DocumentRuntimeError


@pytest.mark.parametrize("state", ["available", "setup_required", "failed_grant", "unverified"])
@pytest.mark.parametrize("allow_elevation", [False, True])
def test_desktop_setup_keeps_the_existing_enforcement_gate(
    monkeypatch: pytest.MonkeyPatch, state: str, allow_elevation: bool
) -> None:
    monkeypatch.setattr(codex_desktop_setup, "sys", SimpleNamespace(platform="win32"))
    grants = [{"status": "failed" if state == "failed_grant" else "granted"}]
    calls: list[object] = []

    def access(**kwargs: object) -> list[dict[str, str]]:
        calls.append(kwargs)
        return grants

    def provision(**kwargs: Any) -> sandbox_cli.CodexProvisionResult:
        assert kwargs["allow_elevation"] is allow_elevation
        return sandbox_cli.CodexProvisionResult(
            ok=state not in {"setup_required", "unverified"},
            status="checked",
            reason="codex_windows_unprovisioned" if state == "setup_required" else "checked",
            detail="not verified",
            next_action="Set up protected execution",
            extra={"fleet_runtime_grants": kwargs["grantor"]()},
        )

    monkeypatch.setattr(codex_desktop_access, "ensure_desktop_runtime_access", access)
    monkeypatch.setattr(sandbox_cli, "provision_codex_windows", provision)
    if state in {"failed_grant", "unverified"}:
        with pytest.raises(DocumentRuntimeError, match="Protected execution verification failed"):
            codex_desktop_setup.prepare_existing_windows_fence(allow_elevation=allow_elevation)
    else:
        result = codex_desktop_setup.prepare_existing_windows_fence(allow_elevation=allow_elevation)
        assert result["status"] == state
    assert len(calls) == 1


def test_permission_command_timeout_is_an_explicit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: Any, **kwargs: Any) -> None:
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired("icacls", 120)

    monkeypatch.setattr(subprocess, "run", run)
    code, detail = sandbox_cli._run_icacls(["icacls", "runtime"])
    assert code == 1
    assert "TimeoutExpired" in detail
