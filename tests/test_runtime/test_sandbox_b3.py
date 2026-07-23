"""B3 (#977): Windows `clio sandbox setup`/`status`, srt Windows backend, WinError-5 mint.

Host-agnostic unit coverage — the real self-elevation + `srt windows-install` are the
OWNER-GATED live gate (a UAC prompt that mutates the machine) and are NEVER exercised here.
Every test drives the flow with FAKED system state (injected srt detection / provisioned
probe / installer), so nothing touches ShellExecute or the real srt binary. Pinned:

* :func:`windows_sandbox_state` against every faked verdict (not_windows / srt_absent /
  unprovisioned / provisioned), with typed guided next-actions (never a raw error);
* :func:`provision_windows_sandbox` idempotency (already_provisioned → no-op, zero prompts,
  installer NEVER called) + the fresh-provision, install-failed and verify-failed branches;
* the self-elevation guard raises off-win32 (the real ShellExecute is the manual live gate);
* the ladder resolves ``srt_windows`` when provisioned and floors ``windows_unprovisioned``/
  the srt-absent reason otherwise (monkeypatched platform + faked verdict);
* the Windows ``shell`` profile reuses ``fleet`` with a typed ``details`` note;
* the WinError-5 → ``policy_violation`` mapping + the in-root/path-less precision skips;
* the ``clio sandbox setup``/``status`` CLI parse + status output shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.runtime import sandbox
from clio_agent.runtime import sandbox_provision as swp
from clio_agent.runtime import sandbox_verify as swv

# --------------------------------------------------------------------------- #
# Faked srt detection helpers (never a real srt/node probe).                    #
# --------------------------------------------------------------------------- #


def _det(reason: str, *, installed: bool = False, version: str = "") -> sandbox.SrtDetection:
    return sandbox.SrtDetection(
        installed=installed,
        binary_path="C:\\srt\\srt.cmd" if installed else "",
        version=version,
        node_present=installed,
        node_version="v22.0.0" if installed else "",
        node_ok=installed,
        socat_present=False,
        reason=reason,
    )


def _ready_det() -> sandbox.SrtDetection:
    """srt present + preconditions met + a supported version (the provisionable state)."""
    return _det(sandbox.REASON_SRT_DETECTED_DEFERRED, installed=True, version="0.0.66")


# --------------------------------------------------------------------------- #
# windows_sandbox_state — the single verdict source.                            #
# --------------------------------------------------------------------------- #


def test_state_not_windows_off_platform() -> None:
    """Off-Windows there is nothing to provision (the ladder fences automatically)."""
    state = swp.windows_sandbox_state(platform="linux", detection=_det("x"))
    assert state.status == swp.STATUS_NOT_WINDOWS
    assert state.reason == swp.REASON_NOT_WINDOWS


def test_state_srt_absent_points_at_npm() -> None:
    """srt absent on Windows → typed srt_absent + the EXACT npm install pointer (never raw)."""
    state = swp.windows_sandbox_state(
        platform="win32", detection=_det(sandbox.REASON_SRT_NOT_INSTALLED)
    )
    assert state.status == swp.STATUS_SRT_ABSENT
    assert state.reason == sandbox.REASON_SRT_NOT_INSTALLED
    assert swp.SRT_INSTALL_POINTER in state.next_action
    assert "npm install -g @anthropic-ai/sandbox-runtime" in state.next_action


def test_state_node_missing_points_at_node() -> None:
    """node missing → typed srt_node_missing with a Node.js pointer (guided precondition)."""
    state = swp.windows_sandbox_state(
        platform="win32", detection=_det(sandbox.REASON_SRT_NODE_MISSING, installed=True)
    )
    assert state.status == swp.STATUS_SRT_ABSENT
    assert state.reason == sandbox.REASON_SRT_NODE_MISSING
    assert "Node.js" in state.next_action


def test_state_version_unsupported_is_srt_absent() -> None:
    """srt present but below the validated floor → srt_absent(srt_version_unsupported)."""
    state = swp.windows_sandbox_state(
        platform="win32",
        detection=_det(sandbox.REASON_SRT_DETECTED_DEFERRED, installed=True, version="0.0.1"),
    )
    assert state.status == swp.STATUS_SRT_ABSENT
    assert state.reason == swp.REASON_SRT_VERSION_UNSUPPORTED


def test_state_unprovisioned_next_action_is_setup() -> None:
    """srt ready but the fence not provisioned → unprovisioned + `clio sandbox setup`."""
    state = swp.windows_sandbox_state(
        platform="win32",
        detection=_ready_det(),
        provisioned_probe=lambda: (False, sandbox.REASON_WINDOWS_UNPROVISIONED),
    )
    assert state.status == swp.STATUS_UNPROVISIONED
    assert state.reason == sandbox.REASON_WINDOWS_UNPROVISIONED
    assert "clio sandbox setup" in state.next_action


def test_state_provisioned_no_action() -> None:
    """A provisioned fence → provisioned, no action required."""
    state = swp.windows_sandbox_state(
        platform="win32",
        detection=_ready_det(),
        provisioned_probe=lambda: (True, swp.REASON_WINDOWS_PROVISIONED),
    )
    assert state.status == swp.STATUS_PROVISIONED
    assert state.reason == swp.REASON_WINDOWS_PROVISIONED


def test_default_provisioned_probe_incomplete_when_principal_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker present but the srt-sandbox account gone → incomplete, NOT a false green."""
    monkeypatch.setattr(swp, "_read_marker", lambda: {"principal": swp.SRT_WINDOWS_PRINCIPAL})
    monkeypatch.setattr(swp, "_srt_principal_exists", lambda *, platform="win32": False)
    provisioned, reason = swp._default_provisioned_probe(platform="win32")
    assert provisioned is False
    assert reason == swp.REASON_WINDOWS_PROVISION_INCOMPLETE


