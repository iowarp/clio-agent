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

import functools
import logging
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
from clio_agent.runtime import sandbox, sandbox_cli, sandbox_codex
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


@pytest.fixture(autouse=True)
def _pin_ladder_to_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the confinement-ladder resolver's platform default to win32 for this whole file.

    ``sandbox._resolve_backend``'s ``platform: str = sys.platform`` default is bound at IMPORT
    time -- on a Linux CI runner that default is permanently ``"linux"``, so the post-setup
    re-resolve chain (``sandbox.reresolve_after_setup`` -> ``install_sandbox`` ->
    ``_resolve_backend(env=env)``, which never passes an explicit ``platform=``) would silently
    take the Linux/Landlock ladder branch instead of the win32 ``codex_windows_gate`` branch
    every test in this file fakes, turning an intended DEGRADED into a false READY. Pin it here
    so these Windows-semantics unit tests behave the same on every CI OS.
    ``test_setup_unsupported_off_windows`` exercises the ROUTE's own off-Windows gate
    separately (it patches ``sys.platform`` directly, before ``run_sandbox_setup`` is ever
    reached) and is unaffected by this pin.
    """

    monkeypatch.setattr(
        sandbox, "_resolve_backend", functools.partial(sandbox._resolve_backend, platform="win32")
    )


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

    # This run activates a FAKE codex fence via `sandbox.reresolve_after_setup`, which mutates
    # the process-global `sandbox._STATE` cache. Restore it at teardown so the fake ACTIVE state
    # can never leak into a later test that reads `sandbox.current_state()` (#A6 review).
    monkeypatch.setattr(sandbox, "_STATE", sandbox.current_state())
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
        # Explicit: `provision_codex_windows`'s own `platform` default is bound at
        # `sandbox_cli` IMPORT time, so on a non-Windows CI runner it would otherwise
        # take the typed off-Windows no-op path regardless of every fake injected above.
        platform="win32",
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
        platform="win32",
    )

    assert result.status == sandbox_cli.OUTCOME_ENFORCEMENT_UNVERIFIED
    assert result.reason == sandbox_codex.REASON_CODEX_ENFORCEMENT_ESCAPED
    assert result.elevated is True
    assert result.row.state == IntegrationState.DEGRADED


# --------------------------------------------------------------------------- #
# Pending-restart: setup succeeds but an already-spawned MCP fleet is not      #
# covered by the just-activated fence (#A6 review, HIGH).                      #
# --------------------------------------------------------------------------- #


class _FakeExecutor:
    """Stand-in for ``SyncMCPToolExecutor``/``AsyncMCPToolExecutor`` — only the attributes
    :func:`clio_agent.gact.sandbox_setup._fleet_already_running` reads."""

    def __init__(
        self,
        *,
        connected: bool,
        namespace_clients: dict[str, Any] | None = None,
        closed: bool = False,
    ) -> None:
        self._connected_namespaces = {"fs"} if connected else set()
        self._namespace_clients = dict(namespace_clients) if namespace_clients else {}
        self.closed = closed


def _fake_app_with_executor(executor: Any) -> Any:
    """A minimal ``app``-shaped stand-in exposing ``app.state.agent.tool_executor``."""

    agent = type("Agent", (), {"tool_executor": executor})()
    state = type("State", (), {"agent": agent})()
    return type("App", (), {"state": state})()


def _fake_app_with_fleet(*, connected: bool) -> Any:
    """A minimal ``app``-shaped stand-in exposing ``app.state.agent.tool_executor``."""

    return _fake_app_with_executor(_FakeExecutor(connected=connected))


def test_setup_marks_pending_restart_when_fleet_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fence goes inactive -> active while an MCP namespace already connected (spawned) in this
    process: the re-probed row is DEGRADED with the typed pending-restart reason -- a successful
    elevation must never claim READY over already-unfenced tool servers."""

    monkeypatch.setattr(
        sandbox,
        "_STATE",
        sandbox.SandboxResult(
            mechanism=sandbox.MECHANISM_NONE, active=False, reason="test_floor", details={}
        ),
    )
    monkeypatch.setattr(sandbox, "_FENCE_PENDING_RESTART", False)
    monkeypatch.setattr(sandbox_codex, "detect_codex", lambda **_kw: _fake_codex_detected())
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **_kw: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )

    result = run_sandbox_setup(
        elevator=lambda _binary: (True, "elevated ok"),
        verifier=lambda _b, _r, platform="win32": (
            True,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED,
        ),
        gate=lambda *, platform: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
        app=_fake_app_with_fleet(connected=True),
        platform="win32",
    )

    assert result.status == sandbox_cli.OUTCOME_PROVISIONED  # provisioning itself still succeeded
    assert result.row.state == IntegrationState.DEGRADED
    assert result.row.details["reason"] == sandbox.REASON_FENCE_PENDING_RESTART
    assert result.row.next_action == "Restart CLIO to fence already-running tool servers."


