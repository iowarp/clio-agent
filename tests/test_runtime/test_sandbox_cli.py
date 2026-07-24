"""B-codex-5: the ``clio sandbox`` CLI (setup/status) + Codex Windows provisioning.

Host-agnostic unit coverage — NO real codex/UAC spawn. The self-elevating ``codex sandbox``
setup is the OWNER-GATED live gate (a UAC prompt that mutates the machine) and is NEVER
exercised here: every test injects a fake ``elevator`` / ``verifier`` / ``gate`` / ``detection``,
so nothing touches ShellExecute or the real codex binary. Pinned:

* :func:`provision_codex_windows` — off-Windows no-op, codex-absent typed guidance,
  idempotent already-provisioned no-op (elevator NEVER called), the fresh-success path
  (elevate → verify → marker → provisioned) and the honest enforcement-unverified degrade (#1026);
* the self-elevation guard raises off-win32 (the real ShellExecute is the manual live gate);
* the ``clio sandbox setup``/``status`` CLI parse + dispatch + status output shape.
"""

from __future__ import annotations

import pytest

from clio_agent.runtime import sandbox_cli as scli
from clio_agent.runtime import sandbox_codex as sc

# --------------------------------------------------------------------------- #
# Injected fakes (never a real codex/UAC spawn).                               #
# --------------------------------------------------------------------------- #


def _codex_ok() -> sc.CodexDetection:
    return sc.CodexDetection(
        installed=True,
        binary_path="C:\\codex\\codex.cmd",
        version="0.145.0",
        reason=sc.REASON_CODEX_DETECTED,
    )


def _codex_absent() -> sc.CodexDetection:
    return sc.CodexDetection(
        installed=False, binary_path="", version="", reason=sc.REASON_CODEX_NOT_INSTALLED
    )


class _SpyElevator:
    """A fake elevated ``codex sandbox`` setup — records calls so we can assert it is NEVER run."""

    def __init__(self, ok: bool = True, detail: str = "ok") -> None:
        self.calls: list[str] = []
        self._ok = ok
        self._detail = detail

    def __call__(self, binary: str) -> tuple[bool, str]:
        self.calls.append(binary)
        return self._ok, self._detail


# --------------------------------------------------------------------------- #
# provision_codex_windows — the flow around the guarded elevation.              #
# --------------------------------------------------------------------------- #


def test_provision_off_windows_is_typed_noop() -> None:
    """Off-Windows there is nothing to provision (codex fences via Seatbelt/bwrap)."""
    spy = _SpyElevator()
    result = scli.provision_codex_windows(platform="linux", elevator=spy)
    assert result.ok is False
    assert result.status == scli.STATUS_NOT_WINDOWS
    assert result.reason == scli.REASON_NOT_WINDOWS
    assert spy.calls == []  # never elevates off-win32


def test_provision_codex_absent_never_elevates() -> None:
    """codex absent → typed guidance + the install pointer; the elevator is NEVER called."""
    spy = _SpyElevator()
    result = scli.provision_codex_windows(platform="win32", detection=_codex_absent(), elevator=spy)
    assert result.ok is False
    assert result.status == scli.OUTCOME_CODEX_ABSENT
    assert result.reason == sc.REASON_CODEX_NOT_INSTALLED
    assert scli.CODEX_INSTALL_POINTER in result.next_action
    assert spy.calls == []


def test_provision_already_provisioned_reapplies_grants_zero_prompt() -> None:
    """Idempotence: a provisioned+verified host re-run no-ops WITHOUT elevating (zero prompts) —
    but STILL (re)applies the fleet-runtime RX grants, so a box provisioned by a prior clio version
    (accounts present, grants absent) gets them on the next `clio sandbox setup`."""
    spy = _SpyElevator()
    granted = [{"grant": "cache", "status": "granted"}]
    result = scli.provision_codex_windows(
        platform="win32",
        detection=_codex_ok(),
        gate=lambda *, platform: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
        elevator=spy,
        grantor=lambda: granted,  # fake grants (never touch icacls)
    )
    assert result.ok is True
    assert result.status == scli.OUTCOME_ALREADY_PROVISIONED
    assert result.reason == scli.REASON_ALREADY_PROVISIONED
    assert spy.calls == []  # the re-run never prompts
    assert result.extra["fleet_runtime_grants"] == granted  # grants ARE applied on the ready path