# --------------------------------------------------------------------------- #
# provision_windows_sandbox — idempotency + the flow around the guarded call.   #
# --------------------------------------------------------------------------- #


class _SpyInstaller:
    """A fake `srt windows-install` — records calls so we can assert it is NEVER run."""

    def __init__(self, ok: bool = True, detail: str = "ok") -> None:
        self.calls: list[str] = []
        self._ok = ok
        self._detail = detail

    def __call__(self, binary: str) -> tuple[bool, str]:
        self.calls.append(binary)
        return self._ok, self._detail


def test_provision_srt_absent_never_elevates() -> None:
    """srt absent → guided reason, and the installer (a UAC prompt) is NEVER called."""
    spy = _SpyInstaller()
    state = swp.WindowsSandboxState(
        status=swp.STATUS_SRT_ABSENT,
        reason=sandbox.REASON_SRT_NOT_INSTALLED,
        next_action=swp.SRT_INSTALL_POINTER,
    )
    result = swp.provision_windows_sandbox(state=state, installer=spy)
    assert result.ok is False
    assert result.status == swp.STATUS_SRT_ABSENT
    assert spy.calls == []  # no elevation attempted


def test_provision_already_provisioned_is_zero_prompt_noop() -> None:
    """Idempotence: a provisioned host re-run no-ops WITHOUT elevating (zero prompts)."""
    spy = _SpyInstaller()
    state = swp.WindowsSandboxState(
        status=swp.STATUS_PROVISIONED, reason=swp.REASON_WINDOWS_PROVISIONED
    )
    result = swp.provision_windows_sandbox(state=state, installer=spy)
    assert result.ok is True
    assert result.status == swp.OUTCOME_ALREADY_PROVISIONED
    assert result.reason == swp.REASON_ALREADY_PROVISIONED
    assert spy.calls == []  # the re-run never prompts


def test_provision_fresh_success_writes_marker_and_verifies() -> None:
    """Unprovisioned → one install, enforcement verified, marker persisted → provisioned."""
    spy = _SpyInstaller(ok=True)
    written: list[tuple[str, bool]] = []
    state = swp.WindowsSandboxState(
        status=swp.STATUS_UNPROVISIONED,
        reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
        srt=_ready_det(),
    )
    provisioned = swp.WindowsSandboxState(
        status=swp.STATUS_PROVISIONED, reason=swp.REASON_WINDOWS_PROVISIONED
    )
    result = swp.provision_windows_sandbox(
        state=state,
        installer=spy,
        marker_writer=lambda v, **k: written.append((v, k.get("enforcement_verified"))),
        state_reader=lambda: provisioned,
        verifier=lambda _b: (True, swv.REASON_WINDOWS_ENFORCEMENT_VERIFIED),
    )
    assert result.ok is True
    assert result.status == swp.OUTCOME_PROVISIONED
    assert result.elevated is True
    assert spy.calls == ["C:\\srt\\srt.cmd"]  # the ready detection's binary
    assert written == [("0.0.66", True)]  # marker persisted: version + verified enforcement