def test_setup_stays_ready_when_no_fleet_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fresh-activation scenario, but with NO already-spawned fleet: the row stays READY."""

    monkeypatch.setattr(
        sandbox,
        "_STATE",
        sandbox.SandboxResult(
            mechanism=sandbox.MECHANISM_NONE, active=False, reason="test_floor", details={}
        ),
    )
    monkeypatch.setattr(sandbox, "_FENCE_PENDING_RESTART", False)
    monkeypatch.setattr(sandbox_codex, "detect_codex", lambda **_kw: _fake_codex_detected())
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **_kw: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )

    result = run_sandbox_setup(
        elevator=lambda _binary: (True, "elevated ok"),
        verifier=lambda _b, _r, platform="win32": (
            True,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED,
        ),
        gate=lambda *, platform: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
        app=_fake_app_with_fleet(connected=False),
        platform="win32",
    )

    assert result.row.state == IntegrationState.READY


def test_setup_marks_pending_restart_when_fleet_prewarmed_without_routed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prewarmed namespace (``_namespace_clients`` populated by ``gact/mcp_readiness.py``'s
    ``prepare_namespace`` -> ``_connect_namespace``) counts as an already-running fleet even
    though ``_connected_namespaces`` -- stamped only on the FIRST ROUTED tool call -- is still
    empty (#A5 review)."""

    monkeypatch.setattr(
        sandbox,
        "_STATE",
        sandbox.SandboxResult(
            mechanism=sandbox.MECHANISM_NONE, active=False, reason="test_floor", details={}
        ),
    )
    monkeypatch.setattr(sandbox, "_FENCE_PENDING_RESTART", False)
    monkeypatch.setattr(sandbox_codex, "detect_codex", lambda **_kw: _fake_codex_detected())
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **_kw: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )

    executor = _FakeExecutor(connected=False, namespace_clients={"fs": object()})
    result = run_sandbox_setup(
        elevator=lambda _binary: (True, "elevated ok"),
        verifier=lambda _b, _r, platform="win32": (
            True,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED,
        ),
        gate=lambda *, platform: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
        app=_fake_app_with_executor(executor),
        platform="win32",
    )

    assert result.row.state == IntegrationState.DEGRADED
    assert result.row.details["reason"] == sandbox.REASON_FENCE_PENDING_RESTART