def test_provision_fresh_success_elevates_verifies_and_marks() -> None:
    """Unprovisioned → one elevation, enforcement verified, marker persisted → provisioned."""
    spy = _SpyElevator(ok=True)
    written: list[tuple[str, object]] = []
    grants = [{"grant": "uv_tool_bin", "status": "granted"}]
    result = scli.provision_codex_windows(
        platform="win32",
        detection=_codex_ok(),
        gate=lambda *, platform: (False, sc.REASON_CODEX_WINDOWS_UNPROVISIONED),
        elevator=spy,
        verifier=lambda _b, _r, platform="win32": (True, sc.REASON_CODEX_ENFORCEMENT_VERIFIED),
        marker_writer=lambda v, **k: written.append((v, k.get("enforcement_verified"))),
        grantor=lambda: grants,  # fake fleet-runtime RX grants (never touch icacls)
    )
    assert result.ok is True
    assert result.status == scli.OUTCOME_PROVISIONED
    assert result.elevated is True
    assert spy.calls == ["C:\\codex\\codex.cmd"]  # the detection's binary
    assert written == [("0.145.0", True)]  # marker persisted: version + verified enforcement
    # The fleet-runtime grant reasons are recorded on the result (never a silent step).
    assert result.extra["fleet_runtime_grants"] == grants


def test_provision_setup_failure_is_typed() -> None:
    """A failed elevation → typed setup_failed with a retry next-action (no raw error)."""
    spy = _SpyElevator(ok=False, detail="elevated codex sandbox setup exited 1")
    written: list[object] = []
    result = scli.provision_codex_windows(
        platform="win32",
        detection=_codex_ok(),
        gate=lambda *, platform: (False, sc.REASON_CODEX_WINDOWS_UNPROVISIONED),
        elevator=spy,
        marker_writer=lambda v, **k: written.append(v),
    )
    assert result.ok is False
    assert result.status == scli.OUTCOME_SETUP_FAILED
    assert result.reason == scli.REASON_SETUP_FAILED
    assert written == []  # no marker written on a failed setup