def test_provision_install_failure_is_typed() -> None:
    """A failed install → typed provision_failed with a retry next-action (no raw error)."""
    spy = _SpyInstaller(ok=False, detail="srt windows-install exited 1")
    state = swp.WindowsSandboxState(
        status=swp.STATUS_UNPROVISIONED,
        reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
        srt=_ready_det(),
    )
    result = swp.provision_windows_sandbox(
        state=state, installer=spy, marker_writer=lambda v, **_k: None, state_reader=lambda: state
    )
    assert result.ok is False
    assert result.status == swp.OUTCOME_PROVISION_FAILED
    assert result.reason == swp.REASON_PROVISION_FAILED


def test_provision_verify_failure_is_typed() -> None:
    """Install ran but the fence did not verify → typed provision_verify_failed."""
    spy = _SpyInstaller(ok=True)
    state = swp.WindowsSandboxState(
        status=swp.STATUS_UNPROVISIONED,
        reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
        srt=_ready_det(),
    )
    still_unprov = swp.WindowsSandboxState(
        status=swp.STATUS_UNPROVISIONED, reason=sandbox.REASON_WINDOWS_UNPROVISIONED
    )
    result = swp.provision_windows_sandbox(
        state=state,
        installer=spy,
        marker_writer=lambda v, **_k: None,
        state_reader=lambda: still_unprov,
    )
    assert result.ok is False
    assert result.status == swp.OUTCOME_PROVISION_VERIFY_FAILED


def test_provision_install_but_enforcement_unverified_is_honest_degrade() -> None:
    """Install + account OK but srt cannot enforce → honest ENFORCEMENT_UNVERIFIED, never green (#1026)."""
    spy = _SpyInstaller(ok=True)
    written: list[tuple[str, object]] = []
    state = swp.WindowsSandboxState(
        status=swp.STATUS_UNPROVISIONED,
        reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
        srt=_ready_det(),
    )
    unverified = swp.WindowsSandboxState(
        status=swp.STATUS_ENFORCEMENT_UNVERIFIED,
        reason=swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED,
    )
    result = swp.provision_windows_sandbox(
        state=state,
        installer=spy,
        marker_writer=lambda v, **k: written.append((v, k.get("enforcement_verified"))),
        state_reader=lambda: unverified,
        verifier=lambda _b: (False, swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED),
    )
    assert result.ok is False
    assert result.status == swp.STATUS_ENFORCEMENT_UNVERIFIED
    assert result.reason == swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED
    assert result.elevated is True
    assert written == [("0.0.66", False)]  # marker records the unverified verdict, not a fake green


def test_windows_state_maps_enforcement_unverified_marker() -> None:
    """A probe reporting the unverified reason → STATUS_ENFORCEMENT_UNVERIFIED (honest third state)."""
    state = swp.windows_sandbox_state(
        platform="win32",
        detection=_ready_det(),
        provisioned_probe=lambda: (False, swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED),
    )
    assert state.status == swp.STATUS_ENFORCEMENT_UNVERIFIED
    assert state.reason == swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED
    assert "advisory file policy" in state.next_action