def test_setup_stays_ready_when_only_a_closed_executor_has_leftover_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLOSED executor's leftover ``_connected_namespaces``/``_namespace_clients`` must not
    count as an already-running fleet (#A5 review)."""

    monkeypatch.setattr(
        sandbox,
        "_STATE",
        sandbox.SandboxResult(
            mechanism=sandbox.MECHANISM_NONE, active=False, reason="test_floor", details={}
        ),
    )
    monkeypatch.setattr(sandbox, "_FENCE_PENDING_RESTART", False)
    monkeypatch.setattr(sandbox_codex, "detect_codex", lambda **_kw: _fake_codex_detected())
    monkeypatch.setattr(
        sandbox_codex,
        "codex_windows_gate",
        lambda **_kw: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
    )

    executor = _FakeExecutor(connected=True, namespace_clients={"fs": object()}, closed=True)
    result = run_sandbox_setup(
        elevator=lambda _binary: (True, "elevated ok"),
        verifier=lambda _b, _r, platform="win32": (
            True,
            sandbox_codex.REASON_CODEX_ENFORCEMENT_VERIFIED,
        ),
        gate=lambda *, platform: (False, sandbox_codex.REASON_CODEX_WINDOWS_UNPROVISIONED),
        app=_fake_app_with_executor(executor),
        platform="win32",
    )

    assert result.row.state == IntegrationState.READY


# --------------------------------------------------------------------------- #
# POST /v1/system/sandbox/setup — conflict + off-Windows, over the real route. #
# --------------------------------------------------------------------------- #


def test_setup_conflict_when_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent setup call gets a typed 409, never a second overlapping elevation.

    The lock is held from a background thread; the request thread's `run_sandbox_setup` must
    fail the non-blocking acquire immediately, before touching any provisioning collaborator.

    The route's OWN live `sys.platform.startswith("win")` gate runs BEFORE it ever calls
    `run_sandbox_setup` (see `routes/sandbox_setup.py`), so on a non-Windows CI runner this
    would otherwise short-circuit straight to the typed 501 and never reach the lock at all --
    forced to "win32" here so the conflict path is exercised the same on every OS (the lock
    check itself is the very first thing `run_sandbox_setup` does, before touching `platform`).
    """

    monkeypatch.setattr(sandbox_setup_routes, "current_platform", lambda: "win32")
    held = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        SETUP_LOCK.acquire()
        held.set()
        # Held until the test releases it (always, in `finally`): a bounded
        # hold let the lock lapse while build_app + TestClient startup ran on a
        # loaded CI runner, and the request then got 200 instead of 409.
        release.wait()
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
    assert body["row"]["setup_in_progress"] is True  # lock held: same shape as the GET
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

    monkeypatch.setattr(sandbox_setup_routes, "current_platform", lambda: "linux")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        resp = client.post("/v1/system/sandbox/setup")

    assert resp.status_code == 501
    body = resp.json()
    assert body["status"] == sandbox_cli.STATUS_NOT_WINDOWS
    assert body["reason"] == sandbox_setup_routes.REASON_SETUP_UNSUPPORTED
    assert body["elevated"] is False
    assert body["row"]["name"] == "sandbox"
    # One projection for every route that carries the row: the refusal bodies must expose
    # the same desktop-panel fields as the GET so the client never sees two row shapes.
    assert set(body["row"]) >= {"setup_in_progress", "reason", "codex_source"}
    assert body["row"]["setup_in_progress"] is False


# --------------------------------------------------------------------------- #
# GET /v1/system/sandbox — the desktop-panel conveniences (#A6 review, MEDIUM). #
# --------------------------------------------------------------------------- #


def test_get_sandbox_row_carries_setup_progress_reason_and_codex_source(
    tmp_path: Path,
) -> None:
    """The row carries `setup_in_progress` (False, idle), a non-blank typed `reason`, and a
    `codex_source` the panel can render without parsing `summary`/`details`."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        resp = client.get("/v1/system/sandbox")

    assert resp.status_code == 200
    body = resp.json()
    assert body["setup_in_progress"] is False
    assert isinstance(body["reason"], str) and body["reason"]
    assert body["codex_source"] in (
        None,
        sandbox_codex.CODEX_SOURCE_BUNDLED,
        sandbox_codex.CODEX_SOURCE_PATH,
    )


def test_get_sandbox_row_reports_setup_in_progress_while_locked(tmp_path: Path) -> None:
    """`setup_in_progress` reflects the module lock live -- the panel can show a spinner instead
    of racing a concurrent setup run."""

    held = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        SETUP_LOCK.acquire()
        held.set()
        # Held until the test releases it (always, in `finally`): a bounded
        # hold let the lock lapse while build_app + TestClient startup ran on a
        # loaded CI runner, and the request then got 200 instead of 409.
        release.wait()
        SETUP_LOCK.release()

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    try:
        assert held.wait(timeout=2), "background thread never acquired the setup lock"
        app = build_app(sessions_path=tmp_path / "sessions.json")
        with TestClient(app) as client:
            resp = client.get("/v1/system/sandbox")
    finally:
        release.set()
        holder.join(timeout=5)

    assert resp.status_code == 200
    assert resp.json()["setup_in_progress"] is True


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


def test_bundled_root_found_but_binary_missing_warns_and_falls_back_to_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A desktop runtime root WAS found (`runtime.json` present) but its bundled `codex.exe` is
    missing on disk -- a packaging defect, not an ordinary "no bundled runtime": a WARNING fires
    and detection falls back to PATH, carrying the miss on the returned detection (#A6 review)."""

    runtime_root = tmp_path
    (runtime_root / "runtime.json").write_text("{}", encoding="utf-8")
    # No codex_cli_bin tree created at all -- the bundled binary path does not exist on disk.

    with caplog.at_level(logging.WARNING, logger="clio_agent.runtime.sandbox_codex"):
        det = sandbox_codex.detect_codex(
            bundled_root=lambda: runtime_root,
            which=lambda _name: "C:\\codex\\codex.cmd" if _name == "codex.cmd" else None,
            version_reader=lambda _binary: "0.145.0",
            platform="win32",
        )

    assert det.installed is True
    assert det.source == sandbox_codex.CODEX_SOURCE_PATH
    assert det.bundled_codex_absent is True
    assert any("bundled_codex_absent" in record.getMessage() for record in caplog.records), (
        "a packaging defect (bundled root found, binary missing) must warn with a typed reason"
    )
