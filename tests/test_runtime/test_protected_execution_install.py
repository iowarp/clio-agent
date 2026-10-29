"""The desktop may defer package preparation only after a real protection verdict."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import document_install as installer


@pytest.mark.parametrize("setup", [False, True])
def test_fence_only_never_installs_document_packages_or_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], setup: bool
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Protection-only startup must not prepare documents or open the store")

    def fence(**kwargs: Any) -> dict[str, str]:
        assert kwargs["allow_elevation"] is setup
        return {"status": "available", "implementation": "mxc", "reason": "codex_mxc_verified"}

    monkeypatch.setattr(installer, "install_document_runtime", forbidden)
    monkeypatch.setattr(installer, "_run_store_migrations", forbidden)
    monkeypatch.setattr(installer, "prune_after_install", forbidden)
    monkeypatch.setattr(installer, "ensure_bundled_codex_windows_helpers", forbidden)
    monkeypatch.setattr(installer, "prepare_existing_windows_fence", fence)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "document_install",
            "--workspace",
            str(tmp_path / "workspace"),
            "--protected-execution-only",
            *(["--setup-protected-execution"] if setup else []),
        ],
    )
    installer.main()
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "ready"
    assert result["protected_execution"]["implementation"] == "mxc"
    assert result["codex_windows_helpers"]["status"] == "not_required"
    assert not (tmp_path / "workspace").exists()


def test_fence_only_exits_nonzero_when_setup_is_still_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        installer,
        "prepare_existing_windows_fence",
        lambda **kwargs: {"status": "setup_required", "next_action": "Complete sandbox setup"},
    )
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "not_required"}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "document_install",
            "--workspace",
            str(tmp_path / "workspace"),
            "--protected-execution-only",
        ],
    )
    with pytest.raises(SystemExit) as failure:
        installer.main()
    assert failure.value.code == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "setup_required"
    assert not (tmp_path / "workspace").exists()


def test_fence_only_preserves_failed_enforcement_without_a_ready_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**kwargs: Any) -> None:
        raise installer.DocumentRuntimeError("Confined-write verification failed")

    monkeypatch.setattr(installer, "prepare_existing_windows_fence", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "document_install",
            "--workspace",
            str(tmp_path / "workspace"),
            "--protected-execution-only",
        ],
    )
    with pytest.raises(installer.DocumentRuntimeError, match="Confined-write"):
        installer.main()
    assert '"status": "ready"' not in capsys.readouterr().out
    assert not (tmp_path / "workspace").exists()