def test_default_probe_unverified_when_enforcement_not_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker + principal present but enforcement_verified != True → unverified, not provisioned."""
    monkeypatch.setattr(swp, "_read_marker", lambda: {"principal": swp.SRT_WINDOWS_PRINCIPAL})
    monkeypatch.setattr(swp, "_srt_principal_exists", lambda *, platform="win32": True)
    provisioned, reason = swp._default_provisioned_probe(platform="win32")
    assert provisioned is False
    assert reason == swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED
    # And a marker WITH a verified verdict is genuinely provisioned.
    monkeypatch.setattr(
        swp,
        "_read_marker",
        lambda: {"principal": swp.SRT_WINDOWS_PRINCIPAL, "enforcement_verified": True},
    )
    provisioned2, reason2 = swp._default_provisioned_probe(platform="win32")
    assert provisioned2 is True
    assert reason2 == swp.REASON_WINDOWS_PROVISIONED


def test_default_installer_is_the_elevation_and_is_guarded() -> None:
    """The default installer IS the self-elevation, structurally guarded off-win32.

    The win32/non-win32 implementation is chosen at IMPORT time (so ``ctypes.wintypes`` is
    never imported off Windows and mypy stays green on the Linux runner). Off win32 the bound
    function is the raising stub — calling it raises BEFORE any ctypes touch. On win32 it is the
    real ShellExecute impl, which must NOT be invoked here (it pops a UAC prompt — the
    owner-gated manual live gate); we assert its identity only.
    """
    assert swp._elevated_srt_windows_install.__name__ == "_elevated_srt_windows_install"
    if swp.sys.platform != "win32":
        with pytest.raises(RuntimeError):
            swp._elevated_srt_windows_install("C:\\srt\\srt.cmd")


# --------------------------------------------------------------------------- #
# npm one-command install offer (owner direction on #977) — injected fakes only. #
# --------------------------------------------------------------------------- #


def _srt_absent_node_present() -> swp.WindowsSandboxState:
    """srt binary absent BUT node present (npm ships with node) — the offer-eligible state."""
    det = _det(sandbox.REASON_SRT_NOT_INSTALLED)  # installed=False, node_present=False by default
    det = sandbox.SrtDetection(
        installed=False,
        binary_path="",
        version="",
        node_present=True,  # node (hence npm) present — srt is the ONLY missing piece
        node_version="v22.0.0",
        node_ok=True,
        socat_present=False,
        reason=sandbox.REASON_SRT_NOT_INSTALLED,
    )
    return swp.WindowsSandboxState(
        status=swp.STATUS_SRT_ABSENT,
        reason=sandbox.REASON_SRT_NOT_INSTALLED,
        srt=det,
        next_action=swp.SRT_INSTALL_POINTER,
    )


class _SpyNpm:
    """A fake `npm install -g` — records calls; the real npm is NEVER run in tests."""

    def __init__(self, ok: bool = True, detail: str = "installed") -> None:
        self.calls = 0
        self._ok = ok
        self._detail = detail

    def __call__(self) -> tuple[bool, str]:
        self.calls += 1
        return self._ok, self._detail


def test_provision_offers_npm_install_then_continues_one_command() -> None:
    """npm present + consent → auto-install srt, then CONTINUE into provision (one command)."""
    npm = _SpyNpm(ok=True)
    elevation = _SpyInstaller(ok=True)
    written: list[str] = []
    # After npm install, the re-probe reports unprovisioned (srt now present); the elevation's
    # own post-install re-probe then reports provisioned.
    reprobes = iter(
        [
            swp.WindowsSandboxState(
                status=swp.STATUS_UNPROVISIONED,
                reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
                srt=_ready_det(),
            ),
            swp.WindowsSandboxState(
                status=swp.STATUS_PROVISIONED, reason=swp.REASON_WINDOWS_PROVISIONED
            ),
        ]
    )
    result = swp.provision_windows_sandbox(
        state=_srt_absent_node_present(),
        installer=elevation,
        marker_writer=lambda v, **_k: written.append(v),
        state_reader=lambda: next(reprobes),
        npm_installer=npm,
        confirm=lambda _prompt: True,
        verifier=lambda _b: (True, swv.REASON_WINDOWS_ENFORCEMENT_VERIFIED),
    )
    assert result.ok is True
    assert result.status == swp.OUTCOME_PROVISIONED
    assert npm.calls == 1  # srt auto-installed
    assert elevation.calls == ["C:\\srt\\srt.cmd"]  # then provisioned under one UAC
    assert written == ["0.0.66"]


def test_provision_npm_install_failure_is_typed_no_marker() -> None:
    """npm install fails → typed srt_install_failed, no elevation, no marker (no silent path)."""
    npm = _SpyNpm(ok=False, detail="npm install exited 1")
    elevation = _SpyInstaller()
    written: list[str] = []
    result = swp.provision_windows_sandbox(
        state=_srt_absent_node_present(),
        installer=elevation,
        marker_writer=lambda v, **_k: written.append(v),
        npm_installer=npm,
        confirm=lambda _prompt: True,
    )
    assert result.ok is False
    assert result.status == swp.OUTCOME_SRT_INSTALL_FAILED
    assert result.reason == swp.REASON_SRT_INSTALL_FAILED
    assert npm.calls == 1
    assert elevation.calls == []  # never elevated
    assert written == []  # no marker written on a failed install


def test_provision_offer_declined_is_pointer_no_npm() -> None:
    """A declined offer → the typed install pointer; npm is NOT run, nothing elevated."""
    npm = _SpyNpm()
    result = swp.provision_windows_sandbox(
        state=_srt_absent_node_present(), npm_installer=npm, confirm=lambda _prompt: False
    )
    assert result.ok is False
    assert result.status == swp.STATUS_SRT_ABSENT
    assert npm.calls == 0


def test_provision_node_absent_is_pointer_not_offer() -> None:
    """node absent → the offer is NOT made (npm ships with node); the typed pointer stands."""
    npm = _SpyNpm()
    node_absent = sandbox.SrtDetection(
        installed=True,
        binary_path="C:\\srt\\srt.cmd",
        version="",
        node_present=False,  # node (hence npm) absent — the offer must NOT be made
        node_version="",
        node_ok=False,
        socat_present=False,
        reason=sandbox.REASON_SRT_NODE_MISSING,
    )
    state = swp.WindowsSandboxState(
        status=swp.STATUS_SRT_ABSENT,
        reason=sandbox.REASON_SRT_NODE_MISSING,
        srt=node_absent,
        next_action="Install Node.js ...",
    )
    result = swp.provision_windows_sandbox(
        state=state, npm_installer=npm, confirm=lambda _prompt: True
    )
    assert result.ok is False
    assert result.status == swp.STATUS_SRT_ABSENT
    assert npm.calls == 0  # no node ⇒ no npm ⇒ no offer


def test_provision_no_confirm_gate_never_offers() -> None:
    """confirm is None (conservative programmatic default) → no offer, no npm run."""
    npm = _SpyNpm()
    result = swp.provision_windows_sandbox(
        state=_srt_absent_node_present(), npm_installer=npm, confirm=None
    )
    assert result.ok is False
    assert result.status == swp.STATUS_SRT_ABSENT
    assert npm.calls == 0


# --------------------------------------------------------------------------- #
# Ladder — srt_windows activates only when provisioned; floors otherwise.        #
# --------------------------------------------------------------------------- #


def _win_state(status: str, reason: str) -> swp.WindowsSandboxState:
    return swp.WindowsSandboxState(status=status, reason=reason)


def test_ladder_windows_provisioned_activates_srt_windows() -> None:
    """Provisioned Windows → MECHANISM_SRT_WINDOWS active, fence_active, proxy started."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "srt"},  # pin srt: win32's default backend is now codex
        platform="win32",
        detection=_ready_det(),
        win_state=_win_state(swp.STATUS_PROVISIONED, swp.REASON_WINDOWS_PROVISIONED),
        start_proxy=lambda: 43210,
    )
    assert result.mechanism == sandbox.MECHANISM_SRT_WINDOWS
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["proxy_port"] == 43210
    assert result.details["target_mechanism"] == sandbox.MECHANISM_SRT_WINDOWS