def test_provision_enforcement_unverified_is_honest_degrade() -> None:
    """Elevation + accounts OK but codex cannot enforce → honest degrade, never green (#1026)."""
    spy = _SpyElevator(ok=True)
    written: list[tuple[str, object]] = []
    result = scli.provision_codex_windows(
        platform="win32",
        detection=_codex_ok(),
        gate=lambda *, platform: (False, sc.REASON_CODEX_WINDOWS_UNPROVISIONED),
        elevator=spy,
        verifier=lambda _b, _r, platform="win32": (False, sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
        marker_writer=lambda v, **k: written.append((v, k.get("enforcement_verified"))),
        grantor=lambda: [{"grant": "user_temp", "status": "granted"}],
    )
    assert result.ok is False
    assert result.status == scli.OUTCOME_ENFORCEMENT_UNVERIFIED
    assert result.reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED
    assert result.elevated is True
    assert written == [
        ("0.145.0", False)
    ]  # marker records the unverified verdict, not a fake green


# --------------------------------------------------------------------------- #
# grant_fleet_runtime_access — the codexsandbox* RX grants (PART B, WINDOWS).    #
# NOTE: no test ever runs a real ``icacls`` — the plan/argv is asserted, and the #
# executor is driven with an injected ``runner``.                                #
# --------------------------------------------------------------------------- #


def test_fleet_grant_plan_builds_rx_argv_for_both_users() -> None:
    """The plan emits ``(OI)(CI)(RX)`` ``/T`` per dir and a bare ``(RX)`` traverse for Temp."""
    plan = scli.build_fleet_runtime_grant_plan(
        paths_override=[
            ("uv_tool_bin", "C:\\u\\bin", True),
            ("user_temp", "C:\\u\\Temp", False),
        ]
    )
    assert [g.label for g in plan] == ["uv_tool_bin", "user_temp"]
    # Both codex restricted users are covered on every path.
    assert plan[0].users == scli.CODEX_SANDBOX_USERS
    # A recursive-inherit dir grant → one icacls per user with (OI)(CI)(RX) + /T.
    argv = plan[0].icacls_argv()
    assert argv == [
        ["icacls", "C:\\u\\bin", "/grant", "CodexSandboxOffline:(OI)(CI)(RX)", "/T"],
        ["icacls", "C:\\u\\bin", "/grant", "CodexSandboxOnline:(OI)(CI)(RX)", "/T"],
    ]
    # Temp is a bare (RX) traverse — no inheritance flags, no /T.
    temp_argv = plan[1].icacls_argv()
    assert temp_argv == [
        ["icacls", "C:\\u\\Temp", "/grant", "CodexSandboxOffline:(RX)"],
        ["icacls", "C:\\u\\Temp", "/grant", "CodexSandboxOnline:(RX)"],
    ]


def test_fleet_grant_runs_icacls_for_existing_paths_only() -> None:
    """An existing path grants both users (typed granted); a missing path is a typed skip."""
    plan = [
        scli.FleetGrant(
            label="uv_tool_bin",
            path="C:\\exists",
            users=scli.CODEX_SANDBOX_USERS,
            inherit=True,
            exists=True,
        ),
        scli.FleetGrant(
            label="uv_python",
            path="C:\\gone",
            users=scli.CODEX_SANDBOX_USERS,
            inherit=True,
            exists=False,
        ),
    ]
    ran: list[list[str]] = []

    def _runner(argv: list[str]) -> tuple[int, str]:
        ran.append(argv)
        return 0, "processed 1 files"

    reasons = scli.grant_fleet_runtime_access(runner=_runner, plan=plan, platform="win32")
    # The existing path ran icacls once PER user; the missing path ran nothing.
    assert ran == plan[0].icacls_argv()
    granted = [r for r in reasons if r["status"] == "granted"]
    assert {r["user"] for r in granted} == set(scli.CODEX_SANDBOX_USERS)
    assert all(r["path"] == "C:\\exists" for r in granted)
    skipped = [r for r in reasons if r["status"] == "skipped_missing"]
    assert skipped == [{"grant": "uv_python", "path": "C:\\gone", "status": "skipped_missing"}]


def test_fleet_grant_reports_typed_failure_never_raises() -> None:
    """A non-zero icacls is a typed ``failed`` reason (logged, stepped past), never an exception."""
    plan = [
        scli.FleetGrant(
            label="clio_kit_cache",
            path="C:\\cache",
            users=("CodexSandboxOffline",),
            inherit=True,
            exists=True,
        )
    ]
    reasons = scli.grant_fleet_runtime_access(
        runner=lambda _argv: (5, "Access is denied."), plan=plan, platform="win32"
    )
    assert reasons[0]["status"] == "failed"
    assert reasons[0]["rc"] == 5
    assert "Access is denied." in reasons[0]["detail"]


def test_fleet_grant_is_typed_noop_off_windows() -> None:
    """Off-win32 the grant is a typed no-op — no icacls, no plan build, a structured skip reason."""
    ran = {"n": 0}
    reasons = scli.grant_fleet_runtime_access(
        runner=lambda _a: ran.__setitem__("n", ran["n"] + 1) or (0, ""),
        platform="linux",
    )
    assert ran["n"] == 0
    assert reasons == [{"grant": "fleet_runtime_access", "status": "skipped_not_windows"}]


def test_fleet_grant_plan_resolves_the_expected_windows_paths() -> None:
    """The real resolver names all five fleet-runtime paths (uv bin/tools/python, cache, Temp)."""
    labels = {label for label, _p, _i in scli._resolve_fleet_runtime_paths()}
    assert labels == {"uv_tool_bin", "uv_tools", "uv_python", "clio_kit_cache", "user_temp"}
    # Temp is the only traverse-only (non-inherit) grant; the rest are recursive dir trees.
    specs = {label: inherit for label, _p, inherit in scli._resolve_fleet_runtime_paths()}
    assert specs["user_temp"] is False
    assert all(v for k, v in specs.items() if k != "user_temp")


def test_default_elevator_is_guarded_off_win32() -> None:
    """The default elevator IS the self-elevation, structurally guarded off-win32.

    Off win32 the bound function is the raising stub — calling it raises BEFORE any ctypes
    touch. On win32 it is the real ShellExecute impl, which must NOT be invoked here (it pops a
    UAC prompt — the owner-gated manual live gate); we assert its identity only.
    """
    assert scli._elevated_codex_setup.__name__ == "_elevated_codex_setup"
    if scli.sys.platform != "win32":
        with pytest.raises(RuntimeError):
            scli._elevated_codex_setup("C:\\codex\\codex.cmd")


# --------------------------------------------------------------------------- #
# CLI parse + status/setup output shape.                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["setup", "status"])
def test_cli_parses_sandbox_subaction(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`clio sandbox setup` / `clio sandbox status` parse and dispatch to the owner module."""
    from clio_agent.ui import cli

    captured: dict[str, object] = {}

    def _spy(act: object, *, json_output: bool = False, assume_yes: bool = False) -> int:
        captured["action"] = act
        captured["json"] = json_output
        return 0

    monkeypatch.setattr(scli, "run_sandbox_cli", _spy)
    monkeypatch.setattr("sys.argv", ["clio-agent", "sandbox", action, "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured["action"] == action
    assert captured["json"] is True


def test_run_sandbox_cli_unknown_action_is_typed() -> None:
    """An unknown sandbox action is a typed exit 2, never a traceback."""
    assert scli.run_sandbox_cli("bogus", json_output=True) == 2


def test_sandbox_status_cli_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """`clio sandbox status --json` emits the sandbox doctor row (name=='sandbox')."""
    import json

    code = scli.run_sandbox_cli("status", json_output=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "sandbox"
    assert payload["status"] in {"ready", "degraded", "skipped"}
    assert payload["next_action"]  # a typed next-action is always present


def test_sandbox_setup_cli_non_windows_is_noop(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`clio sandbox setup` off-Windows is a typed no-op (codex fences automatically)."""
    monkeypatch.setattr(scli.sys, "platform", "linux")
    code = scli.run_sandbox_cli("setup", json_output=True)
    assert code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == scli.STATUS_NOT_WINDOWS
