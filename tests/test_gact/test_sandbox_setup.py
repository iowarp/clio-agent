"""Tests for the desktop protected-execution setup seam (gact/sandbox_setup.py + its routes).

Covers the "Set up protected execution" button's backing surface: `run_sandbox_setup`
(`gact/sandbox_setup.py`) — a thin, lock-guarded wrapper around the EXISTING
`sandbox_cli.provision_codex_windows` engine that re-resolves the confinement ladder and
re-probes the `sandbox` doctor row afterwards — and its two routes
(`GET /v1/system/sandbox`, `POST /v1/system/sandbox/setup`, `gact/routes/sandbox_setup.py`).

HARD SAFETY RULE (mirrors tests/test_runtime/test_sandbox_cli.py): the real self-elevating
`codex sandbox` setup MUTATES the machine (creates the `codexsandbox*` accounts, pops a UAC
prompt) and is NEVER exercised here. An autouse fixture monkeypatches the default
`sandbox_cli._elevated_codex_setup` to an assertion-raising spy, and the default
`sandbox_cli.grant_fleet_runtime_access` / `sandbox_codex.write_codex_provision_marker` to
no-op fakes, so nothing touches ShellExecute, `icacls`, or the real per-user marker file even
when a test does not itself inject those collaborators (e.g. the conflict/unsupported paths
never reach provisioning at all).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.routes import sandbox_setup as sandbox_setup_routes
from clio_agent.gact.routes.health_projection import integration_to_wire
from clio_agent.gact.sandbox_setup import _SETUP_LOCK as SETUP_LOCK
from clio_agent.gact.sandbox_setup import (
    REASON_SETUP_IN_PROGRESS,
    SandboxSetupConflict,
    run_sandbox_setup,
)
from clio_agent.runtime import sandbox_cli, sandbox_codex
from clio_agent.runtime.sandbox_doctor import probe_sandbox
from clio_agent.runtime.status import IntegrationState

# --------------------------------------------------------------------------- #
# Blanket safety net — no test here may touch the real machine.                #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _never_touch_the_real_machine(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Guard every default provisioning collaborator so a test that forgets to inject its own
    fake still cannot reach the real elevation / icacls / marker-write side effects."""

    calls: dict[str, list[Any]] = {"elevate": [], "grant": [], "marker": []}

    def _spy_elevate(binary: str) -> tuple[bool, str]:
        calls["elevate"].append(binary)
        raise AssertionError("the real self-elevation must never run from a unit test")

    def _spy_grant() -> list[dict[str, Any]]:
        calls["grant"].append(True)
        return []

    def _spy_marker(version: str, **kwargs: Any) -> Path:
        calls["marker"].append((version, kwargs))
        return Path("unused-marker.json")

    monkeypatch.setattr(sandbox_cli, "_elevated_codex_setup", _spy_elevate)
    monkeypatch.setattr(sandbox_cli, "grant_fleet_runtime_access", _spy_grant)
    monkeypatch.setattr(sandbox_codex, "write_codex_provision_marker", _spy_marker)
    return calls


def _fake_codex_detected() -> sandbox_codex.CodexDetection:
    return sandbox_codex.CodexDetection(
        installed=True,
        binary_path="C:\\codex\\codex.cmd",
        version="0.145.0",
        reason=sandbox_codex.REASON_CODEX_DETECTED,
        source=sandbox_codex.CODEX_SOURCE_PATH,
    )


# --------------------------------------------------------------------------- #
# GET /v1/system/sandbox                                                       #
# --------------------------------------------------------------------------- #


def test_get_sandbox_row_matches_doctor(tmp_path: Path) -> None:
    """The row route is exactly `integration_to_wire(probe_sandbox())` — no divergent shape."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        resp = client.get("/v1/system/sandbox")
        assert resp.status_code == 200
        expected = integration_to_wire(probe_sandbox())
        body = resp.json()

    assert body["name"] == expected.name == "sandbox"
    assert body["status"] == expected.status
    assert body["summary"] == expected.summary
    assert body["required"] == expected.required


# --------------------------------------------------------------------------- #
# run_sandbox_setup — unit-level, every collaborator injected/faked.           #
# --------------------------------------------------------------------------- #


def test_setup_runs_injected_elevator_and_reprobes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unprovisioned -> injected elevate+verify succeed -> the row RE-PROBES READY.

    `run_sandbox_setup` only forwards `elevator`/`verifier`/`gate` to
    `provision_codex_windows`; the POST-setup ladder re-resolve reads the module-level
    `sandbox_codex.codex_windows_gate` directly (no injection seam through this call chain), so
    that default is faked too -- deterministically, never a real `net user`/marker read.
    """

    monkeypatch.setattr(sandbox_codex, "detect_codex", lambda **_kw: _fake_codex_detected())
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **_kw: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )

    elevated: list[str] = []

    def _elevator(binary: str) -> tuple[bool, str]:
        elevated.append(binary)
        return True, "codex Windows sandbox setup completed under elevation"

    result = run_sandbox_setup(
        elevator=_elevator,
        verifier=lambda _b, _r, platform="win32": (
            True,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED,
        ),
        gate=lambda *, platform: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
    )

    assert elevated == ["C:\\codex\\codex.cmd"]  # the setup elevation ran exactly once
    assert result.status == sandbox_cli.OUTCOME_PROVISIONED
    assert result.reason == sandbox_cli.REASON_PROVISIONED
    assert result.elevated is True
    assert result.row.state == IntegrationState.READY