def test_ladder_windows_unprovisioned_floors() -> None:
    """Unprovisioned Windows → floor none/windows_unprovisioned (as today)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "srt"},  # pin srt: win32's default backend is now codex
        platform="win32",
        detection=_ready_det(),
        win_state=_win_state(swp.STATUS_UNPROVISIONED, sandbox.REASON_WINDOWS_UNPROVISIONED),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == sandbox.REASON_WINDOWS_UNPROVISIONED


def test_ladder_windows_enforcement_unverified_floors_typed() -> None:
    """Provisioned account but srt can't enforce → floor none, carries the typed reason (#1026).

    The false-green this closes: the account exists, so a marker-only probe reported
    ``srt_windows/active``. The ladder must NOT wrap children with a fence that does nothing —
    it floors to the advisory policy and surfaces ``srt_windows_enforcement_unverified``.
    """
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "srt"},  # pin srt: win32's default backend is now codex
        platform="win32",
        detection=_ready_det(),
        win_state=_win_state(
            swp.STATUS_ENFORCEMENT_UNVERIFIED, swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED
        ),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == swv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED


def test_ladder_windows_srt_absent_floors_with_reason() -> None:
    """srt absent on Windows → floor carries the typed srt precondition reason (not the gate)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "srt"},  # pin srt: win32's default backend is now codex
        platform="win32",
        detection=_det(sandbox.REASON_SRT_NOT_INSTALLED),
        win_state=_win_state(swp.STATUS_SRT_ABSENT, sandbox.REASON_SRT_NOT_INSTALLED),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sandbox.REASON_SRT_NOT_INSTALLED


