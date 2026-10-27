"""Installation is complete only after both package sets and Office are available."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest

from clio_agent.runtime import document_install as installer


@pytest.mark.parametrize("javascript", ["ready", "failed"])
def test_installer_records_only_complete_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, javascript: str
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    result: dict[str, Any] = {
        "status": "ready",
        "runtime_id": "locked",
        "javascript": {"status": javascript},
        "native_tools": {},
        "capabilities": {},
    }
    monkeypatch.setattr(installer, "prepare_document_runtime", lambda *a, **kw: result)
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda: "private soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "available"}
    )
    monkeypatch.setattr(
        installer, "prepare_existing_windows_fence", lambda **kwargs: {"status": "available"}
    )
    stages: list[str] = []
    receipt = cache / "locked" / "installed.json"
    if javascript == "failed":
        with pytest.raises(installer.DocumentRuntimeError, match="Node/pnpm"):
            installer.install_document_runtime(
                tmp_path / "workspace", cache_root=cache, progress=stages.append
            )
        assert not receipt.exists()
        assert "All managed runtime packages are installed and verified." not in stages
    else:
        installed = installer.install_document_runtime(
            tmp_path / "workspace", cache_root=cache, progress=stages.append
        )
        assert installed["capabilities"]["office_render_recalculate"] == "available"
        assert installed["native_tools"]["gh"]["authentication"] == "clio_account"
        assert (
            json.loads(receipt.read_text())["native_tools"]["soffice"]["path"] == "private soffice"
        )
        assert set(stages[:-1]) == {
            "Preparing and checking the Office renderer...",
            "Office rendering is ready.",
            "Preparing and checking the GitHub command-line tool...",
            "GitHub command-line tool is ready.",
            "Preparing and checking protected execution...",
        }
        assert stages[-1] == "All managed runtime packages are installed and verified."


def test_installer_overlaps_independent_work_and_joins_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    receipt = cache / "locked" / "installed.json"
    started = Barrier(3, timeout=5)
    packages_done = Event()
    office_done = Event()
    github_done = Event()

    def packages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.wait()
        assert not receipt.exists()
        packages_done.set()
        return {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        }

    def office() -> str:
        started.wait()
        assert packages_done.wait(5)
        assert not receipt.exists()
        office_done.set()
        return "private soffice"

    def github() -> Path:
        started.wait()
        assert packages_done.wait(5)
        assert not receipt.exists()
        github_done.set()
        return tmp_path / "gh"

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(installer, "prepare_office_runtime", office)
    monkeypatch.setattr(installer, "ensure_github_cli", github)
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "available"}
    )
    monkeypatch.setattr(
        installer, "prepare_existing_windows_fence", lambda **kwargs: {"status": "available"}
    )
    installer.install_document_runtime(tmp_path / "workspace", cache_root=cache)
    assert office_done.is_set() and github_done.is_set()
    assert receipt.exists()


@pytest.mark.parametrize("failed_step", ["office", "github", "python"])
def test_installer_failure_waits_for_other_work_without_publishing_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_step: str
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    started = Barrier(3, timeout=5)
    finished: list[str] = []

    def step(name: str) -> None:
        started.wait()
        finished.append(name)
        if name == failed_step:
            raise RuntimeError(f"{name} failed")

    def packages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        step("python")
        return {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        }

    def office() -> str:
        step("office")
        return "private soffice"

    def github() -> Path:
        step("github")
        return tmp_path / "gh"

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(installer, "prepare_office_runtime", office)
    monkeypatch.setattr(installer, "ensure_github_cli", github)
    stages: list[str] = []
    with pytest.raises(RuntimeError, match=f"{failed_step} failed"):
        installer.install_document_runtime(
            tmp_path / "workspace", cache_root=cache, progress=stages.append
        )
    assert set(finished) == {"python", "office", "github"}
    assert not (cache / "locked" / "installed.json").exists()
    assert "All managed runtime packages are installed and verified." not in stages


@pytest.mark.parametrize("setup", [True, False])
@pytest.mark.parametrize("failed", [True, False])
def test_installer_explicit_setup_is_forwarded_and_failed_setup_has_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup: bool, failed: bool
) -> None:
    (tmp_path / "locked").mkdir()
    monkeypatch.setattr(
        installer,
        "prepare_document_runtime",
        lambda *args, **kwargs: {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        },
    )
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda: "soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")

    def fence(**kwargs: Any) -> dict[str, str]:
        assert kwargs["allow_elevation"] is setup
        if failed:
            raise installer.DocumentRuntimeError("Windows approval declined")
        return {"status": "available", "implementation": "mxc"}

    def helpers() -> None:
        pytest.fail("Verified MXC must not download unused legacy helpers")

    monkeypatch.setattr(installer, "prepare_existing_windows_fence", fence)
    monkeypatch.setattr(installer, "ensure_bundled_codex_windows_helpers", helpers)
    if failed:
        with pytest.raises(installer.DocumentRuntimeError, match="approval declined"):
            installer.install_document_runtime(
                tmp_path / "workspace", cache_root=tmp_path, setup_protected_execution=setup
            )
    else:
        result = installer.install_document_runtime(
            tmp_path / "workspace", cache_root=tmp_path, setup_protected_execution=setup
        )
        assert result["native_tools"]["codex_windows_helpers"]["status"] == "not_required"
    assert (tmp_path / "locked/installed.json").exists() is not failed


def test_a_host_without_office_libraries_still_installs_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found on a minimal Linux SSH host: the renderer could not load libXinerama, and the
    whole CLIO install failed. Everything else installs; Office reports cause and fix."""
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    result: dict[str, Any] = {
        "status": "ready",
        "runtime_id": "locked",
        "javascript": {"status": "ready"},
        "native_tools": {},
        "capabilities": {},
    }
    error = (
        "soffice exited 127: /opt/libreoffice26.2/program/oosplash: error while loading "
        "shared libraries: libXinerama.so.1: cannot open shared object file"
    )

    def no_office() -> str:
        raise installer.DocumentError(error)

    monkeypatch.setattr(installer, "prepare_document_runtime", lambda *a, **kw: result)
    monkeypatch.setattr(installer, "prepare_office_runtime", no_office)
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "available"}
    )
    monkeypatch.setattr(
        installer, "prepare_existing_windows_fence", lambda **kwargs: {"status": "available"}
    )
    stages: list[str] = []
    installed = installer.install_document_runtime(
        tmp_path / "workspace", cache_root=cache, progress=stages.append
    )
    office = installed["native_tools"]["soffice"]
    assert office["status"] == "unavailable"
    assert office["reason"] == "office_runtime_unavailable"
    assert "libXinerama.so.1" in office["next_action"]
    assert installed["capabilities"]["office_render_recalculate"] == "unavailable"
    assert installed["native_tools"]["gh"]["status"] == "available"
    # Not recorded as complete, so the next install or launch checks Office again.
    assert not (cache / "locked" / "installed.json").exists()
    assert any("Office rendering is unavailable: Install the system library" in s for s in stages)
    assert stages[-1] == "The other managed packages are installed and verified."