def test_setup_reports_typed_failure_when_verifier_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elevation succeeds but the fence let an out-of-root write through: an honest degrade,
    never a false green (#1026) -- the row stays DEGRADED with the escaped reason."""

    monkeypatch.setattr(sandbox_codex, "detect_codex", lambda **_kw: _fake_codex_detected())
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **_kw: (False, sandbox_codex.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
    )

    result = run_sandbox_setup(
        elevator=lambda _binary: (True, "elevated ok"),
        verifier=lambda _b, _r, platform="win32": (
            False,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_ESCAPED,
        ),
        gate=lambda *, platform: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
    )

    assert result.status == sandbox_cli.OUTCOME_ENFORCEMENT_UNVERIFIED
    assert result.reason == sandbox_codex.REASON_CODEX_ENFORCEMENT_ESCAPED
    assert result.elevated is True
    assert result.row.state == IntegrationState.DEGRADED


# --------------------------------------------------------------------------- #
# POST /v1/system/sandbox/setup — conflict + off-Windows, over the real route. #
# --------------------------------------------------------------------------- #


def test_setup_conflict_when_already_running(tmp_path: Path) -> None:
    """A concurrent setup call gets a typed 409, never a second overlapping elevation.

    The lock is held from a background thread; the request thread's `run_sandbox_setup` must
    fail the non-blocking acquire immediately, before touching any provisioning collaborator.
    """

    held = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        SETUP_LOCK.acquire()
        held.set()
        release.wait(timeout=5)
        SETUP_LOCK.release()

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    try:
        assert held.wait(timeout=2), "background thread never acquired the setup lock"
        app = build_app(sessions_path=tmp_path / "sessions.json")
        with TestClient(app) as client:
            resp = client.post("/v1/system/sandbox/setup")
    finally:
        release.set()
        holder.join(timeout=5)

    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == REASON_SETUP_IN_PROGRESS
    assert body["row"]["name"] == "sandbox"


def test_run_sandbox_setup_raises_typed_conflict_directly() -> None:
    """`run_sandbox_setup` itself raises `SandboxSetupConflict` (the route's 409 source)."""

    assert SETUP_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(SandboxSetupConflict):
            run_sandbox_setup()
    finally:
        SETUP_LOCK.release()


def test_setup_unsupported_off_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Off-Windows the route is a typed 501 with the current row -- nothing to provision."""

    monkeypatch.setattr(sandbox_setup_routes.sys, "platform", "linux")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        resp = client.post("/v1/system/sandbox/setup")

    assert resp.status_code == 501
    body = resp.json()
    assert body["status"] == sandbox_cli.STATUS_NOT_WINDOWS
    assert body["reason"] == sandbox_setup_routes.REASON_SETUP_UNSUPPORTED
    assert body["elevated"] is False
    assert body["row"]["name"] == "sandbox"


# --------------------------------------------------------------------------- #
# Bundled codex.exe detection (desktop runtime).                               #
# --------------------------------------------------------------------------- #


def test_bundled_codex_detected_first(tmp_path: Path) -> None:
    """A desktop runtime root with a bundled `codex.exe` wins over any PATH lookup."""

    runtime_root = tmp_path
    (runtime_root / "runtime.json").write_text("{}", encoding="utf-8")
    codex_dir = runtime_root / "python" / "Lib" / "site-packages" / "codex_cli_bin" / "bin"
    codex_dir.mkdir(parents=True)
    codex_exe = codex_dir / "codex.exe"
    codex_exe.write_text("stub", encoding="utf-8")

    def _which_must_not_be_used(_name: str) -> str | None:
        raise AssertionError("PATH lookup must not run when the bundled binary exists")

    det = sandbox_codex.detect_codex(
        bundled_root=lambda: runtime_root,
        which=_which_must_not_be_used,
        version_reader=lambda _binary: "0.145.0",
    )

    assert det.installed is True
    assert det.source == sandbox_codex.CODEX_SOURCE_BUNDLED
    assert det.binary_path == str(codex_exe)
    assert det.reason == sandbox_codex.REASON_CODEX_DETECTED


def test_codex_detection_falls_back_to_path_outside_bundled_runtime() -> None:
    """No bundled runtime root -> the ordinary PATH lookup still runs (`source == "path"`)."""

    det = sandbox_codex.detect_codex(
        bundled_root=lambda: None,
        which=lambda _name: "C:\\codex\\codex.cmd" if _name == "codex.cmd" else None,
        version_reader=lambda _binary: "0.145.0",
        platform="win32",
    )

    assert det.installed is True
    assert det.source == sandbox_codex.CODEX_SOURCE_PATH
    assert det.binary_path == "C:\\codex\\codex.cmd"