def test_ladder_windows_provisioned_but_proxy_down_floors_typed() -> None:
    """Provisioned but the chokepoint cannot start → typed chokepoint_start_failed (no silent net)."""
    from clio_agent.runtime.net_chokepoint import ChokepointStartError

    def _boom() -> int:
        raise ChokepointStartError("down")

    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "srt"},  # pin srt: win32's default backend is now codex
        platform="win32",
        detection=_ready_det(),
        win_state=_win_state(swp.STATUS_PROVISIONED, swp.REASON_WINDOWS_PROVISIONED),
        start_proxy=_boom,
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sandbox.REASON_CHOKEPOINT_START_FAILED


# --------------------------------------------------------------------------- #
# Windows shell profile reuses fleet (typed note, never a silent narrowing).     #
# --------------------------------------------------------------------------- #


def _srt_windows_state(port: int = 40000) -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_WINDOWS,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"srt_binary": "C:\\srt\\srt.cmd", "proxy_port": port, "net_enforcement": "proxy"},
    )


def test_windows_shell_reuses_fleet_with_typed_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On srt_windows, the shell profile REUSES fleet territory + stamps a typed details note."""
    from clio_agent.runtime import sandbox_srt

    monkeypatch.setattr(
        sandbox_srt,
        "settings_path_for",
        lambda profile, config=None, cache_dir=None: tmp_path / f"{profile}.json",
    )
    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(tmp_path / "narrow")],
        profile=sandbox.PROFILE_SHELL,
        state=_srt_windows_state(),
    )
    assert confined.result.details["compose_profile"] == sandbox.PROFILE_FLEET
    assert confined.result.details["windows_profile_reuse"]  # a non-empty typed note
    # The composed settings file is written under the FLEET name (shell reuses fleet).
    assert (tmp_path / "fleet.json").is_file()
    assert confined.command == "C:\\srt\\srt.cmd"


def test_windows_fleet_profile_has_no_reuse_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet profile is already fleet — no reuse note (the note is a shell-only signal)."""
    from clio_agent.runtime import sandbox_srt

    monkeypatch.setattr(
        sandbox_srt,
        "settings_path_for",
        lambda profile, config=None, cache_dir=None: tmp_path / f"{profile}.json",
    )
    confined = sandbox.wrap_confined(
        "python",
        [],
        write_roots=[str(tmp_path)],
        profile=sandbox.PROFILE_FLEET,
        state=_srt_windows_state(),
    )
    assert "windows_profile_reuse" not in confined.result.details
    assert confined.result.details["compose_profile"] == sandbox.PROFILE_FLEET


# --------------------------------------------------------------------------- #
# WinError 5 → policy_violation (the B2 EROFS/EACCES path, extended for Windows). #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "path"),
    [
        # Python child bracket form under the srt ACL/WFP fence.
        ("[WinError 5] Access is denied: 'C:\\Users\\u\\out.txt'", "C:\\Users\\u\\out.txt"),
        # strerror text form a shell prints, with a quoted path.
        ("Access is denied: 'C:\\ws\\escaped.txt'", "C:\\ws\\escaped.txt"),
    ],
)
def test_winerror5_denial_mapping(text: str, path: str) -> None:
    from clio_agent.gact.artifacts.violations import write_denial_from_result

    denial = write_denial_from_result({"stderr": text, "exit_code": 1})
    assert denial is not None
    assert denial["errno_name"] == "ERROR_ACCESS_DENIED"
    assert denial["winerror"] == 5
    assert denial["path"] == path


def test_winerror5_mints_prevented_when_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fenced out-of-root WinError-5 mints a prevented violation (srt_windows mechanism).

    Uses REAL ``tmp_path`` roots so containment (``_within_roots``) is evaluated with the
    running OS's own Path flavor — portable on Linux CI and Windows alike (a hardcoded
    ``C:\\...`` string is a single opaque segment under PosixPath, which would false-judge
    containment on the Linux runner; that non-portability was the review blocker).
    """
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside" / "escaped.txt"
    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: (ws,))
    active = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_WINDOWS, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )
    app = FastAPI()
    out = v.observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": f"[WinError 5] Access is denied: '{outside}'"},
        workspace_id="ws1",
        state=active,
        started_at=1000.0,
    )
    assert len(out) == 1
    assert out[0].kind == v.VIOLATION_PREVENTED
    assert out[0].mechanism == sandbox.MECHANISM_SRT_WINDOWS
    assert out[0].errno_name == "ERROR_ACCESS_DENIED"
    assert out[0].path == str(outside)


@pytest.mark.parametrize("kind", ["in_root", "path_less", "out_of_root"])
def test_winerror5_precision_skips(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precision over recall: only a proven out-of-root WinError-5 write mints (no false attribution).

    Real ``tmp_path`` roots/paths so containment uses the running OS's Path flavor — portable
    on Linux CI and Windows (the review blocker was hardcoded ``C:\\...`` strings, which
    PosixPath treats as one opaque segment → an in-root drive path false-judged as out-of-root).
    """
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    ws = tmp_path / "ws"
    ws.mkdir()
    stderr, mints = {
        # In-root WinError 5 (a DAC/mandatory denial inside territory) → NO mint.
        "in_root": (f"[WinError 5] Access is denied: '{ws / 'inside.txt'}'", False),
        # Path-less "access is denied" (no quoted/extractable path) → NO mint.
        "path_less": ("Access is denied.", False),
        # Genuine out-of-root WinError 5 write denial → MINT.
        "out_of_root": (f"[WinError 5] Access is denied: '{tmp_path / 'outside' / 'x.txt'}'", True),
    }[kind]
    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: (ws,))
    active = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_WINDOWS, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )
    app = FastAPI()
    out = v.observe_policy_violations(
        app,
        "s",
        tool_name="bash",
        args={},
        call_id="c",
        result={"stderr": stderr, "exit_code": 1},
        workspace_id="ws",
        state=active,
        started_at=1000.0,
    )
    assert bool(out) is mints


# --------------------------------------------------------------------------- #
# CLI parse + status output shape.                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["setup", "status"])
def test_cli_parses_sandbox_subaction(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`clio sandbox setup` / `clio sandbox status` parse and dispatch to the owner module."""
    from clio_agent.ui import cli

    captured: dict[str, object] = {}

    def _spy(act: object, *, json_output: bool = False, assume_yes: bool = False) -> int:
        captured["action"] = act
        captured["json"] = json_output
        captured["assume_yes"] = assume_yes
        return 0

    monkeypatch.setattr(swp, "run_sandbox_cli", _spy)
    monkeypatch.setattr("sys.argv", ["clio-agent", "sandbox", action, "--json", "--yes"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured["action"] == action
    assert captured["json"] is True
    assert captured["assume_yes"] is True  # --yes threads through to the owner module


def test_run_sandbox_cli_unknown_action_is_typed() -> None:
    """An unknown sandbox action is a typed exit 2, never a traceback."""
    assert swp.run_sandbox_cli("bogus", json_output=True) == 2


def test_sandbox_status_cli_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """`clio sandbox status --json` emits the sandbox doctor row (name=='sandbox')."""
    import json

    code = swp.run_sandbox_cli("status", json_output=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "sandbox"
    assert payload["status"] in {"ready", "degraded", "skipped"}
    assert payload["next_action"]  # a typed next-action is always present


def test_sandbox_setup_cli_non_windows_is_noop(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`clio sandbox setup` off-Windows is a typed no-op (the ladder fences automatically)."""
    monkeypatch.setattr(swp.sys, "platform", "linux")
    code = swp.run_sandbox_cli("setup", json_output=True)
    assert code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == swp.STATUS_NOT_WINDOWS
